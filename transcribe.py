#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
تفريغ التسجيلات والمكالمات بالعامية المصرية -- بدقة وبأقل تكلفة ممكنة.

الفكرة الأساسية:
  1) نضغط الصوت ذكيًا (16kHz mono Opus) -- ملف 100MB يصير ~10MB/ساعة
     بدون أي خسارة في دقة التفريغ، لأن كل محركات ASR تعمل داخليًا على 16kHz mono.
  2) نقطّع عند لحظات الصمت (لا عند زمن ثابت) -- القطع في منتصف كلمة هو
     السبب الأول لأخطاء التفريغ.
  3) نمرّر ذيل النص السابق كسياق لكل قطعة -- يحافظ على تسلسل الكلام.
  4) نخرج txt + srt + json.

الاستخدام:
    python transcribe.py "C:\\path\\call.m4a"
    python transcribe.py "C:\\path\\call.m4a" --provider groq
    python transcribe.py "C:\\path\\call.m4a" --speakers 2
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# ---------------------------------------------------------------- الإعدادات

# الطول المستهدف للقطعة الواحدة بالثواني. 900 = 15 دقيقة.
# قطع أقصر = دقة أعلى وتكلفة توكن أقل لكل طلب، وأمان أكبر مع حدود الحجم.
TARGET_CHUNK_SEC = 900
# أقصى طول مسموح قبل القطع القسري لو ما لقينا صمت مناسب.
MAX_CHUNK_SEC = 1200
# عتبة الصمت. -30dB مناسبة لتسجيلات الموبايل والمكالمات (فيها ضجيج خلفية).
SILENCE_NOISE_DB = "-30dB"
SILENCE_MIN_DUR = "0.6"

# معامل الضغط: Opus 24kbps mono 16kHz ~= 10.8 MB لكل ساعة صوت.
OPUS_BITRATE = "24k"
SAMPLE_RATE = "16000"

ENV_FILE = Path(__file__).with_name(".env")


def log(msg: str) -> None:
    print(msg, flush=True)


def die(msg: str) -> "None":
    # نمرّر الرسالة نفسها لـ SystemExit (لا 1 فقط) حتى تصل كاملة لأي كود يمسك
    # SystemExit ليتعامل مع الفشل بلطف (مثل compare.py وسلسلة موديلات Gemini)،
    # مع بقاء نفس السلوك عند عدم مسكها: بايثون يطبع الرسالة على stderr ويخرج بكود 1.
    raise SystemExit(f"[خطأ] {msg}")


# ---------------------------------------------------------------- المفاتيح

def load_env() -> None:
    """يقرأ .env بجانب السكربت بدون أي تبعية خارجية."""
    if not ENV_FILE.exists():
        return
    for raw in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        # متغيرات البيئة الحقيقية لها الأولوية على الملف
        if key and key not in os.environ:
            os.environ[key] = val


# ---------------------------------------------------------------- ffmpeg

def find_binary(name: str) -> str:
    """يبحث عن ffmpeg/ffprobe في PATH ثم في مجلدات winget المعروفة."""
    found = shutil.which(name)
    if found:
        return found

    local = os.environ.get("LOCALAPPDATA", "")
    roots = []
    if local:
        roots.append(Path(local) / "Microsoft" / "WinGet" / "Packages")
    override = os.environ.get("FFMPEG_DIR")
    if override:
        roots.insert(0, Path(override))

    exe = f"{name}.exe" if os.name == "nt" else name
    for root in roots:
        if not root.exists():
            continue
        direct = root / exe
        if direct.exists():
            return str(direct)
        for hit in root.rglob(exe):
            return str(hit)

    die(
        f"لم أجد {name}.\n"
        "  ثبّته بأمر واحد:  winget install --id Gyan.FFmpeg -e\n"
        "  أو حدّد مجلده:    setx FFMPEG_DIR \"C:\\path\\to\\ffmpeg\\bin\""
    )
    raise SystemExit(1)  # لا يُنفَّذ، لإسكات المدقق


def run_ffmpeg(args: list[str], capture_stderr: bool = False) -> str:
    """ينفّذ ffmpeg. ffmpeg يكتب تقاريره على stderr وليس stdout."""
    proc = subprocess.run(
        args,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    if proc.returncode != 0 and not capture_stderr:
        tail = "\n".join((proc.stderr or "").strip().splitlines()[-8:])
        die(f"ffmpeg فشل:\n{tail}")
    return proc.stderr or ""


def probe_duration(ffprobe: str, path: Path) -> float:
    """مدة الملف بالثواني."""
    proc = subprocess.run(
        [
            ffprobe, "-v", "error",
            "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1",
            str(path),
        ],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace",
    )
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        die(f"لم أستطع قراءة مدة الملف. تأكد إنه ملف صوت صالح:\n{path}")
        return 0.0


def to_opus(ffmpeg: str, src: Path, dst: Path) -> None:
    """
    يوحّد أي صيغة (m4a/mp3/wav/opus/amr/فيديو) إلى 16kHz mono Opus.

    هذا هو ما يحل مشكلة الحجم: تسجيل موبايل 100MB يصير ~10MB لكل ساعة،
    فيدخل تحت حدود كل المزودين المجانيين -- وبدون خسارة دقة، لأن الـ ASR
    يخفّض العينة إلى 16kHz mono على أي حال.
    """
    run_ffmpeg([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-i", str(src),
        "-vn",                          # نتجاهل أي مسار فيديو
        "-ac", "1",                     # mono
        "-ar", SAMPLE_RATE,             # 16 kHz
        "-c:a", "libopus",
        "-b:a", OPUS_BITRATE,
        "-application", "voip",         # مُحسَّن للكلام لا الموسيقى
        "-af", "highpass=f=80,dynaudnorm=f=200:g=5",  # تنظيف خفيف وتسوية مستوى
        str(dst),
    ])


def detect_silences(ffmpeg: str, path: Path) -> list[float]:
    """
    يرجّع لحظات الصمت (منتصف كل فترة صمت) كنقاط قطع مرشّحة.
    """
    out = run_ffmpeg([
        ffmpeg, "-hide_banner", "-nostats",
        "-i", str(path),
        "-af", f"silencedetect=noise={SILENCE_NOISE_DB}:d={SILENCE_MIN_DUR}",
        "-f", "null", "-",
    ], capture_stderr=True)

    starts = [float(m) for m in re.findall(r"silence_start:\s*(-?[\d.]+)", out)]
    ends = [float(m) for m in re.findall(r"silence_end:\s*(-?[\d.]+)", out)]

    points: list[float] = []
    for i, s in enumerate(starts):
        e = ends[i] if i < len(ends) else None
        points.append(s + (min(e - s, 1.0) / 2) if e and e > s else s)
    return sorted(p for p in points if p > 0)


def plan_chunks(duration: float, silences: list[float]) -> list[tuple[float, float]]:
    """
    يبني قائمة (بداية، نهاية) بحيث كل قطعة تنتهي عند صمت قريب من الهدف.
    لو ما فيه صمت مناسب، نقطع قسريًا عند MAX_CHUNK_SEC.
    """
    if duration <= MAX_CHUNK_SEC:
        return [(0.0, duration)]

    chunks: list[tuple[float, float]] = []
    start = 0.0
    while duration - start > MAX_CHUNK_SEC:
        target = start + TARGET_CHUNK_SEC
        window_lo = start + TARGET_CHUNK_SEC * 0.5
        window_hi = start + MAX_CHUNK_SEC
        candidates = [p for p in silences if window_lo <= p <= window_hi]
        cut = min(candidates, key=lambda p: abs(p - target)) if candidates else window_hi
        chunks.append((start, cut))
        start = cut
    chunks.append((start, duration))
    return chunks


def slice_chunk(ffmpeg: str, src: Path, dst: Path, start: float, end: float) -> None:
    """يقطع مقطعًا بنسخ التيار مباشرة -- بلا إعادة ترميز، فسريع جدًا."""
    run_ffmpeg([
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-ss", f"{start:.3f}",
        "-t", f"{max(end - start, 0.1):.3f}",
        "-i", str(src),
        "-c", "copy",
        str(dst),
    ])



# ---------------------------------------------------------------- المزودون
#
# كل مزود يستقبل مقطعًا واحدًا ويرجّع قائمة أسطر:
#   {"start": float(ثانية داخل المقطع), "speaker": str|None, "text": str}

HTTP_TIMEOUT = 900


@dataclass
class Line:
    start: float
    text: str
    speaker: str | None = None


# --- الـ prompt: أهم سطر في المشروع كله --------------------------------
#
# الخطأ الأشهر في تفريغ العامية المصرية إن الموديل "يترجم" الكلام لفصحى
# فيضيع المعنى والنبرة. الـ prompt ده يمنع ذلك صراحةً.

def build_prompt(speakers: int, context_tail: str, hints: str) -> str:
    p = [
        "أنت مفرّغ صوتي محترف متخصص في اللهجة المصرية العامية.",
        "فرّغ هذا التسجيل حرفيًا (verbatim) بالعامية المصرية كما نُطقت بالضبط.",
        "",
        "قواعد إلزامية:",
        "- اكتب الكلام بالعامية المصرية كما هو. لا تترجمه للعربية الفصحى،"
        " ولا تصحّح النحو، ولا تُعِد صياغته، ولا تلخّصه، ولا تحذف التكرار.",
        "- احتفظ بالكلمات العامية كما تُلفَظ: إيه، دلوقتي، عشان، مش، كده، يلا،"
        " إزيك، خلاص، بقى، أهو، ماشي.",
        "- الكلمات الأجنبية المنطوقة وسط الكلام اكتبها بالعربية كما تُلفَظ.",
        "- اكتب الأرقام كما نُطقت بالحروف.",
        "- أي كلام غير مفهوم اكتبه [غير واضح]. لا تخترع كلامًا أبدًا.",
        "- الضحك أو التنحنح أو الصمت الطويل: [ضحك] [تنحنح] [صمت].",
        "- لا تكتب أي مقدمة أو تعليق أو خاتمة. المخرج نص التفريغ فقط.",
        "",
        "صيغة المخرج -- سطر لكل جملة، بهذا الشكل بالضبط:",
    ]
    if speakers and speakers > 1:
        p += [
            "[mm:ss] متحدث ١: نص الكلام",
            "[mm:ss] متحدث ٢: نص الكلام",
            "",
            f"التسجيل مكالمة فيها {speakers} متحدثين تقريبًا."
            " ميّز بينهم بثبات من نبرة الصوت، واستخدم نفس الترقيم من أول"
            " التسجيل لآخره.",
        ]
    else:
        p += ["[mm:ss] نص الكلام"]

    p += ["", "الطابع الزمني [mm:ss] محسوب من بداية هذا المقطع (يبدأ من 00:00)."]

    if hints.strip():
        p += [
            "",
            "أسماء ومصطلحات واردة في التسجيل -- اكتبها بهذا الرسم بالضبط:",
            hints.strip(),
        ]
    if context_tail.strip():
        p += [
            "",
            "لمعلوماتك فقط، هذا آخر ما قيل في المقطع السابق (لا تُعِد كتابته):",
            f"...{context_tail.strip()[-400:]}",
        ]
    return "\n".join(p)


_TS_LINE = re.compile(
    r"^\s*\[?\s*(\d{1,2}):(\d{2})(?::(\d{2}))?\s*\]?\s*(.*)$"
)


def parse_timestamped(raw: str) -> list[Line]:
    """يحوّل مخرج الموديل النصي إلى أسطر لها زمن ومتحدث."""
    lines: list[Line] = []
    for raw_line in raw.splitlines():
        s = raw_line.strip()
        if not s or s.startswith("```"):
            continue
        m = _TS_LINE.match(s)
        if m:
            a, b, c, rest = m.groups()
            start = (
                int(a) * 3600 + int(b) * 60 + int(c)
                if c else int(a) * 60 + int(b)
            )
            speaker = None
            sm = re.match(r"^\s*(متحدث\s*[^\s:]+|Speaker\s*\w+)\s*:\s*(.*)$", rest)
            if sm:
                speaker, rest = sm.group(1).strip(), sm.group(2)
            if rest.strip():
                lines.append(Line(float(start), rest.strip(), speaker))
        elif lines:
            lines[-1].text += " " + s
        else:
            lines.append(Line(0.0, s))
    return lines


def _post_json(url: str, payload: dict, headers: dict,
               max_retries: int = 5) -> dict:
    """
    طلب POST مع إعادة محاولة على الأعطال المؤقتة (429 و5xx).

    `max_retries` هو عدد المحاولات على **نفس** العنوان قبل الاستسلام:

    - الافتراضي 5 يحافظ على سلوك `ask.py`، حيث لا يوجد بديل عن الطلب: إن فشل
      فقد فشل السؤال كله، فالانتظار أفضل من الفشل.
    - سلسلة موديلات Gemini تمرّر رقمًا **صغيرًا** عن قصد. الانتظار الطويل على
      موديل مشغول لا معنى له وقد يوجد موديل آخر بحصّة مستقلة تمامًا: السقوط
      للتالي أسرع وأرجح نجاحًا من الانتظار. بـ 5 محاولات كان كل موديل ميت أو
      مشغول يكلّف ٢٠+٤٠+٦٠+٨٠+١٠٠ = **خمس دقائق** قبل تجربة التالي، أي ربع
      ساعة على سلسلة من ثلاثة. هذا يقتل أي تشغيل مجدول.
    """
    import requests
    last_err = ""
    attempts = max(1, int(max_retries))
    for attempt in range(attempts):
        try:
            r = requests.post(url, json=payload, headers=headers, timeout=HTTP_TIMEOUT)
        except Exception as exc:  # شبكة متقطعة
            last_err = str(exc)
            if attempt + 1 < attempts:
                time.sleep(min(4 * (attempt + 1), 30))
            continue
        if r.status_code == 200:
            return r.json()
        # 429 = تجاوزنا حد الطبقة المجانية، 5xx = عطل مؤقت -> نعيد المحاولة
        if r.status_code == 429 or r.status_code >= 500:
            last_err = f"HTTP {r.status_code}: {r.text[:400]}"
            if attempt + 1 >= attempts:
                break                      # لا تنم قبل الاستسلام -- انتظار مهدور
            wait = min(20 * (attempt + 1), 60)
            log(f"    الخدمة مشغولة ({r.status_code}) -- إعادة محاولة بعد {wait}ث")
            time.sleep(wait)
            continue
        die(f"المزود رفض الطلب ({r.status_code}):\n{r.text[:800]}")
    die(f"فشل الطلب بعد {attempts} محاولة. آخر خطأ:\n{last_err[:800]}")
    return {}


#
# سلسلة موديلات Gemini -- من الأقوى إلى الأكثر توفرًا في الطبقة المجانية.
#
# جوجل تُعيد تسمية/تُقاعِد أسماء موديلات Gemini كل بضعة أشهر (حصل بالفعل مع
# gemini-2.5-flash -- رجع 404 "no longer available to new users"). فبدل اسم
# ثابت واحد، نجرّب القائمة بالترتيب: أي فشل خاص بموديل معيّن (404 تقاعد،
# 429 تجاوز حد الطبقة المجانية، 5xx عطل مؤقت) يسقط للموديل التالي تلقائيًا.
# فقط خطأ مفتاح/تصريح حقيقي (401 أو "api key" في الرسالة) يوقف السلسلة كلها
# فورًا -- لأنه سيفشل بنفس الشكل على كل موديل.
#
# للتخصيص بلا تعديل كود: متغيّر بيئة GEMINI_MODELS بفاصلة، مثلًا:
#   GEMINI_MODELS=gemini-flash-latest,gemini-3.5-flash-lite
DEFAULT_GEMINI_MODELS = [
    "gemini-flash-latest",      # alias ذاتي التحديث -- يشير دائمًا لموديل Flash الحالي
    "gemini-3.6-flash",         # الرائد الحالي في عيلة Flash
    "gemini-3.5-flash-lite",    # أسرع وأرخص، توفر عالٍ في الطبقة المجانية
]
# أُزيل "gemini-2.5-flash-lite": تقاعد فعلًا، ورُصد يرجع 404 في تشغيل حقيقي
# ("no longer available to new users ... use models/gemini-3.5-flash-lite").
# لا حاجة لإضافة بدائل يدويًا بعد الآن: النمط أدناه يقرأ اسم البديل من نص
# الخطأ نفسه ويجرّبه فورًا، فالسلسلة تُصلح نفسها عند أي تقاعد لاحق.
_RETIRED_REPLACEMENT = re.compile(r"use\s+models/([A-Za-z0-9._\-]+)")


def _gemini_model_cascade() -> list[str]:
    raw = os.environ.get("GEMINI_MODELS", "").strip()
    if raw:
        return [m.strip() for m in raw.split(",") if m.strip()]
    return list(DEFAULT_GEMINI_MODELS)


def transcribe_gemini(path: Path, speakers: int, tail: str, hints: str) -> list[Line]:
    """Gemini (سلسلة موديلات Flash) -- الطبقة المجانية. الأفضل للعامية لأنه يفهم السياق."""
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        die("GEMINI_API_KEY غير موجود. احصل على مفتاح مجاني من"
            " https://aistudio.google.com/apikey")

    audio_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    payload = {
        "contents": [{
            "parts": [
                {"text": build_prompt(speakers, tail, hints)},
                {"inline_data": {"mime_type": "audio/ogg", "data": audio_b64}},
            ]
        }],
        "generationConfig": {
            "temperature": 0.0,          # صفر = أقل هلوسة
            "maxOutputTokens": 65536,
            # التفريغ لا يحتاج تفكيرًا -- إطفاؤه يوفّر وقتًا وتوكن
            "thinkingConfig": {"thinkingBudget": 0},
        },
    }
    headers = {"x-goog-api-key": key, "Content-Type": "application/json"}

    models = [os.environ["GEMINI_MODEL"]] if os.environ.get("GEMINI_MODEL", "").strip() else _gemini_model_cascade()

    # محاولات قليلة عن قصد: موديل مشغول لا يستحق الانتظار عندما يوجد موديل آخر
    # بحصّة مستقلة. قياس حقيقي: بـ 5 محاولات استهلكت السلسلة 1012 ثانية ثم فشلت.
    try:
        per_model_retries = max(1, int(os.environ.get("GEMINI_RETRIES", "2")))
    except ValueError:
        per_model_retries = 2

    queue = list(models)
    tried: list[str] = []
    last_error = ""

    while queue:
        model = queue.pop(0)
        if model in tried:
            continue
        tried.append(model)
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            data = _post_json(url, payload, headers, max_retries=per_model_retries)
        except SystemExit as exc:
            msg = str(exc)
            # مشكلة مفتاح/تصريح حقيقية -- ستفشل بنفس الشكل على كل موديل، لا فائدة من المحاولة أكثر
            if "api key" in msg.lower() or "401" in msg[:60]:
                raise
            last_error = msg

            # عند تقاعد موديل، جوجل تُرجع 404 وتسمّي البديل صراحةً في نص الخطأ:
            # "This model ... is no longer available. Please update your code to
            #  use models/X". فنتبع البديل فورًا بدل انتظار تحديث يدوي للقائمة.
            hint = _RETIRED_REPLACEMENT.search(msg)
            if hint and hint.group(1) not in tried:
                repl = hint.group(1)
                queue.insert(0, repl)
                log(f"    '{model}' متقاعد -- الخدمة تقترح '{repl}'، أجرّبه فورًا")
            else:
                log(f"    الموديل '{model}' فشل -- التالي في السلسلة...")
            continue

        try:
            parts = data["candidates"][0]["content"]["parts"]
            text = "".join(p.get("text", "") for p in parts)
        except (KeyError, IndexError):
            last_error = json.dumps(data)[:500]
            log(f"    مخرج غير متوقع من '{model}' -- التالي في السلسلة...")
            continue

        if len(tried) > 1:
            log(f"    نجح عبر الموديل '{model}' (بعد سقوط {len(tried)-1})")
        return parse_timestamped(text)

    busy = "503" in last_error or "429" in last_error or "overloaded" in last_error.lower()
    die(f"كل موديلات Gemini فشلت. آخر خطأ:\n{last_error[:800]}"
        f"\nالموديلات المُجرَّبة بالترتيب: {tried}\n"
        + ("الأخطاء من نوع ازدحام (503/429) لا عيب في المفتاح ولا في الكود:"
           " الخدمة كانت مشغولة فعلًا. أعد المحاولة لاحقًا، أو استخدم"
           " --provider groq الآن.\n" if busy else
           "خصّصها عبر متغيّر البيئة GEMINI_MODELS (بفاصلة).\n"))
    return []


def transcribe_groq(path: Path, speakers: int, tail: str, hints: str) -> list[Line]:
    """Groq whisper-large-v3 -- مجاني وسريع جدًا. يعطي طوابع زمنية دقيقة."""
    import requests

    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        die("GROQ_API_KEY غير موجود. احصل على مفتاح مجاني من"
            " https://console.groq.com/keys")

    model = os.environ.get("GROQ_MODEL", "whisper-large-v3").strip()
    # Whisper يستفيد من prompt قصير يضبط اللهجة والرسم -- مش تعليمات طويلة.
    prompt = "تسجيل بالعامية المصرية. " + (hints.strip() or "")

    for attempt in range(5):
        with path.open("rb") as fh:
            r = requests.post(
                "https://api.groq.com/openai/v1/audio/transcriptions",
                headers={"Authorization": f"Bearer {key}"},
                files={"file": (path.name, fh, "audio/ogg")},
                data={
                    "model": model,
                    "language": "ar",
                    "response_format": "verbose_json",
                    "temperature": "0",
                    "prompt": prompt[:880],
                },
                timeout=HTTP_TIMEOUT,
            )
        if r.status_code == 200:
            segs = r.json().get("segments") or []
            if segs:
                return [
                    Line(float(s.get("start", 0.0)), (s.get("text") or "").strip())
                    for s in segs if (s.get("text") or "").strip()
                ]
            return [Line(0.0, (r.json().get("text") or "").strip())]
        if r.status_code == 429 or r.status_code >= 500:
            wait = 20 * (attempt + 1)
            log(f"    Groq مشغول ({r.status_code}) -- إعادة محاولة بعد {wait}ث")
            time.sleep(wait)
            continue
        die(f"Groq رفض الطلب ({r.status_code}):\n{r.text[:800]}")
    die("فشل Groq بعد 5 محاولات.")
    return []


def transcribe_elevenlabs(path: Path, speakers: int, tail: str, hints: str) -> list[Line]:
    """ElevenLabs Scribe -- الأدق مقاسًا على العامية المصرية، لكنه مدفوع."""
    import requests

    key = os.environ.get("ELEVENLABS_API_KEY", "").strip()
    if not key:
        die("ELEVENLABS_API_KEY غير موجود. من https://elevenlabs.io/app/settings/api-keys")

    fields = {
        "model_id": os.environ.get("ELEVENLABS_MODEL", "scribe_v2"),
        "language_code": "ara",
        "diarize": "true" if speakers and speakers > 1 else "false",
        "timestamps_granularity": "word",
    }
    if speakers and speakers > 1:
        fields["num_speakers"] = str(speakers)

    for attempt in range(5):
        with path.open("rb") as fh:
            r = requests.post(
                "https://api.elevenlabs.io/v1/speech-to-text",
                headers={"xi-api-key": key},
                files={"file": (path.name, fh, "audio/ogg")},
                data=fields,
                timeout=HTTP_TIMEOUT,
            )
        if r.status_code == 200:
            body = r.json()
            words = body.get("words") or []
            if not words:
                return [Line(0.0, (body.get("text") or "").strip())]
            # نجمع الكلمات في أسطر عند تغيّر المتحدث أو عند وقفة طويلة
            out: list[Line] = []
            cur: list[str] = []
            cur_start, cur_spk, last_end = 0.0, None, 0.0
            for w in words:
                if w.get("type") == "spacing":
                    continue
                spk = w.get("speaker_id")
                st = float(w.get("start", 0.0))
                if cur and (spk != cur_spk or st - last_end > 1.2):
                    out.append(Line(cur_start, " ".join(cur).strip(), cur_spk))
                    cur = []
                if not cur:
                    cur_start, cur_spk = st, spk
                cur.append(w.get("text", ""))
                last_end = float(w.get("end", st))
            if cur:
                out.append(Line(cur_start, " ".join(cur).strip(), cur_spk))
            return [l for l in out if l.text]
        if r.status_code == 429 or r.status_code >= 500:
            time.sleep(20 * (attempt + 1))
            continue
        die(f"ElevenLabs رفض الطلب ({r.status_code}):\n{r.text[:800]}")
    die("فشل ElevenLabs بعد 5 محاولات.")
    return []


_LOCAL_MODEL = {}


def transcribe_local(path: Path, speakers: int, tail: str, hints: str) -> list[Line]:
    """
    faster-whisper على المعالج -- بدون أي مفتاح وبدون أي تكلفة إطلاقًا.
    أبطأ من الـ API (ساعة صوت ~ 25-40 دقيقة على 4 أنوية) لكنه مجاني بلا حدود.
    """
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        die("الوضع المحلي يحتاج:  pip install faster-whisper")
        return []

    size = os.environ.get("WHISPER_SIZE", "large-v3")
    if "m" not in _LOCAL_MODEL:
        log(f"    تحميل موديل whisper {size} (أول مرة فقط)...")
        _LOCAL_MODEL["m"] = WhisperModel(size, device="cpu", compute_type="int8")

    segments, _ = _LOCAL_MODEL["m"].transcribe(
        str(path),
        language="ar",
        beam_size=5,
        vad_filter=True,                       # يتجاهل الصمت -> أسرع وأنضف
        condition_on_previous_text=False,       # يمنع الدخول في حلقة تكرار
        initial_prompt="تسجيل بالعامية المصرية. " + (hints.strip() or ""),
    )
    return [
        Line(float(s.start), s.text.strip())
        for s in segments if s.text and s.text.strip()
    ]


_QWENCLEO: dict = {}


def transcribe_qwencleo(path: Path, speakers: int, tail: str, hints: str) -> list[Line]:
    """
    QwenCleo-ASR -- موديل مفتوح (Apache-2.0) مبني على Qwen3-ASR-1.7B
    ومدرَّب خصيصًا على العامية المصرية وعلى خلط العربي بالإنجليزي.

    ميزته الحقيقية على Whisper: لا "يفصّح" الكلام، ويحفظ الكلمات الإنجليزية
    المنطوقة وسط الكلام بحروف لاتينية بدل تشويهها لعربي مكسور.

    حجمه 1.7B فقط -- يعمل على أي GPU متواضع، بل وعلى Colab المجاني.
    """
    try:
        from qwencleo_asr import QwenCleoASR, stream_file
    except ImportError:
        die("هذا المحرك يحتاج تثبيتًا أولًا:\n"
            "  pip install torch torchaudio\n"
            "  pip install qwencleo-asr --no-deps\n"
            '  pip install "qwen-asr>=0.0.6" numpy soundfile huggingface_hub')
        return []

    if "m" not in _QWENCLEO:
        log("    تحميل موديل QwenCleo (أول مرة فقط)...")
        _QWENCLEO["m"] = QwenCleoASR()
    asr = _QWENCLEO["m"]

    # الموديل يقطّع داخليًا لنوافذ قصيرة متداخلة، فنعطيه المقطع كما هو.
    lines: list[Line] = []
    for ch in stream_file(asr, str(path), chunk_s=20, overlap_s=2):
        text = (getattr(ch, "text", "") or "").strip()
        if text:
            lines.append(Line(float(getattr(ch, "start", 0.0)), text))
    return lines


PROVIDERS = {
    "qwencleo": transcribe_qwencleo,     # الأدق للمصري بين المفتوحات -- مجاني
    "gemini": transcribe_gemini,
    "groq": transcribe_groq,
    "elevenlabs": transcribe_elevenlabs,  # الأدق المقاس مستقلًا -- مدفوع
    "local": transcribe_local,            # Whisper -- عام، أضعف على العامية
}



# ---------------------------------------------------------------- الإخراج

def fmt_ts(sec: float, srt: bool = False) -> str:
    sec = max(sec, 0.0)
    h, rem = divmod(int(sec), 3600)
    m, s = divmod(rem, 60)
    if srt:
        ms = int((sec - int(sec)) * 1000)
        return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"
    return f"{h:02d}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"


def write_outputs(lines: list[Line], meta: dict, out_dir: Path, stem: str) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = {}

    # --- نص للقراءة والمناقشة (هذا هو الملف الذي ستتحدث معه لاحقًا)
    txt = out_dir / f"{stem}.md"
    head = [
        f"# {meta.get('title') or stem}",
        "",
        f"- التاريخ: {meta.get('date', '')}",
        f"- المدة: {fmt_ts(meta.get('duration', 0))}",
        f"- المصدر: {meta.get('source', '')}",
        f"- محرك التفريغ: {meta.get('provider', '')}",
        "",
        "---",
        "",
    ]
    body = []
    for l in lines:
        who = f"**{l.speaker}:** " if l.speaker else ""
        body.append(f"`[{fmt_ts(l.start)}]` {who}{l.text}")
    txt.write_text("\n".join(head + body) + "\n", encoding="utf-8")
    written["md"] = txt

    # --- نص خام بلا طوابع، مريح للقراءة المتصلة
    plain = out_dir / f"{stem}.txt"
    plain.write_text(
        "\n".join(
            (f"{l.speaker}: {l.text}" if l.speaker else l.text) for l in lines
        ) + "\n",
        encoding="utf-8",
    )
    written["txt"] = plain

    # --- ترجمة متزامنة للاستماع مع المتابعة
    srt = out_dir / f"{stem}.srt"
    blocks = []
    for i, l in enumerate(lines, 1):
        end = lines[i].start if i < len(lines) else l.start + 5
        end = max(end, l.start + 1.0)
        who = f"{l.speaker}: " if l.speaker else ""
        blocks.append(
            f"{i}\n{fmt_ts(l.start, True)} --> {fmt_ts(end, True)}\n{who}{l.text}\n"
        )
    srt.write_text("\n".join(blocks), encoding="utf-8")
    written["srt"] = srt

    # --- بيانات منظمة للبحث والتحليل لاحقًا
    #
    # كل سطر يحمل النص الخام كما نُطق، **و** صورة مطبّعة بجانبه.
    # الخام للعرض ولا يُمسّ (عاميّة المالك هي المُنتَج). المطبّع للبحث
    # والمطابقة: بدونه «إزاي» لا تجد «ازاي» ويفشل البحث بصمت.
    try:
        from arabic import normalize as _norm
    except ImportError:
        def _norm(t: str) -> str:               # لا تُسقط التفريغ لغياب وحدة
            return t

    js = out_dir / f"{stem}.json"
    js.write_text(
        json.dumps(
            {"meta": meta,
             "lines": [{"start": l.start, "speaker": l.speaker,
                        "text": l.text, "normalized": _norm(l.text)}
                       for l in lines]},
            ensure_ascii=False, indent=2,
        ),
        encoding="utf-8",
    )
    written["json"] = js
    return written


# ---------------------------------------------------------------- التشغيل

def transcribe_file(
    src: Path,
    out_dir: Path,
    provider: str,
    speakers: int,
    hints: str,
    title: str = "",
    keep_audio: bool = False,
) -> dict:
    import datetime as _dt

    ffmpeg = find_binary("ffmpeg")
    ffprobe = find_binary("ffprobe")
    fn = PROVIDERS[provider]

    work = out_dir / ".work" / src.stem
    work.mkdir(parents=True, exist_ok=True)

    src_mb = src.stat().st_size / 1e6
    log(f"\n[1/4] ضغط ذكي للصوت  ({src.name} -- {src_mb:.1f} MB)")
    compact = work / "audio.ogg"
    to_opus(ffmpeg, src, compact)
    duration = probe_duration(ffprobe, compact)
    new_mb = compact.stat().st_size / 1e6
    saved = (1 - new_mb / src_mb) * 100 if src_mb else 0
    log(f"      {src_mb:.1f} MB -> {new_mb:.1f} MB  (توفير {saved:.0f}%)"
        f"  |  المدة {fmt_ts(duration)}")

    log("[2/4] تحديد نقاط القطع عند الصمت")
    silences = detect_silences(ffmpeg, compact)
    chunks = plan_chunks(duration, silences)
    log(f"      {len(chunks)} مقطع (قطع عند صمت، مش عند زمن ثابت)")

    log(f"[3/4] التفريغ عبر: {provider}")
    all_lines: list[Line] = []
    tail = ""
    for i, (start, end) in enumerate(chunks, 1):
        piece = work / f"chunk_{i:03d}.ogg"
        slice_chunk(ffmpeg, compact, piece, start, end)
        log(f"      مقطع {i}/{len(chunks)}"
            f"  [{fmt_ts(start)} -> {fmt_ts(end)}]")

        lines = fn(piece, speakers, tail, hints)
        for l in lines:
            all_lines.append(Line(l.start + start, l.text, l.speaker))
        if lines:
            tail = " ".join(l.text for l in lines[-4:])
        if not keep_audio:
            piece.unlink(missing_ok=True)

    all_lines.sort(key=lambda l: l.start)

    log("[4/4] كتابة الملفات")
    meta = {
        "title": title or src.stem,
        "date": _dt.datetime.now().strftime("%Y-%m-%d %H:%M"),
        "source": src.name,
        "duration": duration,
        "provider": provider,
        "chunks": len(chunks),
        "lines": len(all_lines),
        "words": sum(len(l.text.split()) for l in all_lines),
    }
    written = write_outputs(all_lines, meta, out_dir, src.stem)

    if not keep_audio:
        shutil.rmtree(work, ignore_errors=True)

    log(f"\n      تم: {meta['words']} كلمة في {meta['lines']} سطر")
    for kind, p in written.items():
        log(f"      {kind:5} -> {p}")
    return meta


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(
        description="تفريغ التسجيلات والمكالمات بالعامية المصرية",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    ap.add_argument("inputs", nargs="+", help="ملف صوت/فيديو أو مجلد")
    ap.add_argument("--provider", default=os.environ.get("PROVIDER", "gemini"),
                    choices=sorted(PROVIDERS), help="محرك التفريغ")
    ap.add_argument("--out", default="journal", help="مجلد المخرجات")
    ap.add_argument("--speakers", type=int, default=1,
                    help="عدد المتحدثين (2 للمكالمات) -- 1 للتسجيل الشخصي")
    ap.add_argument("--hints", default=os.environ.get("HINTS", ""),
                    help="أسماء ومصطلحات تُكتب برسم محدد، مفصولة بفاصلة")
    ap.add_argument("--title", default="", help="عنوان وصفي للتسجيل")
    ap.add_argument("--keep-audio", action="store_true",
                    help="لا تحذف المقاطع المؤقتة (للتشخيص)")
    args = ap.parse_args()

    exts = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".flac", ".amr",
            ".3gp", ".mp4", ".mkv", ".webm", ".mov", ".wma", ".oga"}
    targets: list[Path] = []
    for raw in args.inputs:
        p = Path(raw).expanduser()
        if p.is_dir():
            targets += sorted(f for f in p.iterdir()
                              if f.is_file() and f.suffix.lower() in exts)
        elif p.is_file():
            targets.append(p)
        else:
            die(f"غير موجود: {p}")
    if not targets:
        die("لم أجد أي ملف صوت في المسار المحدد.")

    out_dir = Path(args.out).expanduser()
    log(f"عدد الملفات: {len(targets)}  |  المخرجات: {out_dir.resolve()}")

    done, failed = [], []
    for idx, f in enumerate(targets, 1):
        log(f"\n{'=' * 62}\nملف {idx}/{len(targets)}: {f.name}\n{'=' * 62}")
        try:
            done.append(transcribe_file(
                f, out_dir, args.provider, args.speakers,
                args.hints, args.title, args.keep_audio,
            ))
        except SystemExit:
            raise
        except Exception as exc:
            log(f"      فشل هذا الملف: {exc}")
            failed.append((f.name, str(exc)))

    log(f"\n{'=' * 62}\nانتهى: نجح {len(done)} / فشل {len(failed)}")
    for name, err in failed:
        log(f"  - {name}: {err[:120]}")
    if failed and not done:
        sys.exit(1)


if __name__ == "__main__":
    main()
