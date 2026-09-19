#!/usr/bin/env python3
"""Регистрация вебхука Telegram — один раз после деплоя на Vercel.

    python scripts/setup_webhook.py            показать текущее состояние
    python scripts/setup_webhook.py --set      зарегистрировать
    python scripts/setup_webhook.py --delete   снять (например, чтобы
                                               запустить поллинг локально)

Читает те же переменные окружения, что и бот. PUBLIC_URL на Vercel
подставляется сам из домена проекта.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.config import load_config  # noqa: E402


def call(token: str, method: str, **params: object) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(
        {k: v for k, v in params.items() if v is not None}
    ).encode()
    with urllib.request.urlopen(url, data=data or None, timeout=20) as response:
        return json.loads(response.read())


def main() -> int:
    parser = argparse.ArgumentParser(description="Вебхук Telegram")
    parser.add_argument("--set", action="store_true", help="зарегистрировать вебхук")
    parser.add_argument("--delete", action="store_true", help="снять вебхук")
    args = parser.parse_args()

    config = load_config()
    token = config.telegram_token

    if args.delete:
        print(json.dumps(call(token, "deleteWebhook", drop_pending_updates="true"),
                         ensure_ascii=False, indent=2))
        return 0

    if args.set:
        if not config.public_url:
            print("Нет PUBLIC_URL (или VERCEL_PROJECT_PRODUCTION_URL).", file=sys.stderr)
            return 1
        if not config.telegram_webhook_secret:
            print("Нет TELEGRAM_WEBHOOK_SECRET.", file=sys.stderr)
            return 1

        result = call(
            token,
            "setWebhook",
            url=config.telegram_webhook_url,
            secret_token=config.telegram_webhook_secret,
            drop_pending_updates="true",
            allowed_updates=json.dumps(["message"]),
        )
        print(f"Адрес: {config.telegram_webhook_url}")
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0 if result.get("ok") else 1

    info = call(token, "getWebhookInfo")
    print(json.dumps(info, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
