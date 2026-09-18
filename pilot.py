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

import transcribe as TR
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
    ap.add_argument("--all", action="store_true",
                    help="امشِ على كل المتصلين، من الأصغر للأكبر")
    ap.add_argument("--max-files", type=int, default=0,
                    help="حد أقصى لعدد الملفات في هذه الجلسة (0 = بلا حد)")
    ap.add_argument("--gemini-cap", type=int, default=220,
                    help="سقف طلبات Gemini في هذه الجلسة (الحد المجاني 250/يوم)")
    args = ap.parse_args()

    s3 = boto3.client("s3", region_name=REGION)
    inv = inventory(s3)
    if not inv:
        print(f"لا ملفات صوت تحت s3://{BUCKET}/inbox/")
        return

    if args.list or (not args.caller and not args.all):
        print(f"المتصلون في s3://{BUCKET}/inbox/\n")
        print(f"{'المتصل':<34} {'ملفات':>6} {'الأصغر':>10}")
        print("-" * 54)
        for c in sorted(inv, key=lambda c: sum(o['Size'] for o in inv[c])):
            print(f"{c:<34} {len(inv[c]):>6} {inv[c][0]['Size']/1e6:>8.1f}MB")
        print("\nابدأ بالأصغر لإثبات السلسلة بأسرع وأرخص تجربة:")
        smallest = min(inv, key=lambda c: sum(o['Size'] for o in inv[c]))
        print(f'  python pilot.py --caller "{smallest}"')
        return

    if args.caller and args.caller not in inv:
        print(f"لا يوجد متصل بهذا الاسم. المتاح: {sorted(inv)}")
        sys.exit(1)

    provider = pick_provider(args.provider)

    # عدّاد طلبات حقيقي: يُحسب كل نداء لمزوّد، لا كل ملف. تسجيل واحد يُقطَّع
    # إلى عدة مقاطع فيُرسل طلبًا لكل مقطع، والتقاطع يضاعفها.
    counter = {"gemini": 0, "groq": 0}

    class CapReached(Exception):
        pass

    def hook(prov: str) -> None:
        counter[prov] = counter.get(prov, 0) + 1
        if prov == "gemini" and counter["gemini"] > args.gemini_cap:
            raise SystemExit(
                f"بلغت سقف هذه الجلسة لـ Gemini ({args.gemini_cap} طلب)."
                " الباقي يُستأنف في تشغيل لاحق.")

    TR.REQUEST_HOOK = hook

    callers = ([args.caller] if args.caller
               else sorted(inv, key=lambda c: sum(o['Size'] for o in inv[c])))
    scratch = Path(os.environ.get("KIROCREW_SCRATCH", ".")) / "pilot"
    scratch.mkdir(parents=True, exist_ok=True)

    done: list = []
    skipped = failed = 0
    stop = False

    for caller in callers:
        if stop:
            break
        out_dir = Path("journal") / safe_name(caller, caller)
        out_dir.mkdir(parents=True, exist_ok=True)
        objs = inv[caller] if args.all else inv[caller][:max(1, args.limit)]
        log(f"\n{'#'*62}\n# {caller}  ({len(objs)} ملف)  |  المحرك: {provider}\n{'#'*62}")

        for i, o in enumerate(objs, 1):
            if args.max_files and len(done) >= args.max_files:
                log(f"\nبلغت حد الملفات لهذه الجلسة ({args.max_files}).")
                stop = True
                break

            key = o["Key"]
            stem = safe_name(Path(key).stem, f"rec{i}")
            # الاستئناف: ما فُرّغ سابقًا لا يُعاد -- فالتشغيل المتكرر آمن ومجاني
            if (out_dir / f"{stem}.json").exists():
                skipped += 1
                continue

            local = scratch / f"{stem}{Path(key).suffix.lower()}"
            log(f"\n{'='*62}\n[{i}/{len(objs)}] {Path(key).name}"
                f"  ({o['Size']/1e6:.1f} MB)"
                f"  [طلبات gemini حتى الآن: {counter['gemini']}]\n{'='*62}")
            try:
                s3.download_file(BUCKET, key, str(local))
                meta = transcribe_file(local, out_dir, provider, args.speakers,
                                       "", title=f"{caller} — {stem}")
                done.append((caller, stem, meta))
            except SystemExit as exc:
                msg = str(exc)
                if "سقف هذه الجلسة" in msg:
                    log(f"  {msg}")
                    stop = True
                    break
                failed += 1
                log(f"  فشل: {msg[:300]}")
            except Exception as exc:
                failed += 1
                log(f"  فشل: {type(exc).__name__}: {str(exc)[:300]}")
            finally:
                local.unlink(missing_ok=True)

    TR.REQUEST_HOOK = None

    # الملخصات لكل متصل تأثّر
    touched = sorted({c for c, _, _ in done})
    summaries = []
    for c in touched:
        p = build_summary(c, Path("journal") / safe_name(c, c))
        if p:
            summaries.append(p)

    log(f"\n{'='*62}\nالنتيجة\n{'='*62}")
    log(f"  نجح: {len(done)}   تُخطّي (مفرَّغ سابقًا): {skipped}   فشل: {failed}")
    log(f"  طلبات: gemini={counter['gemini']}  groq={counter['groq']}")
    if done:
        words = sum(m["words"] for _, _, m in done)
        secs = sum(m["duration"] for _, _, m in done)
        log(f"  المفرَّغ: {words:,} كلمة من {secs/3600:.2f} ساعة صوت")
    for p in summaries:
        log(f"  ملخص: {p}")
    if stop:
        log("\n  توقّف عند حد -- أعد التشغيل بنفس الأمر ليستأنف من حيث انتهى.")

    if not done and not skipped:
        log("\nلم ينجح أي ملف. راجع الخطأ أعلاه.")
        sys.exit(1)

    if args.caller and not args.all and args.preview > 0:
        out_dir = Path("journal") / safe_name(args.caller, args.caller)
        md = sorted(out_dir.glob("*.md"))
        if md:
            log(f"\n{'='*62}\nمعاينة\n{'='*62}")
            left = args.preview
            for line in md[0].read_text(encoding="utf-8").splitlines():
                if line.startswith("`["):
                    log("  " + line)
                    left -= 1
                    if left <= 0:
                        break


if __name__ == "__main__":
    main()
