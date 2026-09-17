# Egypt Deals — GitHub Runner

نسخة تشغيل عامة منزوعة الأسرار، مصممة للعمل مع Cloudflare Worker/D1 الحاليين.

- الخصم المؤهل يبدأ من 5%.
- V11 يشغّل المتاجر غير Amazon مرة واحدة في كل دورة.
- Amazon يعمل بدورة محدودة ويحفظ تقدّم الـwatchlist بين تشغيلات GitHub Actions.
- المراجعة/Approve/Reject والنشر تتم عبر Cloudflare + Telegram webhook الموجودين بالفعل.
- لا توجد Telegram tokens أو Cloudflare API keys داخل المستودع؛ القيم تدخل كـGitHub Actions Secrets فقط.

## Secrets المطلوبة
- `CLOUD_API_URL`
- `CLOUD_API_KEY`

## الجدولة
كل 10 دقائق، ويمكن تشغيل Workflow يدويًا من Actions.
