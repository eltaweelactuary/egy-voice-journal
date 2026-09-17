#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
تجربة التفريغ على متصل واحد -- إثبات السلسلة كاملة على بيانات حقيقية.

يقرأ من S3 (تنزيل فقط)، يفرّغ محليًا، ويكتب النتائج **محليًا** في
`journal/<المتصل>/` مع ملخص مجمّع في `summaries/<المتصل>.md`.

لا يرفع شيئًا إلى S3 ولا يحذف منها: سياسة أمان على هذا الجهاز تمنع رفع أي
ملف محلي إلى خدمة خارجية، وهو حدٌّ مقصود لا نتحايل عليه. المخرجات تبقى
محليًا لتقرأها، ورفعها إلى S3 يتم من جهازك أو من خط الأنابيب داخل AWS.

    python pilot.py --list
    python pilot.py --caller "جمال Konecta" --limit 1
    python pilot.py --caller "جمال Konecta" --provider consensus
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from pathlib import Path

import boto3

from transcribe import PROVIDERS, fmt_ts, load_env, log, transcribe_file
from s3_pipeline import AUDIO_EXTS, list_keys, safe_name

BUCKET = os.environ.get("JOURNAL_BUCKET", "egy-voice-journal-075298365868")
REGION = os.environ.get("AWS_REGION", "us-east-1")


def pick_provider(explicit: str) -> str:
    """
    يختار محركًا صالحًا بحسب المفاتيح المتاحة فعلًا.

    التقاطع (`consensus`) هو الأدق لأن محركين مستقلين لا يهلوسان بنفس الشكل،
    فما يتفق عليه يكاد يكون صحيحًا. لكنه يحتاج مفتاحين، فلا نفرضه بلا داعٍ.
    """
    if explicit:
        return explicit
    has_gem = bool(os.environ.get("GEMINI_API_KEY", "").strip())
    has_groq = bool(os.environ.get("GROQ_API_KEY", "").strip())
    if has_gem and has_groq:
        log("مفتاحان متاحان -> أستخدم 'consensus' (محركان يتقاطعان، الأدق)")
        return "consensus"
    if has_gem:
        log("مفتاح Gemini فقط -> أستخدم 'gemini'."
            " أضف GROQ_API_KEY لتفعيل التقاطع.")
        return "gemini"
    if has_groq:
        log("مفتاح Groq فقط -> أستخدم 'groq'."
            " تنبيه: أضعف على العامية ويهلوس أحيانًا في أول التسجيل.")
        return "groq"
    print("\n[خطأ] لا يوجد أي مفتاح تفريغ.\n"
          "  مجاني من: https://aistudio.google.com/apikey"
          " و https://console.groq.com/keys\n"
          "  ثم: setx GEMINI_API_KEY \"...\"   (وافتح طرفية جديدة)\n"
          "  أو ضعهما في ملف .env بجانب هذا السكربت.", file=sys.stderr)
    sys.exit(2)


def inventory(s3) -> dict[str, list]:
    out: dict[str, list] = defaultdict(list)
    for o in list_keys(s3, BUCKET, "inbox/"):
        parts = o["Key"].split("/")
        if len(parts) >= 3 and o["Size"] > 0 \
                and Path(o["Key"]).suffix.lower() in AUDIO_EXTS:
            out[parts[1]].append(o)
    for v in out.values():
        v.sort(key=lambda o: o["Size"])          # الأصغر أولًا: تجربة أسرع وأرخص
    return out


def build_summary(caller: str, out_dir: Path) -> Path | None:
    """يجمع كل تفريغات المتصل في مستند واحد مرتّب -- هذا ما تُغذّي به أي LLM."""
    import json
    files = sorted(out_dir.glob("*.json"))
    if not files:
        return None
    metas = []
    for f in files:
        try:
            metas.append(json.loads(f.read_text(encoding="utf-8")))
        except Exception:
            continue
    metas.sort(key=lambda d: str(d.get("meta", {}).get("date", "")))

    total_sec = sum(float(d["meta"].get("duration", 0) or 0) for d in metas)
    total_words = sum(int(d["meta"].get("words", 0) or 0) for d in metas)
    parts = [
        f"# أرشيف المكالمات — {caller}", "",
        f"- عدد التسجيلات: {len(metas)}",
        f"- إجمالي المدة: {total_sec/3600:.2f} ساعة",
        f"- إجمالي الكلمات: {total_words:,}", "",
        "> مُجمَّع آليًا، من الأقدم للأحدث. مُعدّ ليُقرأ كاملًا في سياق واحد.",
        "",
    ]
    for d in metas:
        m = d.get("meta", {})
        parts += ["---", "",
                  f"## {m.get('title','')} — {m.get('date','')}"
                  f"  ({fmt_ts(float(m.get('duration',0) or 0))})", ""]
        for l in d.get("lines", []):
            t = (l.get("text") or "").strip()
            if not t:
                continue
            who = f"**{l['speaker']}:** " if l.get("speaker") else ""
            parts.append(f"`[{fmt_ts(float(l.get('start',0)))}]` {who}{t}")
        parts.append("")

    dest = Path("summaries") / f"{safe_name(caller, caller)}.md"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text("\n".join(parts) + "\n", encoding="utf-8")
    return dest


def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(description="تجربة التفريغ على متصل واحد")
    ap.add_argument("--caller", default="", help="اسم المتصل كما في inbox/")
    ap.add_argument("--provider", default="", choices=[""] + sorted(PROVIDERS))
    ap.add_argument("--limit", type=int, default=1, help="عدد الملفات (افتراضي 1)")
    ap.add_argument("--speakers", type=int, default=2)
    ap.add_argument("--list", action="store_true", help="اعرض المتصلين واخرج")
    ap.add_argument("--preview", type=int, default=14, help="أسطر المعاينة")
    args = ap.parse_args()

    s3 = boto3.client("s3", region_name=REGION)
    inv = inventory(s3)
    if not inv:
        print(f"لا ملفات صوت تحت s3://{BUCKET}/inbox/")
        return

    if args.list or not args.caller:
        print(f"المتصلون في s3://{BUCKET}/inbox/\n")
        print(f"{'المتصل':<34} {'ملفات':>6} {'الأصغر':>10}")
        print("-" * 54)
        for c in sorted(inv, key=lambda c: sum(o['Size'] for o in inv[c])):
            print(f"{c:<34} {len(inv[c]):>6} {inv[c][0]['Size']/1e6:>8.1f}MB")
        print("\nابدأ بالأصغر لإثبات السلسلة بأسرع وأرخص تجربة:")
        smallest = min(inv, key=lambda c: sum(o['Size'] for o in inv[c]))
        print(f'  python pilot.py --caller "{smallest}"')
        return

    if args.caller not in inv:
        print(f"لا يوجد متصل بهذا الاسم. المتاح: {sorted(inv)}")
        sys.exit(1)

    provider = pick_provider(args.provider)
    objs = inv[args.caller][:max(1, args.limit)]
    out_dir = Path("journal") / safe_name(args.caller, args.caller)
    out_dir.mkdir(parents=True, exist_ok=True)
    scratch = Path(os.environ.get("KIROCREW_SCRATCH", ".")) / "pilot"
    scratch.mkdir(parents=True, exist_ok=True)

    log(f"\nالمتصل: {args.caller}  |  المحرك: {provider}"
        f"  |  ملفات: {len(objs)}\n")

    done = []
    for i, o in enumerate(objs, 1):
        key = o["Key"]
        stem = safe_name(Path(key).stem, f"rec{i}")
        local = scratch / f"{stem}{Path(key).suffix.lower()}"
        log(f"{'='*62}\n[{i}/{len(objs)}] {Path(key).name}"
            f"  ({o['Size']/1e6:.1f} MB)\n{'='*62}")
        try:
            s3.download_file(BUCKET, key, str(local))
            meta = transcribe_file(local, out_dir, provider, args.speakers,
                                   "", title=f"{args.caller} — {stem}")
            done.append((stem, meta))
        except SystemExit as exc:
            log(f"  فشل: {str(exc)[:400]}")
        except Exception as exc:
            log(f"  فشل: {type(exc).__name__}: {str(exc)[:300]}")
        finally:
            local.unlink(missing_ok=True)

    if not done:
        log("\nلم ينجح أي ملف. راجع الخطأ أعلاه.")
        sys.exit(1)

    summary = build_summary(args.caller, out_dir)

    log(f"\n{'='*62}\nالنتيجة\n{'='*62}")
    for stem, meta in done:
        log(f"  {stem}: {meta['words']} كلمة، {meta['lines']} سطر،"
            f" {fmt_ts(meta['duration'])}، محرك {meta['provider']}")
    log(f"\n  المخرجات: {out_dir.resolve()}")
    if summary:
        log(f"  الملخص المجمّع: {summary.resolve()}")

    md = sorted(out_dir.glob("*.md"))
    if md and args.preview > 0:
        log(f"\n{'='*62}\nمعاينة — احكم بنفسك على الدقة\n{'='*62}")
        for line in md[0].read_text(encoding="utf-8").splitlines():
            if line.startswith("`["):
                log("  " + line)
                args.preview -= 1
                if args.preview <= 0:
                    break


if __name__ == "__main__":
    main()
