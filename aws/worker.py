#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
العامل -- يستنزف طابور SQS بالتتابع ثم يخرج.

مبدأ التصميم الحاكم: **نسخة واحدة فقط تعمل في أي لحظة، وتعالج ملفًا واحدًا
في المرة.** التوازي هنا عطب لا تحسين: الطبقة المجانية لـ Gemini حدّها ١٠
طلبات/دقيقة، ورفع دفعة من ٢٠ ملفًا بالتوازي يفجّرها فورًا. قياس حقيقي: ملف
واحد استهلك 1012 ثانية بسبب 503 متكرر.

يخرج بمجرد أن يفرغ الطابور -- فلا تُدفع ثانية Fargate واحدة بلا عمل.

متغيّرات البيئة:
    BUCKET            اسم الحاوية (إلزامي)
    QUEUE_URL         رابط طابور SQS (إلزامي)
    PROVIDER_ORDER    ترتيب المزوّدين، افتراضي "gemini,groq"
    FALLBACK_POLICY   degrade (افتراضي) أو requeue
    SPEAKERS          افتراضي 2
    SSM_PREFIX        بادئة مفاتيح API في Parameter Store، افتراضي /journal/
    MAX_RUNTIME_SEC   ميزانية زمن التشغيل، افتراضي 3000
    GEMINI_MIN_GAP    ثوانٍ بين طلبات Gemini، افتراضي 6 (= 10/دقيقة)
    GEMINI_DAILY_CAP  سقف يومي، افتراضي 240 (الحد 250)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3                                                    # noqa: E402
from transcribe import transcribe_file, log                     # noqa: E402
from s3_pipeline import (                                        # noqa: E402
    AUDIO_EXTS, RAW_AUDIO_TTL_DAYS, caller_of, list_keys,
    rebuild_summary, safe_name,
)

BUCKET = os.environ.get("BUCKET", "")
QUEUE_URL = os.environ.get("QUEUE_URL", "")
PROVIDER_ORDER = [p.strip() for p in
                  os.environ.get("PROVIDER_ORDER", "gemini,groq").split(",") if p.strip()]
FALLBACK_POLICY = os.environ.get("FALLBACK_POLICY", "degrade").strip().lower()
SPEAKERS = int(os.environ.get("SPEAKERS", "2"))
SSM_PREFIX = os.environ.get("SSM_PREFIX", "/journal/")
MAX_RUNTIME_SEC = int(os.environ.get("MAX_RUNTIME_SEC", "3000"))
GEMINI_MIN_GAP = float(os.environ.get("GEMINI_MIN_GAP", "6"))
GEMINI_DAILY_CAP = int(os.environ.get("GEMINI_DAILY_CAP", "240"))

WORKDIR = Path(os.environ.get("WORKDIR", "/tmp/journal"))
STARTED = time.time()


def budget_left() -> float:
    return MAX_RUNTIME_SEC - (time.time() - STARTED)


# ------------------------------------------------------------ الأسرار

def load_secrets(ssm) -> None:
    """
    يقرأ مفاتيح API من SSM Parameter Store.

    Parameter Store (النوع القياسي) مجاني، بخلاف Secrets Manager الذي يكلّف
    ٠٫٤٠ دولار لكل سرّ شهريًا. ولا نضع المفاتيح في متغيّرات بيئة تعريف
    المهمة لأنها تظهر نصًا صريحًا لكل من يقرأ التعريف في الكونسول.
    """
    names = ["GEMINI_API_KEY", "GROQ_API_KEY", "ELEVENLABS_API_KEY"]
    try:
        resp = ssm.get_parameters(
            Names=[f"{SSM_PREFIX}{n}" for n in names], WithDecryption=True)
    except Exception as exc:
        log(f"تحذير: تعذّر قراءة الأسرار من SSM: {exc}")
        return
    got = []
    for p in resp.get("Parameters", []):
        key = p["Name"].rsplit("/", 1)[-1]
        if p.get("Value"):
            os.environ[key] = p["Value"]
            got.append(key)
    log(f"الأسرار المحمّلة: {got or 'لا شيء'}")
    for miss in resp.get("InvalidParameters", []) or []:
        log(f"  غير مضبوط: {miss}")


# ------------------------------------------------------------ حدود المعدل

class Quota:
    """
    مُحدِّد معدل + سقف يومي محفوظ في S3.

    السقف اليومي يُحفظ في الحاوية لا في الذاكرة، لأن العامل يُنشأ ويُفنى عدة
    مرات في اليوم فذاكرته لا تعيش. عند نفاد السقف نتوقف بهدوء ونترك الرسائل
    في الطابور للغد -- لا نحرقها في فشل مؤكد.
    """

    def __init__(self, s3, bucket: str):
        self.s3, self.bucket = s3, bucket
        self.day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        self.key = f"state/quota-{self.day}.json"
        self.used = self._read()
        self.last_call = 0.0

    def _read(self) -> int:
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=self.key)["Body"].read()
            return int(json.loads(body).get("gemini_calls", 0))
        except Exception:
            return 0

    def _write(self) -> None:
        try:
            self.s3.put_object(
                Bucket=self.bucket, Key=self.key,
                Body=json.dumps({"day": self.day, "gemini_calls": self.used},
                                ensure_ascii=False).encode("utf-8"),
                ContentType="application/json")
        except Exception as exc:
            log(f"تحذير: تعذّر حفظ عدّاد الحصة: {exc}")

    def gemini_available(self) -> bool:
        return self.used < GEMINI_DAILY_CAP

    def before_gemini(self) -> None:
        gap = GEMINI_MIN_GAP - (time.time() - self.last_call)
        if gap > 0:
            time.sleep(gap)
        self.last_call = time.time()
        self.used += 1
        self._write()


# ------------------------------------------------------------ الفهرس

def build_index(s3, bucket: str) -> dict:
    """
    يبني index.json في الجذر -- الملف الوحيد الذي تقرأه أي عملية لاحقة،
    فلا تحتاج سرد الحاوية.
    """
    callers: dict[str, list] = {}
    for obj in list_keys(s3, bucket, "journal/"):
        key = obj["Key"]
        if not key.endswith(".json") or len(key.split("/")) < 3:
            continue
        try:
            data = json.loads(
                s3.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8"))
        except Exception:
            continue
        m = data.get("meta", {})
        callers.setdefault(key.split("/")[1], []).append({
            "stem": Path(key).stem,
            "date": m.get("date", ""),
            "duration": float(m.get("duration", 0) or 0),
            "words": int(m.get("words", 0) or 0),
            "provider": m.get("provider", ""),
            "json_key": key,
        })

    entries = []
    for caller in sorted(callers):
        items = sorted(callers[caller], key=lambda d: d["date"])
        entries.append({
            "caller": caller,
            "recordings": len(items),
            "hours": round(sum(i["duration"] for i in items) / 3600, 2),
            "words": sum(i["words"] for i in items),
            "summary_key": f"summaries/{caller}.md",
            "items": items,
        })

    index = {
        "generated": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "totals": {
            "recordings": sum(e["recordings"] for e in entries),
            "hours": round(sum(e["hours"] for e in entries), 2),
            "words": sum(e["words"] for e in entries),
            "callers": len(entries),
        },
        "callers": entries,
    }
    s3.put_object(Bucket=bucket, Key="index.json",
                  Body=json.dumps(index, ensure_ascii=False, indent=2).encode("utf-8"),
                  ContentType="application/json; charset=utf-8")
    log(f"index.json: {index['totals']}")
    return index


# ------------------------------------------------------------ المعالجة

def keys_from_message(body: str) -> list[str]:
    """يستخرج مفاتيح S3 من رسالة، سواء جاءت من EventBridge أو من إشعار S3."""
    try:
        msg = json.loads(body)
    except Exception:
        return []
    out = []
    detail = msg.get("detail") or {}
    if detail.get("object", {}).get("key"):          # EventBridge
        out.append(detail["object"]["key"])
    for rec in msg.get("Records", []) or []:         # إشعار S3 المباشر
        k = rec.get("s3", {}).get("object", {}).get("key")
        if k:
            out.append(k)
    from urllib.parse import unquote_plus
    return [unquote_plus(k) for k in out]


def transcribe_with_fallback(audio: Path, out_dir: Path, caller: str,
                             stem: str, quota: Quota) -> str:
    """
    يجرّب المزوّدين بالترتيب. يرجّع اسم المزوّد الناجح، أو "" لو فشل الجميع.

    سياسة الفشل:
      degrade  (افتراضي) -- اسقط للمزوّد التالي فورًا. المالك فضّل هذا صراحةً:
                 «النموذج الآخر كان يعمل جيدًا بدون مشاكل ولو مش دقيق».
      requeue  -- لا تسقط لمزوّد أقل دقة؛ أعد الرسالة للطابour وحاول لاحقًا
                 بالمزوّد الجيد. اختره لو الدقة أهم من سرعة الإنجاز.
    """
    for provider in PROVIDER_ORDER:
        if provider == "gemini":
            if not quota.gemini_available():
                log(f"    تخطّي gemini: بلغنا السقف اليومي ({quota.used})")
                continue
            quota.before_gemini()
        try:
            transcribe_file(audio, out_dir, provider, SPEAKERS, "",
                            title=f"{caller} — {stem}")
            return provider
        except SystemExit as exc:
            log(f"    '{provider}' فشل: {str(exc)[:200]}")
        except Exception as exc:
            log(f"    '{provider}' فشل: {type(exc).__name__}: {str(exc)[:200]}")
        if FALLBACK_POLICY == "requeue":
            log("    السياسة requeue -- لا سقوط لمزوّد أقل دقة، سنحاول لاحقًا")
            return ""
    return ""


def handle(s3, sqs, key: str, quota: Quota) -> bool:
    if Path(key).suffix.lower() not in AUDIO_EXTS:
        log(f"  تخطٍّ (ليس صوتًا): {key}")
        return True
    if not key.startswith("inbox/"):
        log(f"  تخطٍّ (خارج inbox/): {key}")
        return True

    caller = caller_of(key)
    stem = safe_name(Path(key).stem, "rec")
    log(f"\n{'='*60}\n{key}\n  المتصل: {caller}\n{'='*60}")

    # إدمبوتنسي: SQS يسلّم at-least-once، وإعادة المحاولة طبيعية
    if list_keys(s3, BUCKET, f"journal/{caller}/{stem}.json"):
        log("  مُفرَّغ سابقًا -- تخطٍّ")
        return True

    local = WORKDIR / caller
    local.mkdir(parents=True, exist_ok=True)
    audio = local / f"{stem}{Path(key).suffix.lower()}"
    out_dir = local / "out"

    try:
        s3.download_file(BUCKET, key, str(audio))
        log(f"  نُزّل: {audio.stat().st_size/1e6:.1f} MB")

        used = transcribe_with_fallback(audio, out_dir, caller, stem, quota)
        if not used:
            log("  فشل كل المزوّدين -- الرسالة تبقى للمحاولة القادمة")
            return False

        for f in sorted(out_dir.glob(f"{stem}.*")):
            ctype = {"md": "text/markdown", "json": "application/json"}.get(
                f.suffix.lstrip("."), "text/plain")
            s3.upload_file(str(f), BUCKET, f"journal/{caller}/{f.name}",
                           ExtraArgs={"ContentType": f"{ctype}; charset=utf-8"})

        s3.copy_object(Bucket=BUCKET, Key=f"processed/{caller}/{audio.name}",
                       CopySource={"Bucket": BUCKET, "Key": key})
        s3.delete_object(Bucket=BUCKET, Key=key)
        log(f"  تم عبر '{used}'. الصوت في processed/"
            f" (يُحذف بعد {RAW_AUDIO_TTL_DAYS} يومًا)")
        return True
    except Exception as exc:
        log(f"  خطأ: {type(exc).__name__}: {str(exc)[:300]}")
        return False
    finally:
        for f in local.rglob("*"):
            if f.is_file():
                try:
                    f.unlink()
                except OSError:
                    pass


def main() -> None:
    if not BUCKET or not QUEUE_URL:
        print("[خطأ] BUCKET و QUEUE_URL إلزاميان", file=sys.stderr)
        sys.exit(2)

    s3, sqs, ssm = boto3.client("s3"), boto3.client("sqs"), boto3.client("ssm")
    load_secrets(ssm)
    WORKDIR.mkdir(parents=True, exist_ok=True)

    quota = Quota(s3, BUCKET)
    log(f"العامل بدأ. المزوّدون: {PROVIDER_ORDER} | السياسة: {FALLBACK_POLICY}"
        f" | حصة gemini المستهلكة اليوم: {quota.used}/{GEMINI_DAILY_CAP}")

    done = failed = 0
    touched: set[str] = set()

    while budget_left() > 300:                 # اترك هامشًا لبناء الملخصات
        resp = sqs.receive_message(
            QueueUrl=QUEUE_URL, MaxNumberOfMessages=1,
            WaitTimeSeconds=20, VisibilityTimeout=3600)
        msgs = resp.get("Messages", [])
        if not msgs:
            log("الطابور فارغ -- إنهاء (لا ثانية Fargate مهدورة)")
            break

        m = msgs[0]
        keys = keys_from_message(m["Body"])
        if not keys:
            log("رسالة غير مفهومة -- حذف")
            sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=m["ReceiptHandle"])
            continue

        ok = True
        for key in keys:
            if handle(s3, sqs, key, quota):
                touched.add(caller_of(key))
                done += 1
            else:
                ok = False
                failed += 1

        if ok:
            sqs.delete_message(QueueUrl=QUEUE_URL, ReceiptHandle=m["ReceiptHandle"])
        else:
            # أعِدها للظهور بعد ٥ دقائق -- SQS يرسلها للـ DLQ بعد 3 محاولات
            sqs.change_message_visibility(
                QueueUrl=QUEUE_URL, ReceiptHandle=m["ReceiptHandle"],
                VisibilityTimeout=300)

        if not quota.gemini_available() and PROVIDER_ORDER == ["gemini"]:
            log("نفدت حصة gemini ولا مزوّد بديل -- إنهاء، والباقي للغد")
            break

    if touched:
        log(f"\nإعادة بناء ملخصات: {sorted(touched)}")
        for c in sorted(touched):
            rebuild_summary(s3, BUCKET, c)
        build_index(s3, BUCKET)

    log(f"\nانتهى العامل: نجح {done} / فشل {failed}")
    sys.exit(0)


if __name__ == "__main__":
    main()
