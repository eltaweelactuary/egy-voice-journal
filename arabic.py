#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
تطبيع النص العربي -- للبحث والمطابقة فقط، لا للعرض.

المشكلة التي يحلها: نفس الكلمة تُكتب بأشكال كثيرة، فتصير عدة كلمات مختلفة
في نظر أي بحث أو مقارنة:

    إزاي / ازاي / إزّاي / أزاي     ->  ازاي
    مدرسة / مدرسه                   ->  مدرسه
    ٢٠٢٦ / 2026                     ->  2026

بدون هذا، البحث في الأرشيف يفشل بصمت (تكتب «إزاي» فلا يجد «ازاي»)، وأي حساب
لـ WER يعطي رقمًا مبالغًا فيه، وإزالة التكرار لا تعمل.

**قاعدة حاكمة: النص الخام لا يُمسّ أبدًا.**
عاميّة المالك هي المُنتَج نفسه. التطبيع يُخزَّن في حقل منفصل (`normalized`)
بجانب `text`، ولا يحل مكانه. العرض دائمًا من `text`.
"""

from __future__ import annotations

import re
import unicodedata

# التشكيل والعلامات القرآنية والتطويل
_TASHKEEL = re.compile(
    "["
    "\u0610-\u061A"      # علامات قرآنية
    "\u064B-\u065F"      # فتحة ضمة كسرة شدة سكون تنوين
    "\u0670"             # ألف خنجرية
    "\u06D6-\u06ED"      # علامات وقف وتجويد
    "]"
)
_TATWEEL = re.compile("\u0640+")

# محارف صفرية العرض ومحارف الاتجاه -- غير مرئية لكنها تكسر المطابقة
_INVISIBLE = re.compile("[\u200B-\u200F\u202A-\u202E\u2066-\u2069\uFEFF]")

# توحيد الحروف المتشابهة رسمًا
_LETTER_MAP = {
    "\u0623": "\u0627",  # أ -> ا
    "\u0625": "\u0627",  # إ -> ا
    "\u0622": "\u0627",  # آ -> ا
    "\u0671": "\u0627",  # ٱ -> ا
    "\u0649": "\u064A",  # ى -> ي
    "\u0629": "\u0647",  # ة -> ه
    "\u0624": "\u0648",  # ؤ -> و
    "\u0626": "\u064A",  # ئ -> ي
    "\u06A9": "\u0643",  # ک -> ك
    "\u06CC": "\u064A",  # ی -> ي
}

# الأرقام العربية-الهندية والفارسية -> لاتينية
_DIGIT_MAP = {chr(0x0660 + i): str(i) for i in range(10)}
_DIGIT_MAP.update({chr(0x06F0 + i): str(i) for i in range(10)})

_TRANSLATION = str.maketrans({**_LETTER_MAP, **_DIGIT_MAP})

_PUNCT = re.compile(r"[^\w\s]", re.UNICODE)
_SPACES = re.compile(r"\s+")


def normalize(text: str, *, drop_punct: bool = True) -> str:
    """
    يرجّع صورة مطبّعة من النص، صالحة للبحث والمقارنة.

    لا تعرض المخرج للمستخدم ولا تحفظه بدلًا من الأصل.
    """
    if not text:
        return ""
    text = unicodedata.normalize("NFKC", text)
    text = _INVISIBLE.sub("", text)
    text = _TASHKEEL.sub("", text)
    text = _TATWEEL.sub("", text)
    text = text.translate(_TRANSLATION)
    if drop_punct:
        text = _PUNCT.sub(" ", text)
    return _SPACES.sub(" ", text).strip().lower()


def tokens(text: str) -> list[str]:
    """كلمات مطبّعة -- للبحث ولحساب المقاييس."""
    return normalize(text).split()


def contains(haystack: str, needle: str) -> bool:
    """
    بحث يتجاهل اختلافات الرسم.

        contains("مش عارف إزّاي", "ازاي")  ->  True
    """
    n = normalize(needle)
    return bool(n) and n in normalize(haystack)


def wer(reference: str, hypothesis: str) -> dict:
    """
    نسبة خطأ الكلمات على نص مطبّع -- المقياس الذي يحسم «أي محرك أدق».

    يُقاس مقابل نص مرجعي فرّغه إنسان. بدون مرجع لا معنى للرقم.
    """
    ref, hyp = tokens(reference), tokens(hypothesis)
    n, m = len(ref), len(hyp)
    if n == 0:
        return {"wer": 0.0 if m == 0 else 1.0, "ref_words": 0,
                "hyp_words": m, "sub": 0, "ins": m, "dele": 0}

    # مسافة تحرير على مستوى الكلمة (Levenshtein) بصف واحد
    prev = list(range(m + 1))
    for i in range(1, n + 1):
        cur = [i] + [0] * m
        for j in range(1, m + 1):
            cur[j] = min(
                prev[j] + 1,                                  # حذف
                cur[j - 1] + 1,                               # إضافة
                prev[j - 1] + (ref[i - 1] != hyp[j - 1]),      # إبدال
            )
        prev = cur
    dist = prev[m]
    return {"wer": dist / n, "ref_words": n, "hyp_words": m, "distance": dist}


if __name__ == "__main__":
    samples = [
        "مش عارف أعمل إيه",
        "مِشْ عَارِف أعْمَل إيه",
        "مش عارف اعمل ايه",
        "مــش عارف أعمل إيه",
        "مدرسة ٢٠٢٦",
        "مدرسه 2026",
    ]
    print("الأصل -> المطبّع")
    for s in samples:
        print(f"  {s!r:38} -> {normalize(s)!r}")

    print()
    print("بحث متجاهل للرسم:")
    print("  'إزّاي' في 'مش عارف إزّاي':", contains("مش عارف إزّاي", "ازاي"))
    print("  'مدرسه' في 'مدرسة كبيرة':", contains("مدرسة كبيرة", "مدرسه"))

    print()
    r = wer("مش عارف أعمل إيه", "مش عارف اعمل ايه")
    print(f"WER بين رسمين مختلفين لنفس الكلام: {r['wer']:.0%} (يجب أن يكون صفرًا)")
