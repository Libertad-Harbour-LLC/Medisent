"""Разбор хвостовых ключей в командах: /img кот --ar 16:9 --res 2K."""

from __future__ import annotations

import re
from dataclasses import dataclass

FLAG_RE = re.compile(r"--([a-z]+)(?:[ =]+([^\s-][^\s]*))?", re.IGNORECASE)


class ArgError(ValueError):
    """Пользователь передал ключ с недопустимым значением."""


@dataclass(slots=True)
class ParsedCommand:
    prompt: str
    flags: dict[str, str]

    def choice(self, key: str, allowed: tuple[str, ...], default: str) -> str:
        raw = self.flags.get(key)
        if raw is None:
            return default
        for option in allowed:
            if raw.lower() == option.lower():
                return option
        raise ArgError(f"{key}:{raw}")

    def flag(self, key: str) -> bool:
        return key in self.flags

    def integer(self, key: str, default: int, lo: int, hi: int) -> int:
        raw = self.flags.get(key)
        if raw is None:
            return default
        try:
            value = int(raw)
        except ValueError as exc:
            raise ArgError(f"{key}:{raw}") from exc
        if not lo <= value <= hi:
            raise ArgError(f"{key}:{raw}")
        return value


def parse(text: str) -> ParsedCommand:
    """Отделяет описание от ключей.

    Ключи ищутся во всей строке, но в промпт попадает только то, что до первого
    ключа — иначе «--» внутри описания резал бы текст пополам.
    """
    flags: dict[str, str] = {}
    first_flag_at = len(text)

    for match in FLAG_RE.finditer(text):
        first_flag_at = min(first_flag_at, match.start())
        flags[match.group(1).lower()] = (match.group(2) or "").strip()

    return ParsedCommand(prompt=text[:first_flag_at].strip(), flags=flags)
