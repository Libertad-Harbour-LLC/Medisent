"""Тесты хранилища на Redis — того, что работает на Vercel.

Сетевой слой подменён: проверяется, что команды формируются правильно и что
задача забирается ровно один раз. Второе критично — на GETDEL держится защита
от повторного колбэка, а значит от повторной отправки файла пользователю.
"""

from __future__ import annotations

import json

import pytest

from bot.config import Config, load_config
from bot.storage import RedisStorage, SqliteStorage, Task, build_storage


class FakeRedis(RedisStorage):
    """Подменяет транспорт, оставляя логику команд настоящей."""

    def __init__(self) -> None:
        super().__init__("https://redis.test", "token")
        self.calls: list[tuple[str, ...]] = []
        self.data: dict[str, object] = {}

    async def _command(self, *args):  # type: ignore[override]
        command = tuple(str(arg) for arg in args)
        self.calls.append(command)
        name = command[0].upper()

        match name:
            case "SET":
                self.data[command[1]] = command[2]
                return "OK"
            case "GETDEL":
                return self.data.pop(command[1], None)
            case "RPUSH":
                self.data.setdefault(command[1], []).append(command[2])  # type: ignore[union-attr]
                return len(self.data[command[1]])  # type: ignore[arg-type]
            case "LRANGE":
                return list(self.data.get(command[1], []))  # type: ignore[arg-type]
            case "DEL":
                self.data.pop(command[1], None)
                return 1
            case _:
                return "OK"


@pytest.fixture
def redis() -> FakeRedis:
    return FakeRedis()


@pytest.mark.asyncio
async def test_task_round_trip(redis: FakeRedis) -> None:
    await redis.add_task(Task("t1", "video", chat_id=7, user_id=1, status_msg=5, prompt="кот"))
    task = await redis.take_task("t1")

    assert task is not None
    assert (task.chat_id, task.kind, task.status_msg) == (7, "video", 5)


@pytest.mark.asyncio
async def test_task_is_taken_only_once(redis: FakeRedis) -> None:
    """Повторный колбэк не должен приводить ко второй отправке файла."""
    await redis.add_task(Task("t2", "image", chat_id=1, user_id=1, status_msg=None, prompt="x"))

    assert await redis.take_task("t2") is not None
    assert await redis.take_task("t2") is None


@pytest.mark.asyncio
async def test_task_has_expiry(redis: FakeRedis) -> None:
    """Без TTL мёртвые задачи копились бы вечно."""
    await redis.add_task(Task("t3", "image", chat_id=1, user_id=1, status_msg=None, prompt="x"))

    set_call = next(call for call in redis.calls if call[0] == "SET")
    assert "EX" in set_call


@pytest.mark.asyncio
async def test_missing_task_returns_none(redis: FakeRedis) -> None:
    assert await redis.take_task("нет-такой") is None


@pytest.mark.asyncio
async def test_history_round_trip(redis: FakeRedis) -> None:
    await redis.append_history(42, "user", "привет")
    await redis.append_history(42, "assistant", "здравствуйте")

    history = await redis.recent_history(42)
    assert history == [
        {"role": "user", "content": "привет"},
        {"role": "assistant", "content": "здравствуйте"},
    ]


@pytest.mark.asyncio
async def test_history_is_trimmed_and_expires(redis: FakeRedis) -> None:
    await redis.append_history(1, "user", "x")
    names = [call[0] for call in redis.calls]
    assert "LTRIM" in names and "EXPIRE" in names


def _config(**overrides) -> Config:
    base = dict(
        telegram_token="t",
        allowed_user_ids=frozenset(),
        kie_api_key="k",
        kie_base_url="https://api.kie.ai",
        chat_model="claude-sonnet-5",
        image_model="img",
        video_model="vid",
        public_url="",
        callback_secret="",
        telegram_webhook_secret="",
        web_host="0.0.0.0",
        web_port=8080,
        database_path="data/t.sqlite3",
        redis_url="",
        redis_token="",
        log_level="INFO",
        request_timeout=60.0,
        max_reply_tokens=4096,
        user_rate_limit_per_minute=6,
    )
    base.update(overrides)
    return Config(**base)


class TestStorageChoice:
    def test_redis_wins_when_configured(self) -> None:
        config = _config(redis_url="https://redis.test", redis_token="tok")
        assert isinstance(build_storage(config), RedisStorage)

    def test_sqlite_for_plain_process(self) -> None:
        assert isinstance(build_storage(_config()), SqliteStorage)

    def test_serverless_without_redis_fails_loudly(self, monkeypatch) -> None:
        """Молчаливый SQLite на Vercel — потерянные результаты генерации."""
        monkeypatch.setenv("VERCEL", "1")
        config = _config()

        with pytest.raises(RuntimeError, match="Upstash"):
            build_storage(config)


class TestPublicUrl:
    def test_vercel_domain_is_picked_up(self, monkeypatch) -> None:
        monkeypatch.delenv("PUBLIC_URL", raising=False)
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setenv("KIE_API_KEY", "k")
        monkeypatch.setenv("VERCEL_PROJECT_PRODUCTION_URL", "bot.vercel.app")

        assert load_config().public_url == "https://bot.vercel.app"

    def test_explicit_url_wins(self, monkeypatch) -> None:
        monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
        monkeypatch.setenv("KIE_API_KEY", "k")
        monkeypatch.setenv("PUBLIC_URL", "https://own.example/")
        monkeypatch.setenv("VERCEL_URL", "bot.vercel.app")

        assert load_config().public_url == "https://own.example"
