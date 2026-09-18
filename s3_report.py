#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
جرد الحاوية -- قراءة فقط، وبلا أي وجع ترميز.

سبب وجوده: AWS CLI على ويندوز يطبع أسماء الملفات العربية عبر ترميز
`charmap` فيفشل، وإعادة التوجيه في PowerShell تكتب UTF-16 مع BOM فيرفضه
الـ CLI بعد ذلك. المرور عبر boto3 مباشرةً يتجاوز السلسلة كلها: القراءة
بايتات، والطباعة UTF-8، ولا وسيط صدفة بينهما.

    python s3_report.py egy-voice-journal-075298365868
"""

from __future__ import annotations

import sys
from collections import defaultdict
from pathlib import Path

import boto3

AUDIO = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".flac",
         ".amr", ".3gp", ".mp4", ".mkv", ".webm", ".wma", ".oga"}


def walk(s3, bucket: str, prefix: str = ""):
    token = None
    while True:
        kw = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kw["ContinuationToken"] = token
        r = s3.list_objects_v2(**kw)
        for o in r.get("Contents", []) or []:
            yield o
        if not r.get("IsTruncated"):
            return
        token = r.get("NextContinuationToken")


def main() -> None:
    bucket = sys.argv[1] if len(sys.argv) > 1 else "egy-voice-journal-075298365868"
    region = sys.argv[2] if len(sys.argv) > 2 else "us-east-1"
    s3 = boto3.client("s3", region_name=region)

    buckets: dict[str, list] = defaultdict(list)
    other: list = []
    total_bytes = 0
    n = 0

    for o in walk(s3, bucket):
        key, size = o["Key"], o["Size"]
        n += 1
        total_bytes += size
        parts = key.split("/")
        top = parts[0] if parts else ""
        if top == "inbox" and len(parts) >= 3 and size > 0:
            buckets[parts[1]].append((parts[-1], size))
        elif size > 0:
            other.append((key, size))

    print(f"الحاوية: {bucket}")
    print(f"إجمالي الكائنات: {n}   الحجم: {total_bytes/2**30:.2f} GiB\n")

    print("=== التصنيف تحت inbox/ ===")
    print(f"{'المتصل':<34} {'ملفات':>7} {'حجم':>10}   صوت/غير صوت")
    print("-" * 74)
    audio_total = nonaudio_total = 0
    for caller in sorted(buckets):
        files = buckets[caller]
        size = sum(s for _, s in files)
        aud = sum(1 for f, _ in files if Path(f).suffix.lower() in AUDIO)
        non = len(files) - aud
        audio_total += aud
        nonaudio_total += non
        flag = "" if non == 0 else f"  <-- {non} غير صوتي"
        print(f"{caller:<34} {len(files):>7} {size/2**20:>8.0f}MB   {aud}/{non}{flag}")
    print("-" * 74)
    print(f"{'المجموع':<34} {sum(len(v) for v in buckets.values()):>7}"
          f" {sum(s for v in buckets.values() for _, s in v)/2**30:>7.2f}GiB"
          f"   {audio_total}/{nonaudio_total}")
    print(f"\nعدد المتصلين (تصنيفات): {len(buckets)}")

    flat = [o for o in walk(s3, bucket, "inbox/")
            if len(o["Key"].split("/")) == 2 and o["Size"] > 0]
    if flat:
        print(f"\nتحذير: {len(flat)} ملف في inbox/ بلا مجلد متصل"
              " -- سيُصنَّف 'غير مصنف'")
        for o in flat[:5]:
            print(f"   {o['Key']}")

    if other:
        print(f"\n=== خارج inbox/ ===")
        by_top: dict[str, int] = defaultdict(int)
        for k, s in other:
            by_top[k.split('/')[0]] += 1
        for t in sorted(by_top):
            print(f"  {t}/  {by_top[t]} كائن")

    print("\n=== ملاحظات تشغيلية ===")
    print("- قاعدة الحذف التلقائي تخصّ processed/ فقط."
          " الصوت في inbox/ يبقى حتى يُعالج.")
    est_chunks = max(1, int(total_bytes / 2**20 / 40 * 4))
    print(f"- تقدير طلبات التفريغ: ~{est_chunks} طلبًا (مقطع كل ١٥ دقيقة)."
          f" السقف اليومي ٢٤٠، فالمتوقع ~{max(1, est_chunks // 240 + 1)} يوم عمل.")


if __name__ == "__main__":
    main()
