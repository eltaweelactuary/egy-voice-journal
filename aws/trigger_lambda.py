"""
Lambda المُشغِّل -- لا تعالج شيئًا، فقط تقرّر: هل نشغّل عاملًا؟

هذه الدالة هي إصلاح عيب التكلفة. المخطط الأول كان يشغّل مهمة Fargate كل ١٥
دقيقة كشبكة أمان، فيكون الحساب:

    96 تشغيل/يوم × 30 يومًا × ~45ث (بدء + خروج فوري) ≈ 36 ساعة مهمة شهريًا
    ≈ دولاران شهريًا مدفوعة مقابل **لا شيء** -- مهام تستيقظ فتجد الطابور
      فارغًا فتخرج.

الإصلاح: تسأل Lambda عن عمق الطابور أولًا. استدعاء Lambda وقراءة SQS داخل
الحصة المجانية الدائمة، فالخمول يكلّف **صفرًا** فعليًا، ولا تعمل Fargate إلا
حين يوجد عمل حقيقي.

وتفرض قاعدة التتابع: إن كان عامل يعمل الآن فلا تشغّل ثانيًا. نسخة واحدة فقط
في أي لحظة -- التوازي يفجّر حدود الطبقة المجانية للمزوّدين.
"""

from __future__ import annotations

import json
import os

import boto3

CLUSTER = os.environ["CLUSTER"]
TASK_DEF = os.environ["TASK_DEF"]
TASK_FAMILY = os.environ.get("TASK_FAMILY", "")
QUEUE_URL = os.environ["QUEUE_URL"]
SUBNETS = [s for s in os.environ.get("SUBNETS", "").split(",") if s]
SECURITY_GROUPS = [g for g in os.environ.get("SECURITY_GROUPS", "").split(",") if g]
CONTAINER_NAME = os.environ.get("CONTAINER_NAME", "worker")

ecs = boto3.client("ecs")
sqs = boto3.client("sqs")


def queue_depth() -> int:
    attrs = sqs.get_queue_attributes(
        QueueUrl=QUEUE_URL,
        AttributeNames=["ApproximateNumberOfMessages",
                        "ApproximateNumberOfMessagesNotVisible"],
    )["Attributes"]
    return (int(attrs.get("ApproximateNumberOfMessages", 0))
            + int(attrs.get("ApproximateNumberOfMessagesNotVisible", 0)))


def worker_running() -> int:
    """
    يعدّ مهام هذه العائلة فقط -- في الحالتين RUNNING و PENDING.

    الفلترة بالعائلة مهمة: بدونها نعدّ أي مهمة أخرى في العنقود فنمتنع عن
    التشغيل بلا سبب.
    """
    total = 0
    for desired in ("RUNNING", "PENDING"):
        kw = {"cluster": CLUSTER, "desiredStatus": desired}
        if TASK_FAMILY:
            kw["family"] = TASK_FAMILY
        total += len(ecs.list_tasks(**kw).get("taskArns", []))
    return total


def handler(event, context):
    depth = queue_depth()
    if depth == 0:
        return _result("skipped", "الطابور فارغ -- لا تشغيل، ولا تكلفة", depth)

    running = worker_running()
    if running > 0:
        return _result("skipped",
                       f"عامل يعمل بالفعل ({running}) -- التتابع مفروض", depth)

    resp = ecs.run_task(
        cluster=CLUSTER,
        taskDefinition=TASK_DEF,
        count=1,                                  # واحد فقط، دائمًا
        launchType="FARGATE",
        networkConfiguration={"awsvpcConfiguration": {
            "subnets": SUBNETS,
            "securityGroups": SECURITY_GROUPS,
            "assignPublicIp": "ENABLED",          # لازم للوصول لواجهات المزوّدين
        }},
    )
    failures = resp.get("failures") or []
    if failures:
        return _result("failed", json.dumps(failures, default=str)[:400], depth)

    arn = (resp.get("tasks") or [{}])[0].get("taskArn", "")
    return _result("launched", arn.rsplit("/", 1)[-1], depth)


def _result(status: str, detail: str, depth: int) -> dict:
    out = {"status": status, "detail": detail, "queue_depth": depth}
    print(json.dumps(out, ensure_ascii=False))
    return out
