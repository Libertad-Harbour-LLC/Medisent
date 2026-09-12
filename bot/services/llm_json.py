"""Разбор JSON из ответа модели.

Модели оборачивают JSON в ```json … ``` вопреки инструкции, а иногда
добавляют прозу вокруг. Один разборщик на все сервисы: правка здесь доходит
до каждого вызова, а не до того, где о ней вспомнили.
"""

from __future__ import annotations

import json
import re
from typing import Any

FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


def parse_llm_json(text: str) -> Any | None:
    """Текст ответа → разобранный JSON. ``None`` — не разобралось."""
    body = (text or "").strip()
    fenced = FENCE_RE.search(body)
    if fenced:
        body = fenced.group(1)
    try:
        return json.loads(body)
    except json.JSONDecodeError:
        return None
