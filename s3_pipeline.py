#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
أتمتة كاملة على S3 -- ترمي التسجيل وتنساه.

    s3://<bucket>/inbox/<اسم المتصل>/*.m4a      <- ترفع هنا
    s3://<bucket>/journal/<اسم المتصل>/*.md|json <- التفريغ
    s3://<bucket>/summaries/<اسم المتصل>.md      <- ملخص مجمّع لتغذية LLM
    s3://<bucket>/processed/<...>                <- الصوت الخام بعد المعالجة

الاستخدام:
    python s3_pipeline.py setup                 # ينشئ الـ bucket خاصًا ومشفّرًا
    python s3_pipeline.py run                   # يعالج كل ما في inbox
    python s3_pipeline.py upload "C:\\rec.m4a" --caller "جمال Konecta"
    python s3_pipeline.py summaries             # يعيد بناء الملخصات فقط

الحاوية خاصة تمامًا: حجب كل وصول عام، تشفير افتراضي، ولا سياسة قراءة عامة.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

from transcribe import Line, fmt_ts, load_env, log, write_outputs

DEFAULT_PREFIXES = ("inbox/", "journal/", "summaries/", "processed/")
AUDIO_EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".flac",
              ".amr", ".3gp", ".mp4", ".mkv", ".webm", ".mov", ".wma", ".oga"}
# مدة بقاء الصوت الخام قبل حذفه تلقائيًا -- التفريغ يبقى للأبد، الصوت لا
RAW_AUDIO_TTL_DAYS = 30


def die(msg: str) -> None:
    print(f"\n[خطأ] {msg}", file=sys.stderr, flush=True)
    sys.exit(1)


# ---------------------------------------------------------------- boto3

def s3_client(profile: str = "", region: str = ""):
    try:
        import boto3
        from botocore.exceptions import NoCredentialsError, ProfileNotFound
    except ImportError:
        die("هذا السكربت يحتاج boto3:\n  pip install boto3")
        raise SystemExit(1)

    try:
        session = (boto3.Session(profile_name=profile) if profile
                   else boto3.Session())
        return session.client("s3", region_name=region or None), session
    except ProfileNotFound:
        die(f"البروفايل '{profile}' غير موجود. اسرد المتاح بـ:\n"
            "  aws configure list-profiles")
    except NoCredentialsError:
        die("لا توجد بيانات اعتماد AWS. نفّذ في طرفيتك:\n"
            "  aws configure   أو   aws sso login")
    raise SystemExit(1)


def account_id(session) -> str:
    try:
        return session.client("sts").get_caller_identity()["Account"]
    except Exception as exc:
        die(f"تعذّر تحديد هوية حساب AWS: {exc}\n"
            "تحقّق بنفسك بـ:  aws sts get-caller-identity")
    return ""


# ---------------------------------------------------------------- أسماء آمنة

def safe_name(text: str, fallback: str = "unnamed") -> str:
    """
    يحوّل اسمًا فيه إيموجي/محارف غريبة إلى اسم صالح لنظام الملفات.

    أسماء تسجيلات الموبايل الحقيقية تحتوي إيموجي وأرقامًا عربية-هندية
    ومحارف اتجاه، وقد رُصد ذلك فعلًا (مثال: 'تسجيل المكالمة Ko🧜‍♀️kվ_٢٦٠٥٢١').
    نُبقي العربي واللاتيني والأرقام فقط.
    """
    text = unicodedata.normalize("NFKC", text)
    # أزل الإيموجي ومحارف التحكم والاتجاه
    text = "".join(ch for ch in text
                   if unicodedata.category(ch)[0] not in ("C", "S", "M")
                   or ch in " _-")
    text = re.sub(r"[^\w\u0600-\u06FF \-.]", "", text, flags=re.UNICODE)
    text = re.sub(r"\s+", " ", text).strip(" .-_")
    return text or fallback


def caller_of(key: str) -> str:
    """
    يستخرج اسم المتصل. الأولوية للمجلد -- أدقّ بكثير من تحليل اسم الملف.

    inbox/جمال Konecta/rec.m4a  ->  "جمال Konecta"
    inbox/rec.m4a               ->  محاولة من اسم الملف، وإلا "غير مصنّف"
    """
    parts = [p for p in key.split("/") if p]
    if len(parts) >= 3:                     # inbox/<caller>/<file>
        return safe_name(parts[1], "غير مصنف")

    stem = Path(parts[-1]).stem if parts else ""
    stem = safe_name(stem, "")
    # أسقط البادئات الشائعة والطوابع الزمنية، والباقي غالبًا الاسم
    stem = re.sub(r"^(تسجيل\s*المكالمة|تسجيل|call|recording|rec)\s*", "",
                  stem, flags=re.IGNORECASE)
    stem = re.sub(r"[\d\u0660-\u0669_\-\s]+$", "", stem).strip()
    return stem or "غير مصنف"


# ---------------------------------------------------------------- الحاوية

def ensure_bucket(s3, session, bucket: str, region: str) -> None:
    """ينشئ الحاوية خاصة ومشفّرة، ويضيف قاعدة حذف الصوت الخام. Idempotent."""
    from botocore.exceptions import ClientError

    try:
        s3.head_bucket(Bucket=bucket)
        log(f"الحاوية موجودة: {bucket}")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in ("404", "NoSuchBucket", "403"):
            die(f"تعذّر فحص الحاوية: {exc}")
        if code == "403":
            die(f"الحاوية '{bucket}' موجودة لكنها مملوكة لحساب آخر."
                " اختر اسمًا مختلفًا بـ --bucket.")
        log(f"إنشاء الحاوية: {bucket}  (المنطقة {region})")
        kwargs = {"Bucket": bucket}
        if region and region != "us-east-1":
            kwargs["CreateBucketConfiguration"] = {"LocationConstraint": region}
        s3.create_bucket(**kwargs)

    # 1) حجب كل وصول عام -- هذه تسجيلات شخصية
    s3.put_public_access_block(
        Bucket=bucket,
        PublicAccessBlockConfiguration={
            "BlockPublicAcls": True, "IgnorePublicAcls": True,
            "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
        },
    )
    log("  حجب الوصول العام: مُفعَّل")

    # 2) تشفير افتراضي
    #
    # لا نضع BucketKeyEnabled مع AES256: مفاتيح الحاوية (S3 Bucket Keys) خاصية
    # من خصائص SSE-KMS لتقليل نداءات KMS، ولا معنى لها مع SSE-S3 -- وقد نبّه
    # مراجع مستقل إلى أن الجمع بينهما قد يُفشل النشر أصلًا.
    s3.put_bucket_encryption(
        Bucket=bucket,
        ServerSideEncryptionConfiguration={"Rules": [{
            "ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"},
        }]},
    )
    log("  التشفير الافتراضي: مُفعَّل (SSE-S3)")

    # 2b) رفض أي وصول غير مشفّر النقل -- تسجيلات شخصية لا تُنقل على HTTP
    import json as _json
    s3.put_bucket_policy(Bucket=bucket, Policy=_json.dumps({
        "Version": "2012-10-17",
        "Statement": [{
            "Sid": "DenyInsecureTransport",
            "Effect": "Deny",
            "Principal": "*",
            "Action": "s3:*",
            "Resource": [f"arn:aws:s3:::{bucket}", f"arn:aws:s3:::{bucket}/*"],
            "Condition": {"Bool": {"aws:SecureTransport": "false"}},
        }],
    }))
    log("  رفض النقل غير المشفّر: مُفعَّل")

    # 3) الصوت الخام يُحذف بعد مدة -- التفريغ يبقى. يحدّ التكلفة تلقائيًا.
    s3.put_bucket_lifecycle_configuration(
        Bucket=bucket,
        LifecycleConfiguration={"Rules": [{
            "ID": "expire-raw-audio",
            "Status": "Enabled",
            "Filter": {"Prefix": "processed/"},
            "Expiration": {"Days": RAW_AUDIO_TTL_DAYS},
        }, {
            "ID": "abort-incomplete-uploads",
            "Status": "Enabled",
            "Filter": {"Prefix": ""},
            "AbortIncompleteMultipartUpload": {"DaysAfterInitiation": 7},
        }]},
    )
    log(f"  حذف الصوت الخام بعد {RAW_AUDIO_TTL_DAYS} يومًا: مُفعَّل")
    log("  (التفريغ والملخصات لا تُحذف)")


# ---------------------------------------------------------------- عمليات

def list_keys(s3, bucket: str, prefix: str) -> list[dict]:
    out, token = [], None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        resp = s3.list_objects_v2(**kw)
        out += resp.get("Contents", []) or []
        if not resp.get("IsTruncated"):
            return out
        token = resp.get("NextContinuationToken")


def rebuild_summary(s3, bucket: str, caller: str) -> int:
    """
    يجمع كل تفريغات متصل واحد في ملف واحد مرتّب زمنيًا.

    هذا هو الملف الذي تُغذّي به أي LLM: سياق كامل عن شخص واحد في مستند واحد،
    بدل عشرات الملفات المتفرقة.
    """
    prefix = f"journal/{caller}/"
    metas = []
    for obj in list_keys(s3, bucket, prefix):
        if not obj["Key"].endswith(".json"):
            continue
        body = s3.get_object(Bucket=bucket, Key=obj["Key"])["Body"].read()
        try:
            data = json.loads(body.decode("utf-8"))
        except Exception:
            continue
        metas.append(data)

    if not metas:
        return 0

    metas.sort(key=lambda d: str(d.get("meta", {}).get("date", "")))
    total_sec = sum(float(d["meta"].get("duration", 0) or 0) for d in metas)
    total_words = sum(int(d["meta"].get("words", 0) or 0) for d in metas)

    parts = [
        f"# أرشيف المكالمات — {caller}",
        "",
        f"- عدد التسجيلات: {len(metas)}",
        f"- إجمالي المدة: {total_sec/3600:.1f} ساعة",
        f"- إجمالي الكلمات: {total_words:,}",
        "",
        "> ملف مجمّع آليًا من كل تفريغات هذا المتصل، مرتّبًا من الأقدم للأحدث.",
        "> مُعدّ ليُقرأ كاملًا في سياق واحد.",
        "",
    ]
    for d in metas:
        m = d.get("meta", {})
        parts += [
            "---",
            "",
            f"## {m.get('title', '')} — {m.get('date', '')}"
            f"  ({fmt_ts(float(m.get('duration', 0) or 0))})",
            "",
        ]
        for l in d.get("lines", []):
            text = (l.get("text") or "").strip()
            if not text:
                continue
            who = f"**{l['speaker']}:** " if l.get("speaker") else ""
            parts.append(f"`[{fmt_ts(float(l.get('start', 0)))}]` {who}{text}")
        parts.append("")

    key = f"summaries/{caller}.md"
    s3.put_object(Bucket=bucket, Key=key,
                  Body=("\n".join(parts) + "\n").encode("utf-8"),
                  ContentType="text/markdown; charset=utf-8")
    log(f"  ملخص: s3://{bucket}/{key}  ({len(metas)} تسجيل)")
    return len(metas)


def process_inbox(s3, bucket: str, provider: str, speakers: int,
                  hints: str, workdir: Path) -> tuple[int, int, set]:
    from transcribe import transcribe_file

    objs = [o for o in list_keys(s3, bucket, "inbox/")
            if Path(o["Key"]).suffix.lower() in AUDIO_EXTS and o["Size"] > 0]
    if not objs:
        log("لا يوجد شيء جديد في inbox/")
        return 0, 0, set()

    log(f"وجدت {len(objs)} تسجيلًا في inbox/\n")
    done, failed, touched = 0, 0, set()

    for i, obj in enumerate(objs, 1):
        key = obj["Key"]
        caller = caller_of(key)
        stem = safe_name(Path(key).stem, f"rec{i}")
        log(f"{'='*62}\n[{i}/{len(objs)}] {key}\n   المتصل: {caller}\n{'='*62}")

        # تخطَّ ما فُرّغ سابقًا -- يجعل التشغيل المجدول آمنًا للتكرار
        already = [o["Key"] for o in list_keys(s3, bucket, f"journal/{caller}/{stem}.json")]
        if already:
            log("   مُفرَّغ سابقًا -- تخطٍّ")
            touched.add(caller)
            continue

        local_dir = workdir / caller
        local_dir.mkdir(parents=True, exist_ok=True)
        local_audio = local_dir / f"{stem}{Path(key).suffix.lower()}"

        try:
            log(f"   تنزيل ({obj['Size']/1e6:.1f} MB)...")
            s3.download_file(bucket, key, str(local_audio))

            out_dir = local_dir / "out"
            transcribe_file(local_audio, out_dir, provider, speakers,
                            hints, title=f"{caller} — {stem}")

            uploaded = 0
            for f in sorted(out_dir.glob(f"{stem}.*")):
                ctype = ("text/markdown; charset=utf-8" if f.suffix == ".md"
                         else "application/json; charset=utf-8" if f.suffix == ".json"
                         else "text/plain; charset=utf-8")
                s3.upload_file(str(f), bucket, f"journal/{caller}/{f.name}",
                               ExtraArgs={"ContentType": ctype})
                uploaded += 1
            log(f"   رُفِع {uploaded} ملف إلى journal/{caller}/")

            # انقل الصوت الخام بدل حذفه فورًا -- يسمح بإعادة التفريغ لو الجودة سيئة
            s3.copy_object(Bucket=bucket, Key=f"processed/{caller}/{local_audio.name}",
                           CopySource={"Bucket": bucket, "Key": key})
            s3.delete_object(Bucket=bucket, Key=key)
            log(f"   نُقل الصوت إلى processed/ (يُحذف تلقائيًا بعد {RAW_AUDIO_TTL_DAYS} يومًا)")

            touched.add(caller)
            done += 1
        except SystemExit as exc:
            failed += 1
            log(f"   فشل: {str(exc)[:300]}")
            log("   الصوت باقٍ في inbox/ -- سيُعاد في التشغيل القادم")
        except Exception as exc:
            failed += 1
            log(f"   فشل: {type(exc).__name__}: {str(exc)[:300]}")
            log("   الصوت باقٍ في inbox/ -- سيُعاد في التشغيل القادم")
        finally:
            for f in local_dir.rglob("*"):
                if f.is_file():
                    try:
                        f.unlink()
                    except OSError:
                        pass

    return done, failed, touched


# ---------------------------------------------------------------- CLI

def main() -> None:
    load_env()
    ap = argparse.ArgumentParser(description="أتمتة أرشيف الصوت على S3")
    ap.add_argument("command", choices=["setup", "run", "summaries", "upload"])
    ap.add_argument("files", nargs="*", help="ملفات للرفع (مع upload)")
    ap.add_argument("--bucket", default=os.environ.get("JOURNAL_BUCKET", ""))
    ap.add_argument("--profile", default=os.environ.get("AWS_PROFILE", ""))
    ap.add_argument("--region", default=os.environ.get("AWS_REGION", "us-east-1"))
    ap.add_argument("--caller", default="", help="اسم المتصل (مع upload)")
    ap.add_argument("--provider", default=os.environ.get("PROVIDER", "gemini"))
    ap.add_argument("--speakers", type=int, default=2)
    ap.add_argument("--hints", default=os.environ.get("HINTS", ""))
    ap.add_argument("--workdir", default=os.environ.get("KIROCREW_SCRATCH", ".s3work"))
    args = ap.parse_args()

    s3, session = s3_client(args.profile, args.region)

    bucket = args.bucket
    if not bucket:
        bucket = f"egy-voice-journal-audio-{account_id(session)}"
        log(f"لم تحدّد --bucket، سأستخدم: {bucket}")

    if args.command == "setup":
        ensure_bucket(s3, session, bucket, args.region)
        for p in DEFAULT_PREFIXES:
            s3.put_object(Bucket=bucket, Key=p)
        log(f"\nجاهز. ارفع تسجيلاتك إلى:  s3://{bucket}/inbox/<اسم المتصل>/")
        log(f"ثبّت في بيئتك:  JOURNAL_BUCKET={bucket}")
        return

    if args.command == "upload":
        if not args.files:
            die("حدّد ملفًا أو أكثر للرفع.")
        caller = safe_name(args.caller, "غير مصنف")
        for raw in args.files:
            p = Path(raw).expanduser()
            if not p.is_file():
                log(f"تخطٍّ (غير موجود): {p}")
                continue
            key = f"inbox/{caller}/{safe_name(p.stem, p.stem)}{p.suffix.lower()}"
            log(f"رفع {p.name}  ({p.stat().st_size/1e6:.1f} MB)  ->  {key}")
            s3.upload_file(str(p), bucket, key)
        log("تم. شغّل:  python s3_pipeline.py run")
        return

    if args.command == "summaries":
        callers = {k["Key"].split("/")[1] for k in list_keys(s3, bucket, "journal/")
                   if len(k["Key"].split("/")) >= 3}
        for c in sorted(callers):
            rebuild_summary(s3, bucket, c)
        log(f"\nأُعيد بناء {len(callers)} ملخصًا.")
        return

    # run
    workdir = Path(args.workdir).expanduser() / "s3_pipeline"
    workdir.mkdir(parents=True, exist_ok=True)
    done, failed, touched = process_inbox(
        s3, bucket, args.provider, args.speakers, args.hints, workdir)

    if touched:
        log(f"\n{'='*62}\nإعادة بناء الملخصات\n{'='*62}")
        for c in sorted(touched):
            rebuild_summary(s3, bucket, c)

    log(f"\n{'='*62}\nنجح {done} / فشل {failed}")
    if done:
        log(f"الملخصات: s3://{bucket}/summaries/")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()
