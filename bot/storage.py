"""Хранение задач между запросами.

Видео делается минутами, за это время бот может перезапуститься. Держать
соответствие «задача → чат» в памяти нельзя: после рестарта колбэк придёт
в пустоту. Поэтому SQLite.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import aiosqlite

log = logging.getLogger(__name__)

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


@dataclass(slots=True)
class Task:
    task_id: str
    kind: str
    chat_id: int
    user_id: int
    status_msg: int | None
    prompt: str


class Storage:
    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    async def open(self) -> None:
        Path(self._path).parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.executescript(SCHEMA)
        await self._db.commit()
        log.info("хранилище готово: %s", self._path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("Storage.open() не вызывался")
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
        """Забирает задачу и помечает выполненной.

        Помечаем в той же транзакции, что и чтение: провайдер может прислать
        колбэк дважды, и второй раз отправлять пользователю файл не нужно.
        """
        async with self.db.execute(
            "SELECT task_id, kind, chat_id, user_id, status_msg, prompt"
            " FROM tasks WHERE task_id = ? AND done_at IS NULL",
            (task_id,),
        ) as cursor:
            row = await cursor.fetchone()

        if row is None:
            return None

        await self.db.execute(
            "UPDATE tasks SET done_at = datetime('now') WHERE task_id = ?",
            (task_id,),
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
            "SELECT role, content FROM history WHERE user_id = ?"
            " ORDER BY id DESC LIMIT ?",
            (user_id, limit),
        ) as cursor:
            rows = await cursor.fetchall()

        return [{"role": row["role"], "content": row["content"]} for row in reversed(rows)]

    async def clear_history(self, user_id: int) -> None:
        await self.db.execute("DELETE FROM history WHERE user_id = ?", (user_id,))
        await self.db.commit()
