"""Хранение задач и истории беседы.

Две реализации под два способа жизни бота:

  SqliteStorage — постоянный процесс (локально, Railway, VPS). Файл на диске.
  RedisStorage  — serverless (Vercel). Через REST API Upstash: обычного
                  соединения там держать негде, функция умирает после ответа.

Почему это вообще важно: пользователь пишет /video, функция создаёт задачу и
завершается; через несколько минут провайдер присылает колбэк уже в другой
вызов. Если связку «задача → чат» негде хранить, результат придёт в никуда.
"""

from __future__ import annotations

import json
import logging
from abc import ABC, abstractmethod
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Задача живёт сутки: дольше ждать колбэк бессмысленно.
TASK_TTL_SECONDS = 24 * 60 * 60
HISTORY_TTL_SECONDS = 7 * 24 * 60 * 60
HISTORY_MAX_MESSAGES = 40


@dataclass(slots=True)
class Task:
    task_id: str
    kind: str
    chat_id: int
    user_id: int
    status_msg: int | None
    prompt: str


class Storage(ABC):
    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def add_task(self, task: Task) -> None: ...

    @abstractmethod
    async def take_task(self, task_id: str) -> Task | None:
        """Забирает задачу и сразу удаляет.

        Операция обязана быть атомарной: провайдер может прислать колбэк
        дважды, и отправлять пользователю файл второй раз не нужно.
        """

    @abstractmethod
    async def append_history(self, user_id: int, role: str, content: str) -> None: ...

    @abstractmethod
    async def recent_history(self, user_id: int, limit: int = 20) -> list[dict[str, str]]: ...

    @abstractmethod
    async def clear_history(self, user_id: int) -> None: ...


# --------------------------------------------------------------------------- #
# SQLite: постоянный процесс
# --------------------------------------------------------------------------- #

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    task_id     TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    chat_id     INTEGER NOT NULL,
    user_id     INTEGER NOT NULL,
    status_msg  INTEGER,
    prompt      TEXT NOT NULL,
    created_at  TEXT NOT NULL DEFAULT (datetime('now')),
    done_at     TEXT
);

CREATE TABLE IF NOT EXISTS history (
    id       INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id  INTEGER NOT NULL,
    role     TEXT NOT NULL,
    content  TEXT NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS history_user_idx ON history (user_id, id);
"""


class SqliteStorage(Storage):
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: Any = None

    async def open(self) -> None:
        import aiosqlite

        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        log.info("хранилище SQLite готово: %s", self._path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> Any:
        if self._db is None:
            raise RuntimeError("SqliteStorage.open() не вызывался")
        return self._db

    async def add_task(self, task: Task) -> None:
        await self.db.execute(
            "INSERT OR REPLACE INTO tasks"
            " (task_id, kind, chat_id, user_id, status_msg, prompt)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (task.task_id, task.kind, task.chat_id, task.user_id,
             task.status_msg, task.prompt),
        )
        await self.db.commit()

    async def take_task(self, task_id: str) -> Task | None:
        async with self.db.execute(
            "SELECT task_id, kind, chat_id, user_id, status_msg, prompt"
            " FROM tasks WHERE task_id = ? AND done_at IS NULL",
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None

        await self.db.execute(
            "UPDATE tasks SET done_at = datetime('now') WHERE task_id = ?", (task_id,)
        )
        await self.db.commit()

        return Task(
            task_id=row["task_id"],
            kind=row["kind"],
            chat_id=row["chat_id"],
            user_id=row["user_id"],
            status_msg=row["status_msg"],
            prompt=row["prompt"],
        )

    async def append_history(self, user_id: int, role: str, content: str) -> None:
        await self.db.execute(
            "INSERT INTO history (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        await self.db.commit()

    async def recent_history(self, user_id: int, limit: int = 20) -> list[dict[str, str]]:
        async with self.db.execute(
            "SELECT role, content FROM history WHERE user_id = ? ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()

        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    async def clear_history(self, user_id: int) -> None:
        await self.db.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
        await self.db.commit()


# --------------------------------------------------------------------------- #
# Redis через REST: serverless
# --------------------------------------------------------------------------- #


class RedisStorage(Storage):
    """Upstash Redis поверх HTTP.

    Именно REST, а не обычный клиент: держать TCP-соединение между вызовами
    функции невозможно, а переподключаться на каждый запрос дороже, чем один
    HTTP-вызов.
    """

    def __init__(self, url: str, token: str, *, timeout: float = 10.0) -> None:
        self._url = url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def open(self) -> None:
        self._client = httpx.AsyncClient(
            base_url=self._url,
            timeout=httpx.Timeout(self._timeout, connect=5.0),
            headers={"Authorization": f"Bearer {self._token}"},
        )
        log.info("хранилище Redis готово")

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _command(self, *args: Any) -> Any:
        if self._client is None:
            await self.open()
        assert self._client is not None

        response = await self._client.post("/", json=[str(arg) for arg in args])
        response.raise_for_status()
        body = response.json()

        if isinstance(body, dict) and body.get("error"):
            raise RuntimeError(f"Redis: {body['error']}")
        return body.get("result") if isinstance(body, dict) else body

    async def add_task(self, task: Task) -> None:
        await self._command(
            "SET", f"task:{task.task_id}", json.dumps(asdict(task)), "EX", TASK_TTL_SECONDS
        )

    async def take_task(self, task_id: str) -> Task | None:
        # GETDEL читает и удаляет одной операцией — повторный колбэк уже ничего
        # не найдёт и файл не уйдёт дважды.
        raw = await self._command("GETDEL", f"task:{task_id}")
        if not raw:
            return None
        return Task(**json.loads(raw))

    async def append_history(self, user_id: int, role: str, content: str) -> None:
        key = f"hist:{user_id}"
        await self._command("RPUSH", key, json.dumps({"role": role, "content": content}))
        await self._command("LTRIM", key, -HISTORY_MAX_MESSAGES, -1)
        await self._command("EXPIRE", key, HISTORY_TTL_SECONDS)

    async def recent_history(self, user_id: int, limit: int = 20) -> list[dict[str, str]]:
        items = await self._command("LRANGE", f"hist:{user_id}", -limit, -1)
        if not isinstance(items, list):
            return []
        return [json.loads(item) for item in items]

    async def clear_history(self, user_id: int) -> None:
        await self._command("DEL", f"hist:{user_id}")


def build_storage(config: Any) -> Storage:
    """Выбирает хранилище по окружению.

    Redis выигрывает, если настроен: на Vercel файл на диске не переживёт
    вызова функции, а локально Redis тоже не мешает.
    """
    if config.redis_enabled:
        return RedisStorage(config.redis_url, config.redis_token)

    if config.serverless:
        raise RuntimeError(
            "На Vercel обязательно внешнее хранилище: подключите Upstash Redis "
            "в Storage и задайте KV_REST_API_URL и KV_REST_API_TOKEN. "
            "Иначе результат генерации придёт в никуда."
        )

    return SqliteStorage(config.database_path)
