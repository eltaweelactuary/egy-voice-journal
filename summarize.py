#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""يطبع جدول ملخص بصيغة Markdown لكل التفريغات -- يُستخدم في ملخص Actions."""

from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> None:
    journal = Path(sys.argv[1] if len(sys.argv) > 1 else "journal")
    files = sorted(journal.glob("*.json"))
    if not files:
        print("_لا يوجد أي تفريغ بعد._")
        return

    print("| التسجيل | التاريخ | المدة | كلمات | المحرك |")
    print("|---|---|---|---|---|")
    total_sec = 0.0
    total_words = 0
    for f in files:
        try:
            meta = json.loads(f.read_text(encoding="utf-8")).get("meta", {})
        except Exception:
            continue
        dur = float(meta.get("duration", 0) or 0)
        words = int(meta.get("words", 0) or 0)
        total_sec += dur
        total_words += words
        print(
            f"| {meta.get('title', f.stem)} "
            f"| {str(meta.get('date', ''))[:16]} "
            f"| {int(dur)//60}:{int(dur)%60:02d} "
            f"| {words:,} "
            f"| {meta.get('provider', '')} |"
        )
    print(
        f"\n**الإجمالي:** {len(files)} تسجيل، "
        f"{total_sec/3600:.1f} ساعة صوت، {total_words:,} كلمة."
    )


if __name__ == "__main__":
    main()
