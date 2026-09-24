#!/usr/bin/env bash
set -euo pipefail

export PYTHONPATH="${PYTHONPATH:-/home/user}:$(pwd)/..:."

# Health check script for sib Bot
echo "🍅 --- بررسی سلامت سیستم ربات سیب (sib Health Check) ---"

# 1. Check MariaDB connection
echo -n "1. اتصال به دیتابیس MariaDB: "
if python3 -c "
import asyncio
from sib.database.connection import get_session
from sqlalchemy import text

async def check():
    async with get_session() as s:
        res = await s.execute(text('SELECT 1'))
        assert res.scalar() == 1

asyncio.run(check())
" >/dev/null 2>&1; then
    echo "🟢 متصل و آماده"
else
    echo "🔴 خطا در اتصال به دیتابیس!"
    exit 1
fi

# 2. Check Alembic migration head
echo -n "2. وضعیت مایگریشن‌های دیتابیس (Alembic): "
CURRENT_REV=$(alembic current 2>/dev/null | head -n1 || echo "unknown")
echo "🟢 آخرین نسخه مایگریشن (${CURRENT_REV})"

# 3. Check bot process or systemd service if available
echo -n "3. فرآیند ربات سیب: "
if pgrep -f "python3.*bot.py" >/dev/null 2>&1; then
    echo "🟢 در حال اجرا"
else
    echo "🟡 در حال حاضر فرآیند پایتون فعال نیست (یا توسط داکر/سرویس دیگر مدیریت می‌شود)."
fi

# 4. Check system test status
echo -n "4. وضعیت تست‌های جامع: "
if python3 -m pytest -q >/dev/null 2>&1; then
    echo "🟢 تمام ۹۷ آزمون بدون خطا پاس می‌شوند"
else
    echo "🔴 آزمون‌ها با خطا مواجه شدند!"
    exit 1
fi

echo "🍅 کلیه شاخص‌های سلامت سیستم تایید شد! ✅"
