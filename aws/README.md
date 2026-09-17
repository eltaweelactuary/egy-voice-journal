# النشر على AWS — خط أنابيب أوتوماتيكي

ترفع تسجيلًا (من الموبايل أو الجهاز) ← يُفرَّغ تلقائيًا ← تجد المخرجات جاهزة.
بلا تشغيل يدوي، وبلا سيرفر يعمل بلا داعٍ.

---

## من أين تأتي التكلفة بالضبط

سؤال مهم، وجوابه ليس «AWS» بشكل عام. **مصدر واحد فقط يكلّف فعلًا: زمن حوسبة
Fargate.** كل ما عداه يقع داخل حصص مجانية أو يكلّف كسور السنت:

| البند | الحصة المجانية | تكلفتك |
|---|---|---|
| Lambda (المُشغِّل) | مليون استدعاء شهريًا — **دائمة، لا تنتهي** | صفر |
| SQS (الطابور + DLQ) | أول مليون طلب شهريًا — **دائمة** | صفر |
| EventBridge | أحداث خدمات AWS مجانية | صفر |
| SSM Parameter Store (قياسي) | مجاني | صفر |
| S3 تخزين | ٥ جيجا (أول ١٢ شهرًا) | سنتات — تفريغك بالكيلوبايت |
| S3 طلبات | — | كسور السنت |
| CloudWatch Logs | ٥ جيجا | صفر تقريبًا |
| SNS + Budget | مجاني في هذا الحجم | صفر |
| **Fargate** | **لا حصة مجانية إطلاقًا** | **كل الفاتورة تقريبًا** |

Fargate بـ ١ vCPU و٤ جيجا ≈ **٠٫٠٥٨ دولار للساعة**. عشرون ساعة صوت شهريًا
تحتاج ساعة إلى ساعتين من زمن المهمة (ffmpeg + انتظار الواجهات)، أي
**٦ إلى ١٢ سنتًا شهريًا**.

**فالفاتورة الكلية أقل من دولار شهريًا.** والتفريغ نفسه صفر لأن Gemini وGroq
في الطبقة المجانية — فنعم، **كل ما تدفعه هو AWS، وكل ما تدفعه لـ AWS تقريبًا
هو Fargate.**

### عيب كان في المخطط، وسؤالك كشفه

المخطط الأول كان يشغّل مهمة Fargate **كل ١٥ دقيقة** كشبكة أمان. الحساب:

```
96 تشغيل/يوم × 30 يومًا × ~45ث (بدء ثم خروج فوري لأن الطابور فارغ)
≈ 36 ساعة مهمة شهريًا ≈ ~2 دولار شهريًا مدفوعة مقابل لا شيء
```

أي أن **تكلفة الخمول كانت ستتجاوز تكلفة العمل الحقيقي بعشرين ضعفًا.**

الإصلاح المنفَّذ: Lambda تفحص عمق الطابور أولًا، ولا تشغّل Fargate إلا إن وُجد
عمل فعلي. استدعاء Lambda وقراءة SQS داخل الحصة المجانية الدائمة، فصارت
**تكلفة الخمول صفرًا**، وخُفِّض الجدول إلى كل ساعة لأنه لم يبقَ إلا شبكة أمان.
وحُوِّلت الأسرار من Secrets Manager (٠٫٤٠ دولار/سرّ/شهر) إلى Parameter Store
القياسي (مجاني).

---

## النشر — خمس خطوات

### ١) الأسرار (مرة واحدة)

Parameter Store لا يُنشأ من القالب لأن CloudFormation لا يدعم `SecureString`:

```bash
aws ssm put-parameter --name /journal/GEMINI_API_KEY --type SecureString --value "..." --overwrite
aws ssm put-parameter --name /journal/GROQ_API_KEY   --type SecureString --value "..." --overwrite
```

### ٢) بناء الصورة ورفعها إلى ECR

```bash
ACCOUNT=$(aws sts get-caller-identity --query Account --output text)
REGION=us-east-1
REPO=$ACCOUNT.dkr.ecr.$REGION.amazonaws.com/journal-worker

aws ecr create-repository --repository-name journal-worker --region $REGION || true
aws ecr get-login-password --region $REGION | docker login --username AWS --password-stdin $REPO

docker build -f aws/Dockerfile -t journal-worker .
docker tag journal-worker:latest $REPO:latest
docker push $REPO:latest
```

### ٣) شبكة Fargate

```bash
VPC=$(aws ec2 describe-vpcs --filters Name=isDefault,Values=true --query 'Vpcs[0].VpcId' --output text)
SUBNETS=$(aws ec2 describe-subnets --filters Name=vpc-id,Values=$VPC --query 'Subnets[].SubnetId' --output text | tr '\t' ',')
SG=$(aws ec2 describe-security-groups --filters Name=vpc-id,Values=$VPC Name=group-name,Values=default --query 'SecurityGroups[0].GroupId' --output text)
```

### ٤) النشر

```bash
aws cloudformation deploy \
  --stack-name journal \
  --template-file aws/template.yaml \
  --capabilities CAPABILITY_IAM \
  --parameter-overrides \
      Subnets="$SUBNETS" \
      SecurityGroupId="$SG" \
      ImageUri="$REPO:latest" \
      NotifyEmail="بريدك@example.com" \
      ProviderOrder="gemini,groq" \
      FallbackPolicy="degrade"
```

أكّد اشتراك البريد من الرسالة التي تصلك، وإلا لن تستقبل التنبيهات.

### ٥) التجربة

```bash
BUCKET=$(aws cloudformation describe-stacks --stack-name journal \
  --query 'Stacks[0].Outputs[?OutputKey==`BucketName`].OutputValue' --output text)

aws s3 cp "تسجيل.m4a" "s3://$BUCKET/inbox/جمال Konecta/"
aws logs tail /ecs/journal-worker --follow
```

خلال دقيقة أو دقيقتين يجب أن ترى العامل يبدأ، ثم:

```bash
aws s3 ls "s3://$BUCKET/journal/جمال Konecta/"
aws s3 cp "s3://$BUCKET/index.json" -
```

---

## الرفع اليومي

**دفعة من الجهاز — أمر واحد:**

```bash
aws s3 sync "مجلد التسجيلات" "s3://$BUCKET/inbox/جمال Konecta/" \
  --exclude "*" --include "*.m4a" --include "*.mp3"
```

**من الموبايل:** تطبيق AWS Console للموبايل يرفع إلى S3 اليوم بلا أي بناء —
استخدمه أولًا. لتجربة أفضل (مشاركة مباشرة من مسجّل الصوت) ابنِ نقطة presigned
كما في `DESIGN_AUTO_PIPELINE.md` المرحلة ١. **لا تضع مفتاح AWS دائمًا على
الموبايل** بأي حال.

اسم المجلد بعد `inbox/` هو اسم المتصل، وعليه تُجمَّع الملخصات.

---

## المخرجات

| المسار | ما هو |
|---|---|
| `journal/<متصل>/*.json` | التفريغ الكامل — كل سطر بنصه الخام و`normalized` بجانبه |
| `journal/<متصل>/*.md,.txt,.srt` | للقراءة والاستماع |
| `summaries/<متصل>.md` | كل تسجيلات المتصل في مستند واحد مرتّب زمنيًا |
| `index.json` | **الفهرس** — الملف الوحيد الذي تقرأه أي عملية لاحقة |
| `processed/` | الصوت الخام، يُحذف تلقائيًا بعد ٣٠ يومًا |

النص الخام يبقى بعاميّتك حرفيًا ولا يُمسّ. `normalized` للبحث فقط: بدونه
البحث عن «إزاي» لا يجد «ازاي» ويفشل بصمت.

---

## ملاحظات تشغيلية

- **التتابع مفروض بالتصميم:** عامل واحد في أي لحظة. التوازي يفجّر حد Gemini
  المجاني (١٠ طلبات/دقيقة) — وقد قيس فعلًا: ملف واحد استهلك ١٠١٢ ثانية بسبب
  503 متكرر. رفع دفعة كبيرة آمن: تُعالج بالتتابع.
- **سقف يومي:** ٢٤٠ طلب Gemini، محفوظ في `state/quota-YYYY-MM-DD.json`. عند
  النفاد يتوقف العامل بهدوء ويترك الرسائل للغد بلا حرقها.
- **`FallbackPolicy=degrade`** (الافتراضي): إن ازدحم Gemini يسقط لـ Groq
  فورًا. Groq أسرع وأقل دقة — يُفصّح ويهلوس أحيانًا. غيّرها إلى `requeue` لو
  فضّلت الانتظار على تفريغ أقل دقة.
- **إعادة الرفع آمنة:** الملف المُفرَّغ سابقًا يُتخطّى، بلا تكلفة إضافية.
- **الحاوية `Retain`:** حذف الستاك لا يحذف تسجيلاتك.
- **الفشل:** بعد ٣ محاولات تذهب الرسالة للـ DLQ ويصلك تنبيه.

الأسعار تتغير بالمنطقة والوقت — تحقّق من أرقامك عبر
[حاسبة AWS](https://calculator.aws/) و[صفحة أسعار Fargate](https://aws.amazon.com/fargate/pricing/).
