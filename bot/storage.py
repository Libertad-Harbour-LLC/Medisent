"""Память беседы. Больше ничего здесь не хранится.

Раньше тут лежали ещё и задачи генерации — это было лишнее: маршрут доставки
теперь едет в самом адресе колбэка (см. services/tokens.py), и запоминать
между вызовами нечего.

Осталась только история диалога, и она опциональна:

  SqliteStorage — постоянный процесс: файл на диске, бот помнит контекст.
  NullStorage   — serverless: помнить негде, каждый вопрос отвечается отдельно.
"""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)


class Storage(ABC):
    @abstractmethod
    async def open(self) -> None: ...

    @abstractmethod
    async def close(self) -> None: ...

    @abstractmethod
    async def append_history(self, user_id: int, role: str, content: str) -> None: ...

    @abstractmethod
    async def recent_history(self, user_id: int, limit: int = 20) -> list[dict[str, str]]: ...

    @abstractmethod
    async def clear_history(self, user_id: int) -> None: ...

    @property
    def remembers(self) -> bool:
        """Помнит ли бот предыдущие реплики."""
        return True


class NullStorage(Storage):
    """Без памяти: каждый вопрос сам по себе.

    Для serverless это честный вариант по умолчанию — хранить историю там
    негде, а тащить ради неё внешнюю базу не стоит того.
    """

    async def open(self) -> None:
        log.info("память беседы выключена: каждый вопрос отвечается отдельно")

    async def close(self) -> None:
        return None

    async def append_history(self, user_id: int, role: str, content: str) -> None:
        return None

    async def recent_history(self, user_id: int, limit: int = 20) -> list[dict[str, str]]:
        return []

    async def clear_history(self, user_id: int) -> None:
        return None

    @property
    def remembers(self) -> bool:
        return False


SCHEMA = """
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
        log.info("память беседы в SQLite: %s", self._path)

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    @property
    def db(self) -> Any:
        if self._db is None:
            raise RuntimeError("SqliteStorage.open() не вызывался")
        return self._db

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


def build_storage(config: Any) -> Storage:
    """SQLite там, где есть диск; без памяти там, где его нет."""
    return NullStorage() if config.serverless else SqliteStorage(config.database_path)
