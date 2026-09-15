#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
ينزّل ملف صوت من رابط -- هذا هو الطريق العملي للملفات الكبيرة.

سبب وجود هذا الملف: واجهة GitHub على المتصفح لا تقبل رفع أكثر من 25 ميجا،
وتسجيل ساعة على الموبايل يتعدى ذلك. فبدل الرفع، ترفع التسجيل على Drive
(زر المشاركة في الموبايل) وتلصق الرابط -- بلا حد حجم عمليًا.

    python fetch_audio.py "<الرابط>" --out inbox
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

EXTS = {".mp3", ".m4a", ".wav", ".ogg", ".opus", ".aac", ".flac", ".amr",
        ".3gp", ".mp4", ".mkv", ".webm", ".mov", ".wma", ".oga"}


def normalize(url: str) -> str:
    """يحوّل روابط المشاركة إلى روابط تنزيل مباشر."""
    url = url.strip().strip('"').strip("'")

    # Google Drive -> رابط تنزيل مباشر
    m = (re.search(r"drive\.google\.com/file/d/([\w-]+)", url)
         or re.search(r"drive\.google\.com/open\?id=([\w-]+)", url)
         or re.search(r"drive\.usercontent\.google\.com/.*[?&]id=([\w-]+)", url))
    if m:
        return f"https://drive.usercontent.google.com/download?id={m.group(1)}&export=download&confirm=t"
    if "drive.google.com/uc" in url:
        qs = parse_qs(urlparse(url).query)
        fid = (qs.get("id") or [""])[0]
        if fid:
            return f"https://drive.usercontent.google.com/download?id={fid}&export=download&confirm=t"

    # Dropbox -> تنزيل خام
    if "dropbox.com" in url:
        url = re.sub(r"[?&]dl=0", "", url)
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}dl=1"

    # OneDrive / SharePoint
    if "1drv.ms" in url or "sharepoint.com" in url:
        sep = "&" if "?" in url else "?"
        return f"{url}{sep}download=1"

    return url


def guess_name(url: str, resp) -> str:
    """يحدد اسم الملف من الترويسة أو الرابط، ويضمن امتدادًا معقولًا."""
    cd = resp.headers.get("content-disposition", "")
    m = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)", cd)
    name = unquote(m.group(1)) if m else Path(unquote(urlparse(url).path)).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip() or "recording"

    if Path(name).suffix.lower() not in EXTS:
        ctype = (resp.headers.get("content-type") or "").lower()
        ext = (".m4a" if "mp4" in ctype or "m4a" in ctype else
               ".mp3" if "mpeg" in ctype or "mp3" in ctype else
               ".ogg" if "ogg" in ctype else
               ".wav" if "wav" in ctype else
               ".opus" if "opus" in ctype else ".m4a")
        name = Path(name).stem + ext
    return name


def download(url: str, out_dir: Path) -> Path:
    import requests

    direct = normalize(url)
    if direct != url:
        print(f"الرابط بعد التطبيع: {direct[:110]}", flush=True)

    out_dir.mkdir(parents=True, exist_ok=True)
    with requests.get(direct, stream=True, timeout=300, allow_redirects=True,
                      headers={"User-Agent": "Mozilla/5.0"}) as r:
        if r.status_code != 200:
            print(f"\n[خطأ] الرابط رد بحالة {r.status_code}.\n"
                  "  تأكد إن الملف مشارك بصلاحية 'أي شخص لديه الرابط'.",
                  file=sys.stderr)
            sys.exit(1)

        ctype = (r.headers.get("content-type") or "").lower()
        if "text/html" in ctype:
            print("\n[خطأ] الرابط رجّع صفحة ويب مش ملف صوت.\n"
                  "  غالبًا الملف غير مشارك للعموم، أو الرابط رابط عرض مش تنزيل.",
                  file=sys.stderr)
            sys.exit(1)

        name = guess_name(url, r)
        dest = out_dir / name
        n = 1
        while dest.exists():
            dest = out_dir / f"{Path(name).stem}_{n}{Path(name).suffix}"
            n += 1

        total = int(r.headers.get("content-length") or 0)
        done = 0
        step = 0
        with dest.open("wb") as fh:
            for block in r.iter_content(chunk_size=1 << 20):
                if not block:
                    continue
                fh.write(block)
                done += len(block)
                if total and done // (10 << 20) > step:
                    step = done // (10 << 20)
                    print(f"  نزّلت {done/1e6:.0f} / {total/1e6:.0f} MB", flush=True)

    mb = dest.stat().st_size / 1e6
    if mb < 0.01:
        print("\n[خطأ] الملف المنزَّل فارغ.", file=sys.stderr)
        sys.exit(1)
    print(f"تم التنزيل: {dest.name}  ({mb:.1f} MB)", flush=True)
    return dest


def main() -> None:
    ap = argparse.ArgumentParser(description="نزّل ملف صوت من رابط")
    ap.add_argument("url")
    ap.add_argument("--out", default="inbox")
    args = ap.parse_args()
    download(args.url, Path(args.out))


if __name__ == "__main__":
    main()
