#!/bin/sh
# راه‌انداز روی هاست رایگان: نصب وابستگی‌ها و اجرای سرور
pip install -q --no-cache-dir -r requirements.txt
exec uvicorn app.main:app --host 0.0.0.0 --port "${PORT:-8000}" --proxy-headers --forwarded-allow-ips="*"
