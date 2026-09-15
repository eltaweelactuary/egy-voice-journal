#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ناقش تسجيلاتك المفرّغة -- اسأل أرشيفك الصوتي واستخرج منه أنماطًا.

    python ask.py "إيه الحاجات اللي بتقلقني بشكل متكرر؟"
    python ask.py --interactive
    python ask.py --since 2026-01-01 "إيه اللي اتغير في تفكيري السنة دي؟"

يعتمد على نافذة سياق Gemini الكبيرة: بدل قواعد بيانات متجهية معقّدة،
نمرّر الأرشيف كله (أو أقرب أجزائه للسؤال) في طلب واحد.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from transcribe import load_env, die, log, _post_json  # noqa: E402

# نافذة Gemini مليون توكن. نحتفظ بهامش أمان كبير.
MAX_CORPUS_CHARS = 1_200_000

SYSTEM = """أنت رفيق تفكير يساعد صاحب هذه التسجيلات على فهم نفسه.

المصدر الوحيد للحقيقة هو مقاطع التفريغ المرفقة -- تسجيلات صوتية شخصية
سجّلها بنفسه بالعامية المصرية.

كيف تعمل:
- ابنِ كل ملاحظة على ما قاله فعلًا. اقتبس كلامه بالعامية كما نطقه،
  واذكر تاريخ التسجيل ووقته بين قوسين بعد كل اقتباس.
- لو السؤال يتعلق بشيء غير موجود في التسجيلات، قل ذلك بصراحة.
  لا تُكمل الفراغ بتخمين ولا بكلام عام.
- ميّز بوضوح بين: "قال كذا" (نقل) و"يبدو أن" (استنتاج). سمِّ الاستنتاج استنتاجًا.
- ابحث عن الأنماط المتكررة والتحولات عبر الزمن -- ده أنفع شيء يقدّمه أرشيف
  زي ده. اربط تسجيلًا بتسجيل. لاحظ ما تكرر، وما اختفى، وما تغيّر.
- لاحظ التناقضات بلطف وبدون حكم: ما قاله مرة وخالفه مرة.
- تكلّم معه بالعامية المصرية، بشكل مباشر ودافئ وبدون مجاملة فارغة.
  الصدق أنفع له من الطبطبة.

حدود مهمة:
- أنت لست طبيبًا نفسيًا ولا معالجًا. لا تُشخّص ولا تسمّي حالات مرضية
  ولا تصف علاجًا أو دواء.
- لو ظهر في كلامه ما يوحي بخطر حقيقي على نفسه، اترك التحليل وقل له
  بوضوح وهدوء إن ده يستاهل إنه يكلّم متخصص أو حد يثق فيه قريب منه.
"""


def load_corpus(journal: Path, since: str = "", until: str = "") -> list[dict]:
    """يقرأ كل التفريغات المتاحة مع بياناتها."""
    if not journal.exists():
        die(f"مجلد الأرشيف غير موجود: {journal}\nفرّغ تسجيلًا أولًا بـ transcribe.py")

    docs: list[dict] = []
    for js in sorted(journal.glob("*.json")):
        try:
            data = json.loads(js.read_text(encoding="utf-8"))
        except Exception:
            continue
        meta = data.get("meta", {})
        date = str(meta.get("date", ""))[:10]
        if since and date and date < since:
            continue
        if until and date and date > until:
            continue
        text = "\n".join(
            (f"{l.get('speaker')}: {l['text']}" if l.get("speaker") else l["text"])
            for l in data.get("lines", []) if l.get("text")
        )
        if text.strip():
            docs.append({
                "title": meta.get("title") or js.stem,
                "date": meta.get("date", ""),
                "duration": meta.get("duration", 0),
                "text": text,
                "chars": len(text),
            })

    # أي .md أو .txt أضافه المستخدم يدويًا بدون json مصاحب
    seen = {d["title"] for d in docs}
    for extra in sorted(list(journal.glob("*.txt")) + list(journal.glob("*.md"))):
        if extra.stem in seen:
            continue
        body = extra.read_text(encoding="utf-8", errors="replace").strip()
        if body:
            docs.append({"title": extra.stem, "date": "", "duration": 0,
                         "text": body, "chars": len(body)})
            seen.add(extra.stem)
    return docs


def select(docs: list[dict], question: str) -> tuple[list[dict], bool]:
    """
    لو الأرشيف أكبر من النافذة، نرتّب بالصلة بالسؤال ثم بالأحدث.
    """
    total = sum(d["chars"] for d in docs)
    if total <= MAX_CORPUS_CHARS:
        return docs, False

    words = {w for w in re.findall(r"[\w\u0600-\u06FF]{3,}", question)}
    for d in docs:
        hits = sum(d["text"].count(w) for w in words)
        d["_score"] = hits / max(d["chars"] / 5000, 1)
    ranked = sorted(docs, key=lambda d: (-d.get("_score", 0), d.get("date", "")),
                    reverse=False)

    picked, budget = [], 0
    for d in ranked:
        if budget + d["chars"] > MAX_CORPUS_CHARS:
            continue
        picked.append(d)
        budget += d["chars"]
    picked.sort(key=lambda d: d.get("date", ""))
    return picked, True


def build_corpus_text(docs: list[dict]) -> str:
    out = []
    for d in docs:
        out.append(
            f"\n\n===== تسجيل: {d['title']}"
            f" | التاريخ: {d['date'] or 'غير معروف'} =====\n{d['text']}"
        )
    return "".join(out)


def ask(docs: list[dict], question: str, history: list[dict] | None = None) -> str:
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        die("GEMINI_API_KEY غير موجود. مفتاح مجاني من https://aistudio.google.com/apikey")

    model = os.environ.get("GEMINI_CHAT_MODEL", "gemini-2.5-flash").strip()
    picked, trimmed = select(docs, question)
    if trimmed:
        log(f"  (الأرشيف كبير -- استخدمت {len(picked)} من {len(docs)} تسجيل"
            f" الأقرب للسؤال)")

    contents = []
    contents.append({"role": "user", "parts": [{"text":
        "هذه تسجيلاتي الصوتية الشخصية مفرّغة نصًا. اعتمد عليها في كل إجاباتك:\n"
        + build_corpus_text(picked)}]})
    contents.append({"role": "model", "parts": [{"text":
        "قرأت الأرشيف. اسأل."}]})
    for turn in (history or []):
        contents.append(turn)
    contents.append({"role": "user", "parts": [{"text": question}]})

    data = _post_json(
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        {
            "systemInstruction": {"parts": [{"text": SYSTEM}]},
            "contents": contents,
            "generationConfig": {"temperature": 0.4, "maxOutputTokens": 16384},
        },
        {"x-goog-api-key": key, "Content-Type": "application/json"},
    )
    try:
        parts = data["candidates"][0]["content"]["parts"]
        return "".join(p.get("text", "") for p in parts).strip()
    except (KeyError, IndexError):
        return f"[لم أستطع قراءة رد الموديل]\n{json.dumps(data, ensure_ascii=False)[:600]}"


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(description="ناقش أرشيف تسجيلاتك")
    ap.add_argument("question", nargs="*", help="سؤالك")
    ap.add_argument("--journal", default="journal", help="مجلد التفريغات")
    ap.add_argument("--since", default="", help="من تاريخ YYYY-MM-DD")
    ap.add_argument("--until", default="", help="إلى تاريخ YYYY-MM-DD")
    ap.add_argument("--interactive", "-i", action="store_true", help="محادثة مستمرة")
    ap.add_argument("--save", default="", help="احفظ الرد في ملف")
    args = ap.parse_args()

    docs = load_corpus(Path(args.journal).expanduser(), args.since, args.until)
    if not docs:
        die("الأرشيف فاضي. فرّغ تسجيلًا أولًا.")

    total_words = sum(len(d["text"].split()) for d in docs)
    total_min = sum(d["duration"] for d in docs) / 60
    log(f"الأرشيف: {len(docs)} تسجيل | {total_words:,} كلمة"
        f" | {total_min:.0f} دقيقة صوت")

    if args.interactive:
        log("\nاكتب سؤالك. اكتب 'خروج' للإنهاء.\n")
        history: list[dict] = []
        while True:
            try:
                q = input("أنت: ").strip()
            except (EOFError, KeyboardInterrupt):
                break
            if not q:
                continue
            if q in {"خروج", "exit", "quit", "q"}:
                break
            answer = ask(docs, q, history)
            print(f"\n{answer}\n")
            history += [
                {"role": "user", "parts": [{"text": q}]},
                {"role": "model", "parts": [{"text": answer}]},
            ]
            history = history[-12:]
        return

    if not args.question:
        die("اكتب سؤالك، أو استخدم --interactive")

    answer = ask(docs, " ".join(args.question))
    print("\n" + answer + "\n")

    if args.save:
        p = Path(args.save)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(
            f"# {' '.join(args.question)}\n\n"
            f"_{dt.datetime.now():%Y-%m-%d %H:%M} -- "
            f"{len(docs)} تسجيل، {total_words:,} كلمة_\n\n{answer}\n",
            encoding="utf-8",
        )
        log(f"محفوظ في: {p}")


if __name__ == "__main__":
    main()
