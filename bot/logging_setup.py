"""Логи в файл с ротацией плюс дублирование в stdout.

Требование ТЗ: по каждой заявке должно быть видно, что спросили у каждого
источника и что он ответил. Поэтому у записей есть поле request_id, а сообщения
внешних сервисов пишутся полностью.
"""

from __future__ import annotations

import logging
import logging.handlers
import sys
from pathlib import Path
from typing import Any

_REQUEST_ID_DEFAULT = "-"


class RequestIdFilter(logging.Filter):
    """Подставляет request_id, если вызывающий его не передал."""

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = _REQUEST_ID_DEFAULT
        return True


def setup_logging(level: str = "INFO", log_dir: str | Path = "logs") -> None:
    """Настраивает корневой логгер. Вызывается один раз при старте."""
    directory = Path(log_dir)
    directory.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s [%(request_id)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(level.upper())
    root.handlers.clear()

    # Ротация по размеру: 10 МБ на файл, 14 файлов — столько же, сколько
    # дампов базы держим по ТЗ.
    file_handler = logging.handlers.RotatingFileHandler(
        directory / "bot.log", maxBytes=10 * 1024 * 1024, backupCount=14, encoding="utf-8"
    )
    file_handler.setFormatter(fmt)
    file_handler.addFilter(RequestIdFilter())
    root.addHandler(file_handler)

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(fmt)
    stream_handler.addFilter(RequestIdFilter())
    root.addHandler(stream_handler)

    # aiogram и httpx на INFO слишком болтливы.
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)


def log_extra(request_id: int | str | None) -> dict[str, Any]:
    """Готовит extra= для logger.*, чтобы в строке был номер заявки."""
    return {"request_id": str(request_id) if request_id is not None else _REQUEST_ID_DEFAULT}
