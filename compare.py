#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
قارن محركات التفريغ على تسجيلك أنت -- لا على تسجيلات البنشمارك.

أرقام WER المنشورة تُقاس على بودكاست ومكالمات ليست صوتك، ولا ميكروفونك،
ولا ضجيج غرفتك، ولا طريقة نطقك. الطريقة الوحيدة الجدّية لمعرفة "الأدق
للمصري" في حالتك هي تشغيل المحركات على عيّنة من تسجيلك ومقارنتها بعينك.

    python compare.py sample.m4a
    python compare.py sample.m4a --minutes 3 --providers qwencleo,gemini,elevenlabs

المخرج: ملف Markdown فيه المقاطع جنبًا إلى جنب + الزمن المستهلك لكل محرك.
"""

from __future__ import annotations

import argparse
import time
import traceback
from pathlib import Path

from transcribe import (
    PROVIDERS, Line, find_binary, fmt_ts, load_env, log,
    probe_duration, slice_chunk, to_opus,
)

# تقدير تكلفة إرشادي فقط -- الأسعار تتغير، راجعها عند المزوّد.
COST_HINT = {
    "qwencleo": "مجاني (موديل مفتوح، يعمل عندك)",
    "local": "مجاني (Whisper مفتوح، يعمل عندك)",
    "gemini": "مجاني في الطبقة المجانية",
    "groq": "مجاني في الطبقة المجانية",
    "elevenlabs": "مدفوع — الطبقة المجانية دقائق قليلة شهريًا",
}


def run_one(name: str, audio: Path, speakers: int, hints: str) -> dict:
    fn = PROVIDERS[name]
    t0 = time.time()
    try:
        lines = fn(audio, speakers, "", hints)
        return {
            "name": name,
            "ok": True,
            "seconds": time.time() - t0,
            "lines": lines,
            "words": sum(len(l.text.split()) for l in lines),
            "error": "",
        }
    except SystemExit as exc:                 # die() داخل المزوّد
        return {"name": name, "ok": False, "seconds": time.time() - t0,
                "lines": [], "words": 0,
                "error": str(exc) or "المحرك غير مهيأ (مفتاح ناقص أو حزمة غير مثبتة)"}
    except Exception:
        return {"name": name, "ok": False, "seconds": time.time() - t0,
                "lines": [], "words": 0,
                "error": traceback.format_exc(limit=2).strip().splitlines()[-1]}


def bucket(lines: list[Line], step: float, n: int) -> list[str]:
    """يوزّع الأسطر على فترات متساوية حتى تتقابل المحركات صفًّا بصف."""
    slots: list[list[str]] = [[] for _ in range(n)]
    for l in lines:
        i = min(int(l.start // step), n - 1)
        slots[i].append(l.text)
    return [" ".join(s).strip() for s in slots]


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(description="قارن محركات التفريغ على تسجيلك")
    ap.add_argument("audio", help="ملف صوت للعيّنة")
    ap.add_argument("--minutes", type=float, default=3.0,
                    help="طول العيّنة بالدقائق (افتراضي 3 -- يكفي للحكم ويوفّر)")
    ap.add_argument("--providers", default="qwencleo,gemini,groq",
                    help="المحركات مفصولة بفاصلة")
    ap.add_argument("--speakers", type=int, default=1)
    ap.add_argument("--hints", default="")
    ap.add_argument("--out", default="comparison.md")
    args = ap.parse_args()

    names = [p.strip() for p in args.providers.split(",") if p.strip()]
    unknown = [n for n in names if n not in PROVIDERS]
    if unknown:
        log(f"محركات غير معروفة: {unknown}\nالمتاح: {sorted(PROVIDERS)}")
        return

    ffmpeg, ffprobe = find_binary("ffmpeg"), find_binary("ffprobe")
    src = Path(args.audio).expanduser()
    if not src.exists():
        log(f"غير موجود: {src}")
        return

    work = Path("./.compare")
    work.mkdir(exist_ok=True)

    log("تجهيز العيّنة...")
    full = work / "full.ogg"
    to_opus(ffmpeg, src, full)
    total = probe_duration(ffprobe, full)
    span = min(args.minutes * 60, total)

    sample = work / "sample.ogg"
    slice_chunk(ffmpeg, full, sample, 0.0, span)
    log(f"العيّنة: {fmt_ts(span)} من أصل {fmt_ts(total)}"
        f"  ({sample.stat().st_size/1e6:.2f} MB)\n")

    results = []
    for n in names:
        log(f"تشغيل {n} ...")
        r = run_one(n, sample, args.speakers, args.hints)
        results.append(r)
        if r["ok"]:
            log(f"   تم في {r['seconds']:.0f}s — {r['words']} كلمة،"
                f" {len(r['lines'])} سطر")
        else:
            log(f"   فشل: {r['error'][:150]}")

    working = [r for r in results if r["ok"] and r["lines"]]

    # جدول صفًّا بصف كل 20 ثانية
    step = 20.0
    n_slots = max(1, int(span // step) + (1 if span % step else 0))
    columns = {r["name"]: bucket(r["lines"], step, n_slots) for r in working}

    out = [
        "# مقارنة محركات التفريغ على تسجيلك",
        "",
        f"- الملف: `{src.name}`",
        f"- العيّنة: {fmt_ts(span)} من أصل {fmt_ts(total)}",
        f"- عدد المتحدثين المفترض: {args.speakers}",
        "",
        "## الملخص",
        "",
        "| المحرك | الحالة | الزمن | كلمات | التكلفة |",
        "|---|---|---|---|---|",
    ]
    for r in results:
        state = "نجح" if r["ok"] and r["lines"] else "فشل"
        out.append(
            f"| `{r['name']}` | {state} | {r['seconds']:.0f}s | {r['words']} "
            f"| {COST_HINT.get(r['name'], '—')} |"
        )
    for r in results:
        if not (r["ok"] and r["lines"]):
            out += ["", f"> `{r['name']}` — {r['error'][:300]}"]

    if len(working) >= 2:
        out += [
            "",
            "## المقارنة صفًّا بصف",
            "",
            "**اقرأ بعينك وحكم:** الأدق هو الذي يحفظ عاميّتك كما نطقتها،"
            " ولا يحوّلها لفصحى، ولا يخترع كلامًا لم تقله.",
            "",
            "| الوقت | " + " | ".join(f"`{n}`" for n in columns) + " |",
            "|---" * (len(columns) + 1) + "|",
        ]
        for i in range(n_slots):
            cells = [(columns[n][i] or "—").replace("|", "/") for n in columns]
            out.append(f"| {fmt_ts(i*step)} | " + " | ".join(cells) + " |")

    out += [
        "",
        "## النص الكامل لكل محرك",
        "",
    ]
    for r in working:
        out += [f"### `{r['name']}`", "",
                " ".join(l.text for l in r["lines"]), ""]

    dest = Path(args.out)
    dest.write_text("\n".join(out) + "\n", encoding="utf-8")
    log(f"\nالمقارنة جاهزة: {dest.resolve()}")
    if len(working) < 2:
        log("تحذير: نجح محرك واحد أو لا شيء — راجع المفاتيح والحزم أعلاه.")


if __name__ == "__main__":
    main()
