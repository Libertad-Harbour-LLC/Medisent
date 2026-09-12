"""Общие фикстуры. Внешние API в тестах не вызываются никогда."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# Настройки должны существовать до импорта любого модуля бота.
os.environ.setdefault("TELEGRAM_BOT_TOKEN", "111:test-token")
os.environ.setdefault("TELEGRAM_OWNER_ID", "42")
os.environ.setdefault("DATABASE_URL", "postgresql+asyncpg://test:test@localhost/test")
os.environ.setdefault("LOG_DIR", "/tmp/medisent-test-logs")  # noqa: S108


def load_fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def load_json_fixture(name: str) -> Any:
    return json.loads(load_fixture(name))


@pytest.fixture(autouse=True)
def _no_metering() -> Iterator[None]:
    """Учёт расходов в тестах пишется в никуда, а не в базу."""
    from bot.services import http

    calls: list[dict[str, Any]] = []

    async def _fake_meter(**kwargs: Any) -> None:
        calls.append(kwargs)

    http.set_meter(_fake_meter)
    yield
    http.set_meter(None)


@pytest.fixture
def metered() -> list[dict[str, Any]]:
    """Список записанных расходов — для тестов, которые их проверяют."""
    from bot.services import http

    calls: list[dict[str, Any]] = []

    async def _fake_meter(**kwargs: Any) -> None:
        calls.append(kwargs)

    http.set_meter(_fake_meter)
    return calls
