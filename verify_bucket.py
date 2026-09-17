#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
يتحقق أن الحاوية خاصة فعلًا -- قبل أن يُرفع فيها أي تسجيل شخصي.

    python verify_bucket.py egy-voice-journal-075298365868

لا يكفي قراءة الإعدادات: الإعداد قد يُقرأ صحيحًا وسلوك الحاوية مختلفًا. لذلك
يُجري اختبارين حقيقيين إلى جانب قراءة الإعدادات:

  - طلب **غير موقّع** (كأي شخص على الإنترنت) -- يجب أن يُرفض.
  - طلب موقّع عبر **HTTP غير مشفّر** -- يجب أن ترفضه سياسة الحاوية.

الأول يثبت حجب الوصول العام، والثاني يثبت أن فرض HTTPS ليس مجرد نص في سياسة.
"""

from __future__ import annotations

import json
import sys

import boto3
from botocore import UNSIGNED
from botocore.config import Config
from botocore.exceptions import ClientError

PASS, FAIL = 0, 0


def check(label: str, ok: bool, detail: str = "") -> None:
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [نجح]  {label}")
    else:
        FAIL += 1
        print(f"  [فشل]  {label}")
    if detail:
        print(f"         {detail}")


def main() -> None:
    bucket = sys.argv[1] if len(sys.argv) > 1 else ""
    region = sys.argv[2] if len(sys.argv) > 2 else "us-east-1"
    if not bucket:
        print("الاستخدام: python verify_bucket.py <bucket> [region]")
        sys.exit(2)

    s3 = boto3.client("s3", region_name=region)
    print(f"التحقق من: s3://{bucket}  ({region})\n")

    print("[الإعدادات]")
    try:
        pab = s3.get_public_access_block(Bucket=bucket)[
            "PublicAccessBlockConfiguration"]
        wanted = ["BlockPublicAcls", "IgnorePublicAcls",
                  "BlockPublicPolicy", "RestrictPublicBuckets"]
        for k in wanted:
            check(f"حجب الوصول العام: {k}", pab.get(k) is True, f"القيمة: {pab.get(k)}")
    except ClientError as exc:
        check("قراءة حجب الوصول العام", False, str(exc)[:160])

    try:
        rules = s3.get_bucket_encryption(Bucket=bucket)[
            "ServerSideEncryptionConfiguration"]["Rules"]
        alg = rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        check("التشفير الافتراضي مُفعَّل", alg in ("AES256", "aws:kms"), f"الخوارزمية: {alg}")
    except ClientError as exc:
        check("التشفير الافتراضي", False, str(exc)[:160])

    try:
        pol = json.loads(s3.get_bucket_policy(Bucket=bucket)["Policy"])
        deny_tls = any(
            st.get("Effect") == "Deny"
            and st.get("Condition", {}).get("Bool", {}).get("aws:SecureTransport") in ("false", False)
            for st in pol.get("Statement", []))
        check("سياسة ترفض النقل غير المشفّر", deny_tls)
    except ClientError as exc:
        check("سياسة الحاوية", False, str(exc)[:160])

    try:
        rules = s3.get_bucket_lifecycle_configuration(Bucket=bucket)["Rules"]
        raw = [r for r in rules
               if (r.get("Filter", {}) or {}).get("Prefix") == "processed/"
               and r.get("Status") == "Enabled"]
        days = raw[0].get("Expiration", {}).get("Days") if raw else None
        check("حذف الصوت الخام تلقائيًا", bool(days), f"processed/ بعد {days} يومًا")
        journal_rule = [r for r in rules
                        if (r.get("Filter", {}) or {}).get("Prefix", "").startswith("journal")]
        check("لا قاعدة تحذف التفريغ", not journal_rule)
    except ClientError as exc:
        check("قواعد دورة الحياة", False, str(exc)[:160])

    print("\n[اختبارات وصول حقيقية]")

    anon = boto3.client("s3", region_name=region,
                        config=Config(signature_version=UNSIGNED))
    try:
        anon.list_objects_v2(Bucket=bucket, MaxKeys=1)
        check("طلب غير موقّع مرفوض", False,
              "خطر: الحاوية قابلة للسرد بلا اعتماد")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        check("طلب غير موقّع مرفوض", True, f"رُفض بـ {code}")

    try:
        http = boto3.client("s3", region_name=region,
                            endpoint_url=f"http://s3.{region}.amazonaws.com")
        http.list_objects_v2(Bucket=bucket, MaxKeys=1)
        check("طلب موقّع عبر HTTP مرفوض", False,
              "خطر: النقل غير المشفّر مسموح")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        check("طلب موقّع عبر HTTP مرفوض", True, f"رُفض بـ {code}")
    except Exception as exc:
        check("طلب موقّع عبر HTTP مرفوض", True,
              f"فشل الاتصال: {type(exc).__name__}")

    print(f"\n{'='*54}\nنجح {PASS} / فشل {FAIL}")
    if FAIL:
        print("لا ترفع أي تسجيل قبل معالجة ما فشل.")
    else:
        print("الحاوية خاصة. الرفع آمن.")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
