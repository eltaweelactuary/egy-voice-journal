# صورة العامل -- ffmpeg + بايثون + كود المستودع.
#
# لا نضع أي مفتاح API هنا: العامل يقرأها من SSM Parameter Store وقت التشغيل.
# لو أضفت مفتاحًا في الصورة فهو يبقى في طبقاتها ويصل لكل من يسحبها من ECR.

FROM python:3.12-slim

# ffmpeg هو حجر الأساس: الضغط إلى 16kHz mono opus (~11-12 ميجا/ساعة) هو ما
# يُدخل ملفًا من 100 ميجا تحت حدود الطبقات المجانية بلا خسارة دقة تفريغ.
RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt boto3

COPY transcribe.py s3_pipeline.py arabic.py ./
COPY aws/worker.py ./aws/worker.py

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    WORKDIR=/tmp/journal

# مستخدم غير جذري -- لا داعي لصلاحيات إضافية داخل الحاوية
RUN useradd --create-home --uid 10001 worker \
 && mkdir -p /tmp/journal && chown -R worker /tmp/journal /app
USER worker

ENTRYPOINT ["python", "-u", "aws/worker.py"]
