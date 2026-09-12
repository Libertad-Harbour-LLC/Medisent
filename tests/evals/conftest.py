"""Общее для оценочных наборов."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

CASES = Path(__file__).parent / "cases"


def load_case(name: str) -> dict[str, Any]:
    """Записанный ответ модели вместе с тем, что ей давали на вход."""
    data: dict[str, Any] = json.loads((CASES / f"{name}.json").read_text(encoding="utf-8"))
    return data
