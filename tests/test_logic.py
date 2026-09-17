#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
اختبارات منطقية بلا شبكة ولا مفاتيح -- تعمل في أي مكان وفي أي CI.

    python tests/test_logic.py

تغطّي الأجزاء التي يمكن التحقق منها بلا استدعاء أي مزوّد: التطبيع العربي،
حساب الاتفاق بين المسودات، تطبيع أسماء الملفات، واستخراج اسم المتصل.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import arabic                                    # noqa: E402
import s3_pipeline as S                          # noqa: E402
import transcribe as T                           # noqa: E402

PASS, FAIL = 0, 0


def check(label: str, got, want=None, *, truthy: bool = False) -> None:
    global PASS, FAIL
    ok = bool(got) if truthy else (got == want)
    if ok:
        PASS += 1
        print(f"  نجح   {label}")
    else:
        FAIL += 1
        print(f"  فشل   {label}\n         حصلنا: {got!r}\n         نتوقع: {want!r}")


# ---------------------------------------------------------- التطبيع العربي

def test_arabic() -> None:
    print("\n[التطبيع العربي]")
    target = "مش عارف اعمل ايه"
    for variant in ["مش عارف أعمل إيه", "مِشْ عَارِف أعْمَل إيه",
                    "مــش عارف أعمل إيه", "مش عارف اعمل ايه"]:
        check(f"صور مختلفة تنطبق: {variant[:18]}...",
              arabic.normalize(variant), target)

    check("الأرقام العربية-الهندية", arabic.normalize("سنة ٢٠٢٦"), "سنه 2026")
    check("التاء المربوطة", arabic.normalize("مدرسة"), "مدرسه")
    check("بحث يتجاهل الرسم",
          arabic.contains("مش عارف إزّاي", "ازاي"), True)
    check("بحث سلبي صحيح",
          arabic.contains("مش عارف", "مدرسة"), False)

    # الجوهر: اختلاف الرسم وحده يجب ألا يُحسب خطأ تفريغ
    same = arabic.wer("مش عارف أعمل إيه", "مش عارف اعمل ايه")
    check("WER بين رسمين لنفس الكلام = صفر", same["wer"], 0.0)
    diff = arabic.wer("عشان انا عندي جدو تحت", "مرحبا يا عبد البر")
    check("WER بين كلامين مختلفين > 0.5", diff["wer"] > 0.5, True)


# ---------------------------------------------------------- التقاطع

def test_consensus_logic() -> None:
    print("\n[منطق تقاطع المحركات]")
    check("محرك التقاطع مسجَّل", "consensus" in T.PROVIDERS, True)

    L = T.Line
    # مأخوذ من مخرَج حقيقي: groq هلوس في البداية وفصّح الكلام
    groq = [L(0.0, "مرحباً. مرحباً. مرحباً. يا عبد البر"),
            L(8.0, "هل حياتك جميلة؟"),
            L(14.0, "عشان انا عندي جدو تحت")]
    gemini = [L(0.0, "ألو ألو"),
              L(8.0, "إزيك يا عبد البر"),
              L(14.0, "عشان أنا عندي جدو تحت")]

    low = T._agreement({"groq": groq, "gemini": gemini})
    check("اتفاق منخفض عند وجود هلوسة (< 0.7)", low < 0.7, True)

    # نفس الكلام برسم مختلف يجب أن يبدو متفقًا، لا مختلفًا
    a = [L(0.0, "عشان انا عندي جدو تحت")]
    b = [L(0.0, "عشان أنا عندى جدو تحت")]
    high = T._agreement({"a": a, "b": b})
    check("اتفاق عالٍ لنفس الكلام برسم مختلف (> 0.95)", high > 0.95, True)

    blk = T._draft_block({"groq": groq, "gemini": gemini})
    check("كتلة المسودات تسمّي كل محرك", "محرك: groq" in blk and
          "محرك: gemini" in blk, True)
    check("كتلة المسودات فيها طوابع زمنية", "[00:14]" in blk, True)


# ---------------------------------------------------------- أسماء S3

def test_names() -> None:
    print("\n[تطبيع الأسماء واستخراج المتصل]")
    messy = "تسجيل المكالمة Ko\U0001F9DC\u200D\u2640\uFE0Fk\u057E_\u0666\u0662\u0665"
    safe = S.safe_name(messy)
    check("الإيموجي يُزال", "\U0001F9DC" not in safe, True)
    check("محارف الاتجاه تُزال", "\u200D" not in safe, True)
    check("لا محارف ممنوعة في مفاتيح S3",
          not (set('<>:"/\\|?*') & set(safe)), True)

    check("المتصل من المجلد", S.caller_of("inbox/جمال Konecta/rec.m4a"),
          "جمال Konecta")
    check("المتصل من مجلد عربي", S.caller_of("inbox/زوجتي/تسجيل 12.m4a"),
          "زوجتي")
    check("ملف بلا مجلد ولا اسم", S.caller_of("inbox/rec_20260915.mp3"),
          "غير مصنف")
    check("المتصل لا يحتوي شرطة مائلة",
          "/" not in S.caller_of("inbox/a/b/c/d.m4a"), True)


# ---------------------------------------------------------- التقطيع

def test_chunking() -> None:
    print("\n[تخطيط المقاطع]")
    short = T.plan_chunks(600.0, [100.0, 200.0])
    check("تسجيل قصير = مقطع واحد", len(short), 1)

    silences = [float(x) for x in range(20, 3600, 25)]
    chunks = T.plan_chunks(3600.0, silences)
    check("ساعة تنقسم لأكثر من مقطع", len(chunks) > 1, True)
    check("المقاطع متصلة بلا فراغ",
          all(abs(chunks[i][1] - chunks[i + 1][0]) < 1e-6
              for i in range(len(chunks) - 1)), True)
    check("تغطي المدة كلها", abs(chunks[-1][1] - 3600.0) < 1e-6, True)
    check("لا مقطع يتجاوز الحد الأقصى",
          all(e - s <= T.MAX_CHUNK_SEC + 1 for s, e in chunks), True)
    # القطع يجب أن يقع عند صمت، لا عند زمن ثابت
    check("القطع عند صمت",
          all(any(abs(p - e) < 1.5 for p in silences)
              for s, e in chunks[:-1]), True)


# ---------------------------------------------------------- المحاولات

def test_retry_contract() -> None:
    print("\n[عقد إعادة المحاولة]")
    import inspect
    sig = inspect.signature(T._post_json)
    check("_post_json يقبل max_retries", "max_retries" in sig.parameters, True)
    check("الافتراضي 5 حفاظًا على ask.py",
          sig.parameters["max_retries"].default, 5)

    real404 = ("المزود رفض الطلب (404): This model models/gemini-2.5-flash-lite"
               " is no longer available to new users. Please update your code to"
               " use models/gemini-3.5-flash-lite for the latest features")
    m = T._RETIRED_REPLACEMENT.search(real404)
    check("يستخرج الموديل البديل من خطأ التقاعد",
          m.group(1) if m else None, "gemini-3.5-flash-lite")
    check("الموديل المتقاعد أُزيل من السلسلة",
          "gemini-2.5-flash-lite" not in T.DEFAULT_GEMINI_MODELS, True)


def main() -> None:
    print("اختبارات منطقية -- بلا شبكة وبلا مفاتيح")
    test_arabic()
    test_consensus_logic()
    test_names()
    test_chunking()
    test_retry_contract()
    print(f"\n{'='*52}\nنجح {PASS} / فشل {FAIL}")
    sys.exit(1 if FAIL else 0)


if __name__ == "__main__":
    main()
