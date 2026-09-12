"""Проверка здоровья для HEALTHCHECK в Dockerfile.

Бот не слушает порт, поэтому здоровьем считается свежесть heartbeat-файла:
главный цикл трогает его каждые 30 секунд. Файл старше HEARTBEAT_MAX_AGE —
процесс жив, но завис, и контейнер надо перезапустить.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

HEARTBEAT_PATH = Path("/tmp/medisent-heartbeat")  # noqa: S108
HEARTBEAT_MAX_AGE = 180.0
HEARTBEAT_INTERVAL = 30.0


def touch() -> None:
    """Отметить, что главный цикл жив."""
    HEARTBEAT_PATH.write_text(str(time.time()), encoding="utf-8")


def age_seconds() -> float | None:
    """Сколько секунд назад была последняя отметка. None — отметок ещё не было."""
    try:
        return time.time() - HEARTBEAT_PATH.stat().st_mtime
    except FileNotFoundError:
        return None


def main() -> int:
    age = age_seconds()
    if age is None:
        print("heartbeat отсутствует")
        return 1
    if age > HEARTBEAT_MAX_AGE:
        print(f"heartbeat устарел на {age:.0f} с")
        return 1
    print(f"ok, heartbeat {age:.0f} с назад")
    return 0


if __name__ == "__main__":
    sys.exit(main())
