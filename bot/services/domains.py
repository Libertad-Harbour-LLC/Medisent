"""Нормализация ключей дедупликации поставщиков: домен и ИНН.

Чистые функции без базы. Ими пользуются и ``repo`` (перед записью), и
поиск (при разборе выдачи): ключ обязан считаться одинаково в обоих местах,
иначе две строки, схлопнувшиеся в один ``lower(domain)``, попадают в один
``INSERT … ON CONFLICT`` и Postgres отвергает весь пакет.
"""

from __future__ import annotations


def normalise_domain(value: str | None) -> str | None:
    """``https://WWW.Example.RU/catalog?x=1#top`` → ``example.ru``.

    Уникальный индекс стоит на ``lower(domain)``, но срезать схему, ``www.``,
    путь, параметры, якорь и точку в конце база за нас не станет.
    """
    if not value:
        return None
    cleaned = value.strip().lower()
    for prefix in ("https://", "http://"):
        if cleaned.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
    cleaned = cleaned.split("/", 1)[0].split("?", 1)[0].split("#", 1)[0]
    if cleaned.startswith("www."):
        cleaned = cleaned[4:]
    cleaned = cleaned.rstrip(".")
    return cleaned or None


def normalise_tax_id(value: str | None) -> str | None:
    """ИНН — только цифры. 10 знаков у юрлица, 12 у ИП; иное отбрасываем."""
    if not value:
        return None
    digits = "".join(ch for ch in value if ch.isdigit())
    return digits if len(digits) in (10, 12) else None
