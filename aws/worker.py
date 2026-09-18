#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
العامل -- يستنزف طابور SQS بالتتابع ثم يخرج.

هذه النسخة تعالج ما وجده مجلس مراجعة من ثلاثة موديلات مستقلة (كلهم: REVISE):

1. **القفل الذري** (BLOCKER، وجده مراجعان). فحصُ Lambda لعدد المهام العاملة
   ثم تشغيلُ مهمة هو تسلسل غير ذري، وECS متسق تدريجيًا -- فاستدعاءان
   متقاربان قد يريان «لا مهام» فيشغّل كلٌّ منهما عاملًا. الحل: عقد إيجار
   حصري بكتابة S3 شرطية. العامل الثاني يجد القفل فيخرج فورًا. الضمان مفروض
   هنا حيث يهم، لا في فحص استشاري قبله.

2. **heartbeat مهلة الظهور** (BLOCKER، وجده مراجعان). الوثيقة وعدت بتمديد
   دوري والتنفيذ لم يفعله. ملف واحد استهلك ١٠١٢ ثانية في قياس حقيقي، فتجاوز
   ٣٦٠٠ ثانية ممكن -- وحينها تعود الرسالة للظهور ويلتقطها عامل آخر.

3. **الحساب لكل طلب لا لكل ملف** (BLOCKER، وجده مراجعان). التفريغ يقطّع
   الملف ويرسل طلبًا لكل مقطع، فتسجيل ساعتين = ٨ طلبات كانت تُحسب طلبًا
   واحدًا. الآن نضبط `transcribe.REQUEST_HOOK` فيُحسب كل طلب فعلي.

4. **مصالحة الصوت الخام** (وجده مراجعان). لو مات العامل بعد رفع التفريغ
   وقبل نقل الصوت، كان الصوت يبقى في inbox للأبد: الإدمبوتنسي ترى التفريغ
   موجودًا فتتخطّى، ولا حدث جديد يُطلق معالجة. الآن مسار التخطّي **يصالح**
   حالة الصوت بدل أن يتجاهلها.

5. **فك الترميز الصحيح**. أحداث EventBridge غير مُرمَّزة، إشعارات S3
   مُرمَّزة. تطبيق `unquote_plus` على الاثنين يفسد مفتاحًا فيه `+` حرفيًا.

6. **التسلسل الذاتي**. لو خرج العامل وفي الطابور عمل، يستدعي المُشغِّل بدل
   انتظار الجدول.

متغيّرات البيئة: BUCKET، QUEUE_URL (إلزاميان)، PROVIDER_ORDER،
FALLBACK_POLICY، SPEAKERS، SSM_PREFIX، MAX_RUNTIME_SEC، RESERVE_SEC،
GEMINI_MIN_GAP، GEMINI_DAILY_CAP، TRIGGER_FUNCTION، LOCK_TTL_SEC.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import sys
import threading
import time
from pathlib import Path
from urllib.parse import unquote_plus

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import boto3                                                    # noqa: E402
from botocore.exceptions import ClientError                      # noqa: E402

import transcribe as TR                                          # noqa: E402
from transcribe import log, transcribe_file                      # noqa: E402
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
RESERVE_SEC = int(os.environ.get("RESERVE_SEC", "600"))
GEMINI_MIN_GAP = float(os.environ.get("GEMINI_MIN_GAP", "6"))
GEMINI_DAILY_CAP = int(os.environ.get("GEMINI_DAILY_CAP", "240"))
TRIGGER_FUNCTION = os.environ.get("TRIGGER_FUNCTION", "")
LOCK_TTL_SEC = int(os.environ.get("LOCK_TTL_SEC", "3900"))
LOCK_KEY = "state/worker.lock"

WORKDIR = Path(os.environ.get("WORKDIR", "/tmp/journal"))
STARTED = time.time()


def budget_left() -> float:
    return MAX_RUNTIME_SEC - (time.time() - STARTED)


# ------------------------------------------------------------ قفل حصري

class Lease:
    """
    عقد إيجار حصري على العمل، بكتابة S3 **شرطية** (If-None-Match).

    الكتابة الشرطية ذرية على مستوى S3: من يكتب أولًا يفوز، والثاني يُرفض
    بـ PreconditionFailed. هذا يجعل «عامل واحد» خاصية مفروضة لا رجاءً.

    للقفل مدة صلاحية: عامل يُقتل بلا تحرير لا يعطّل النظام للأبد.
    """

    def __init__(self, s3, bucket: str):
        self.s3, self.bucket = s3, bucket
        self.held = False
        self.owner = f"{os.environ.get('HOSTNAME', 'worker')}-{os.getpid()}-{int(STARTED)}"

    def _body(self) -> bytes:
        return json.dumps({
            "owner": self.owner,
            "acquired": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
            "ttl_sec": LOCK_TTL_SEC,
        }, ensure_ascii=False).encode("utf-8")

    def _try_write(self) -> bool:
        try:
            self.s3.put_object(Bucket=self.bucket, Key=LOCK_KEY,
                               Body=self._body(), IfNoneMatch="*",
                               ContentType="application/json")
            return True
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in ("PreconditionFailed", "ConditionalRequestConflict"):
                return False
            raise
        except (TypeError, ValueError):
            # botocore أقدم من أن يعرف IfNoneMatch -- لا نتظاهر بضمان لا نملكه
            log("تحذير: هذه النسخة من botocore لا تدعم الكتابة الشرطية،"
                " فالقفل غير ذري. حدّث boto3 لضمان عامل واحد.")
            self.s3.put_object(Bucket=self.bucket, Key=LOCK_KEY,
                               Body=self._body())
            return True

    def acquire(self) -> bool:
        if self._try_write():
            self.held = True
            return True
        # القفل مأخوذ: افحص عمره، فقد يكون عاملًا مات دون تحرير
        try:
            head = self.s3.head_object(Bucket=self.bucket, Key=LOCK_KEY)
            age = (dt.datetime.now(dt.timezone.utc) - head["LastModified"]).total_seconds()
        except ClientError:
            age = 0.0
        if age > LOCK_TTL_SEC:
            log(f"القفل قديم ({age:.0f}ث > {LOCK_TTL_SEC}ث) -- أعتبره متروكًا وأستحوذ")
            self.s3.delete_object(Bucket=self.bucket, Key=LOCK_KEY)
            if self._try_write():
                self.held = True
                return True
        log(f"عامل آخر يحمل القفل (عمره {age:.0f}ث) -- أخرج بلا عمل")
        return False

    def refresh(self) -> None:
        if self.held:
            try:
                self.s3.put_object(Bucket=self.bucket, Key=LOCK_KEY,
                                   Body=self._body(),
                                   ContentType="application/json")
            except ClientError:
                pass

    def release(self) -> None:
        if not self.held:
            return
        try:
            self.s3.delete_object(Bucket=self.bucket, Key=LOCK_KEY)
        except ClientError:
            pass
        self.held = False


# ------------------------------------------------------------ heartbeat

class Heartbeat(threading.Thread):
    """
    يمدّد مهلة ظهور الرسالة ويجدّد القفل أثناء معالجة طويلة.

    بدونه، ملف يتجاوز مهلة الظهور تعود رسالته للطابور فيُعالج مرتين --
    وقياس حقيقي أعطى ١٠١٢ ثانية لملف واحد، فالتجاوز ممكن لا نظري.
    """

    def __init__(self, sqs, queue_url: str, receipt: str, lease: Lease,
                 interval: int = 300, visibility: int = 1800):
        super().__init__(daemon=True)
        self.sqs, self.queue_url, self.receipt = sqs, queue_url, receipt
        self.lease, self.interval, self.visibility = lease, interval, visibility
        self._stop = threading.Event()

    def run(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.sqs.change_message_visibility(
                    QueueUrl=self.queue_url, ReceiptHandle=self.receipt,
                    VisibilityTimeout=self.visibility)
                self.lease.refresh()
            except Exception as exc:
                log(f"  تحذير: تمديد المهلة فشل: {type(exc).__name__}")

    def stop(self) -> None:
        self._stop.set()


# ------------------------------------------------------------ الأسرار

def load_secrets(ssm) -> None:
    """يقرأ مفاتيح API من SSM Parameter Store (النوع القياسي مجاني)."""
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


# ------------------------------------------------------------ الحصة

class Quota:
    """
    فاصل زمني وسقف يومي، يُحاسبان **لكل طلب فعلي**.

    يُثبَّت كـ `transcribe.REQUEST_HOOK` فيُستدعى قبل كل نداء لمزوّد -- بما
    في ذلك كل مقطع من الملف، وكل محرك في التقاطع، وطلب المصالحة. الحساب على
    مستوى الملف كان يخطئ بمعامل يساوي عدد المقاطع.
    """

    def __init__(self, s3, bucket: str):
        self.s3, self.bucket = s3, bucket
        self.day = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d")
        self.key = f"state/quota-{self.day}.json"
        self.used = self._read()
        self.start_used = self.used
        self.last_call = 0.0

    def _read(self) -> int:
        try:
            body = self.s3.get_object(Bucket=self.bucket, Key=self.key)["Body"].read()
            return int(json.loads(body).get("gemini_calls", 0))
        except Exception:
            return 0

    def flush(self) -> None:
        try:
            self.s3.put_object(
                Bucket=self.bucket, Key=self.key,
                Body=json.dumps({"day": self.day, "gemini_calls": self.used},
                                ensure_ascii=False).encode("utf-8"),
                ContentType="application/json")
        except Exception as exc:
            log(f"  تحذير: حفظ عدّاد الحصة فشل: {type(exc).__name__}")

    def hook(self, provider: str) -> None:
        """يُستدعى قبل كل طلب. يرفع SystemExit عند نفاد الحصة."""
        if provider != "gemini":
            return                       # حدود groq المجانية سخية، لا نقيّدها
        if self.used >= GEMINI_DAILY_CAP:
            raise SystemExit(
                f"نفدت حصة Gemini اليومية ({self.used}/{GEMINI_DAILY_CAP})."
                " سيسقط للمزوّد التالي أو يُعاد لاحقًا.")
        gap = GEMINI_MIN_GAP - (time.time() - self.last_call)
        if gap > 0:
            time.sleep(gap)
        self.last_call = time.time()
        self.used += 1
        if self.used % 5 == 0:           # لا نكتب على S3 لكل طلب
            self.flush()


# ------------------------------------------------------------ الفهرس

def build_index(s3, bucket: str) -> dict:
    """يبني index.json -- الملف الوحيد الذي تقرأه أي عملية لاحقة."""
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


# ------------------------------------------------------------ الرسائل

def keys_from_message(body: str) -> list[str]:
    """
    يستخرج مفاتيح S3 من رسالة.

    فرقٌ مهم: `detail.object.key` في EventBridge **غير** مُرمَّز، بينما مفتاح
    إشعار S3 المباشر مُرمَّز بترميز URL. فك الترميز على الاثنين يفسد أي مفتاح
    فيه `+` حرفيًا -- وأسماء تسجيلات الموبايل مليئة بالمحارف الغريبة.
    """
    try:
        msg = json.loads(body)
    except Exception:
        return []
    out: list[str] = []
    detail = msg.get("detail") or {}
    k = (detail.get("object") or {}).get("key")
    if k:
        out.append(k)                                  # EventBridge: كما هو
    for rec in msg.get("Records", []) or []:
        k = ((rec.get("s3") or {}).get("object") or {}).get("key")
        if k:
            out.append(unquote_plus(k))                # إشعار S3: مُرمَّز
    return out


# ------------------------------------------------------------ المعالجة

def reconcile_raw(s3, key: str, caller: str, stem: str) -> None:
    """
    يُنهي حالة الصوت الخام لملف فُرّغ سابقًا.

    السبب: لو مات العامل بعد رفع التفريغ وقبل نقل الصوت، يبقى الصوت في
    inbox بلا نهاية -- الإدمبوتنسي ترى التفريغ فتتخطّى، ولا حدث جديد يُطلق
    معالجة، وقاعدة الحذف التلقائي تخصّ processed/ لا inbox/. فالتخطّي يجب
    أن **يصالح** لا أن يتجاهل.
    """
    try:
        s3.head_object(Bucket=BUCKET, Key=key)
    except ClientError:
        return                                   # لم يبقَ شيء، تمام
    dest = f"processed/{caller}/{stem}{Path(key).suffix.lower()}"
    try:
        s3.copy_object(Bucket=BUCKET, Key=dest,
                       CopySource={"Bucket": BUCKET, "Key": key})
        s3.delete_object(Bucket=BUCKET, Key=key)
        log(f"  مصالحة: نُقل صوت خام متروك إلى {dest}")
    except ClientError as exc:
        log(f"  تحذير: مصالحة الصوت الخام فشلت: {exc.response.get('Error', {}).get('Code')}")


def transcribe_with_fallback(audio: Path, out_dir: Path, caller: str,
                             stem: str) -> str:
    """
    يجرّب المزوّدين بالترتيب. الحصة تُحاسب داخل الخطّاف لا هنا.

    السياسة `degrade` هي الافتراضي **بقرار المالك الصريح**: «النموذج الآخر
    كان يعمل جيدًا بدون مشاكل ولو مش دقيق». الثمن معروف وموثّق: groq يُفصّح
    ويهلوس أحيانًا.
    """
    for provider in PROVIDER_ORDER:
        try:
            transcribe_file(audio, out_dir, provider, SPEAKERS, "",
                            title=f"{caller} — {stem}")
            return provider
        except SystemExit as exc:
            log(f"    '{provider}' فشل: {str(exc)[:200]}")
        except Exception as exc:
            log(f"    '{provider}' فشل: {type(exc).__name__}: {str(exc)[:200]}")
        if FALLBACK_POLICY == "requeue":
            log("    السياسة requeue -- لا سقوط لمزوّد أقل دقة، نحاول لاحقًا")
            return ""
    return ""


def handle(s3, key: str) -> bool:
    if Path(key).suffix.lower() not in AUDIO_EXTS:
        log(f"  تخطٍّ (ليس صوتًا): {key}")
        return True
    if not key.startswith("inbox/"):
        log(f"  تخطٍّ (خارج inbox/): {key}")
        return True

    caller = caller_of(key)
    stem = safe_name(Path(key).stem, "rec")
    log(f"\n{'='*60}\n{key}\n  المتصل: {caller}\n{'='*60}")

    if list_keys(s3, BUCKET, f"journal/{caller}/{stem}.json"):
        log("  مُفرَّغ سابقًا -- تخطٍّ، مع مصالحة حالة الصوت")
        reconcile_raw(s3, key, caller, stem)
        return True

    local = WORKDIR / caller
    local.mkdir(parents=True, exist_ok=True)
    audio = local / f"{stem}{Path(key).suffix.lower()}"
    out_dir = local / "out"

    try:
        s3.download_file(BUCKET, key, str(audio))
        log(f"  نُزّل: {audio.stat().st_size/1e6:.1f} MB")

        used = transcribe_with_fallback(audio, out_dir, caller, stem)
        if not used:
            log("  فشل كل المزوّدين -- الرسالة تبقى للمحاولة القادمة")
            return False

        for f in sorted(out_dir.glob(f"{stem}.*")):
            ctype = {"md": "text/markdown", "json": "application/json"}.get(
                f.suffix.lstrip("."), "text/plain")
            s3.upload_file(str(f), BUCKET, f"journal/{caller}/{f.name}",
                           ExtraArgs={"ContentType": f"{ctype}; charset=utf-8"})

        reconcile_raw(s3, key, caller, stem)
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


def queue_depth(sqs) -> int:
    try:
        a = sqs.get_queue_attributes(
            QueueUrl=QUEUE_URL,
            AttributeNames=["ApproximateNumberOfMessages",
                            "ApproximateNumberOfMessagesNotVisible"])["Attributes"]
        return (int(a.get("ApproximateNumberOfMessages", 0))
                + int(a.get("ApproximateNumberOfMessagesNotVisible", 0)))
    except Exception:
        return 0


def chain_next(depth: int) -> None:
    """
    يستدعي المُشغِّل لو بقي عمل عند الخروج.

    بدون هذا، عامل يستنفد ميزانيته وفي الطابور عمل ينتظر الجدول -- وهي فجوة
    توفّر نبّه إليها المجلس.
    """
    if depth <= 0 or not TRIGGER_FUNCTION:
        return
    try:
        boto3.client("lambda").invoke(
            FunctionName=TRIGGER_FUNCTION, InvocationType="Event",
            Payload=b'{"source":"worker-chain"}')
        log(f"بقي {depth} في الطابور -- استدعيت المُشغِّل لعامل تالٍ")
    except Exception as exc:
        log(f"تحذير: التسلسل الذاتي فشل ({type(exc).__name__})."
            " الجدول الاحتياطي سيتكفّل.")


def main() -> None:
    if not BUCKET or not QUEUE_URL:
        print("[خطأ] BUCKET و QUEUE_URL إلزاميان", file=sys.stderr)
        sys.exit(2)

    s3, sqs, ssm = boto3.client("s3"), boto3.client("sqs"), boto3.client("ssm")

    lease = Lease(s3, BUCKET)
    if not lease.acquire():
        sys.exit(0)                     # ليس خطأ: عامل آخر يعمل، وهذا مقصود

    done = failed = 0
    touched: set[str] = set()
    quota = Quota(s3, BUCKET)

    try:
        load_secrets(ssm)
        WORKDIR.mkdir(parents=True, exist_ok=True)
        TR.REQUEST_HOOK = quota.hook    # الحساب لكل طلب، لا لكل ملف
        log(f"العامل بدأ (قفل: {lease.owner}). المزوّدون: {PROVIDER_ORDER}"
            f" | السياسة: {FALLBACK_POLICY}"
            f" | حصة gemini اليوم: {quota.used}/{GEMINI_DAILY_CAP}")

        while budget_left() > RESERVE_SEC:
            resp = sqs.receive_message(
                QueueUrl=QUEUE_URL, MaxNumberOfMessages=1,
                WaitTimeSeconds=20, VisibilityTimeout=1800)
            msgs = resp.get("Messages", [])
            if not msgs:
                log("الطابور فارغ -- إنهاء (لا ثانية Fargate مهدورة)")
                break

            m = msgs[0]
            keys = keys_from_message(m["Body"])
            if not keys:
                log("رسالة غير مفهومة -- حذف")
                sqs.delete_message(QueueUrl=QUEUE_URL,
                                   ReceiptHandle=m["ReceiptHandle"])
                continue

            hb = Heartbeat(sqs, QUEUE_URL, m["ReceiptHandle"], lease)
            hb.start()
            try:
                ok = True
                for key in keys:
                    if handle(s3, key):
                        touched.add(caller_of(key))
                        done += 1
                    else:
                        ok = False
                        failed += 1
            finally:
                hb.stop()

            if ok:
                sqs.delete_message(QueueUrl=QUEUE_URL,
                                   ReceiptHandle=m["ReceiptHandle"])
            else:
                sqs.change_message_visibility(
                    QueueUrl=QUEUE_URL, ReceiptHandle=m["ReceiptHandle"],
                    VisibilityTimeout=300)

        if touched:
            log(f"\nإعادة بناء ملخصات: {sorted(touched)}")
            for c in sorted(touched):
                rebuild_summary(s3, BUCKET, c)
            build_index(s3, BUCKET)
    finally:
        TR.REQUEST_HOOK = None
        if quota.used != quota.start_used:
            quota.flush()
        depth = queue_depth(sqs)
        lease.release()                  # حرّر القفل قبل استدعاء التالي
        chain_next(depth)

    log(f"\nانتهى العامل: نجح {done} / فشل {failed}"
        f" | طلبات gemini هذه الجلسة: {quota.used - quota.start_used}")
    sys.exit(0)


if __name__ == "__main__":
    main()
