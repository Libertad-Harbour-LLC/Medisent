"""Контакты поставщика: что считать рабочим e-mail.

Одно правило для скрейпа и для выдачи поиска. Раньше у них были разные
регулярные выражения и разный фильтр: ``noreply@`` с сайта отбрасывался,
а тот же адрес из выдачи поиска попадал в базу как контакт.
"""

from __future__ import annotations

import re

EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]{2,}")

# Почтовые ящики, которые встречаются на любом сайте и поставщика не идентифицируют.
GENERIC_EMAIL_PREFIXES = ("noreply", "no-reply", "postmaster", "abuse", "webmaster")
# Регулярка ловит и имена файлов вида logo@2x.png.
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".svg", ".webp", ".gif")


def is_contact_email(candidate: str) -> bool:
    """Похоже ли на адрес, по которому поставщику можно написать."""
    value = (candidate or "").strip().lower()
    if not value or not EMAIL_RE.fullmatch(value):
        return False
    local = value.split("@", 1)[0]
    if any(local.startswith(prefix) for prefix in GENERIC_EMAIL_PREFIXES):
        return False
    return not value.endswith(IMAGE_SUFFIXES)


def extract_email(text: str) -> str | None:
    """Первый рабочий e-mail из текста страницы."""
    for match in EMAIL_RE.finditer(text or ""):
        candidate = match.group(0).lower()
        if is_contact_email(candidate):
            return candidate
    return None
