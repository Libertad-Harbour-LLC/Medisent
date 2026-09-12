"""Все тексты для пользователя. В хендлерах строк для показа быть не должно.

Бот общается по-русски. Правка формулировок делается здесь и только здесь.
"""

from __future__ import annotations

import html


def esc(value: object) -> str:
    """Экранировать текст для Telegram-HTML.

    Бот шлёт всё в ``parse_mode=HTML`` (умолчание в ``main.py``), а значит
    любое ``<``, ``>`` или ``&`` в чужом тексте — название поставщика «A&D»,
    строка ``Иван <ivan@x.ru> писал(а):`` из письма — ломает всё сообщение:
    Telegram отвечает «can't parse entities», и владелец не получает ничего.
    Всё, что пришло не из этого файла, проходит через ``esc``.
    """
    return html.escape(str(value), quote=False)


# --- Общие ---------------------------------------------------------------

START = (
    "Бот подбора поставщиков медизделий.\n\n"
    "Пришлите запрос: текстом, фото коробки, голосовым или файлом.\n"
    "Я найду поставщиков, проверю изделие в реестрах Росздравнадзора "
    "и пришлю отчёт."
)

ACCESS_DENIED = "Бот работает только с владельцем."

HELP = (
    "Что умею:\n\n"
    "• Текст, фото, голосовое или файл — начну подбор поставщиков\n"
    "• /stats — расходы на внешние сервисы\n"
    "• /session — текущая заявка и её статус\n"
    "• /blacklist — чёрный список поставщиков\n"
    "• /forward — переслать приложенный файл на почту без обработки\n"
    "• /cancel — отменить текущую заявку"
)

ERROR_GENERIC = "Что-то пошло не так. Подробности в логе, заявка не потеряна."


def started_with_warnings(warnings: list[str]) -> str:
    return "Бот запущен. Выключено:\n" + "\n".join(f"• {esc(w)}" for w in warnings)


# --- Этап 3: приём запроса ----------------------------------------------

INTAKE_ACCEPTED = "Принял. Разбираю запрос…"
INTAKE_VOICE = "Слушаю голосовое…"
INTAKE_PHOTO = "Смотрю фото…"
INTAKE_FILE = "Читаю файл…"

INTAKE_GEMINI_OFF = (
    "Распознавание выключено: не задан GEMINI_API_KEY. "
    "Пришлите запрос текстом — это работает без ключа."
)


def intake_recognised(product: str, qty: str, token: str) -> str:
    return (
        f"Изделие: <b>{esc(product)}</b>\n"
        f"Количество: {esc(qty)}\n"
        f"Заявка: <code>{token}</code>\n\n"
        "Ищу поставщиков…"
    )


INTAKE_NOT_RECOGNISED = (
    "Не смог разобрать, что за изделие. Напишите название текстом — " "выдумывать не буду."
)

FORWARD_OK = "Файл отправлен на {email}."
FORWARD_NO_FILE = "Приложите файл к команде /forward."
FORWARD_OFF = "Пересылка выключена: Gmail не настроен."

# --- Этап 4: поиск -------------------------------------------------------

SEARCH_OFF = "Поиск выключен: не задан PERPLEXITY_API_KEY."
SEARCH_RUNNING = "Ищу поставщиков…"
SEARCH_NOTHING = "Поставщиков не нашёл. Попробуйте уточнить название изделия."


def search_failed(reason: str) -> str:
    """Поиск не отработал. Это не «ничего не нашли»: виноват сервис, а не
    название изделия, и советовать «уточните» здесь нельзя."""
    return (
        f"Поиск не отработал: {esc(reason)}.\n"
        "Это сбой сервиса, а не название изделия. Заявку закрыл — пришлите запрос "
        "ещё раз чуть позже."
    )


BUDGET_PER_REQUEST_EXCEEDED = (
    "⚠️ Заявка упёрлась в потолок расходов (${limit}). Собрал что успел — "
    "часть сайтов могла остаться непроверенной. Потолок меняется переменной "
    "MAX_COST_PER_REQUEST_USD."
)


def search_found(total: int, blacklisted: int) -> str:
    tail = f", из них {blacklisted} в чёрном списке — исключены" if blacklisted else ""
    return f"Нашёл кандидатов: {total}{tail}."


# --- Этап 5: отчёт -------------------------------------------------------

REPORT_BUILDING = "Собираю отчёт…"
REPORT_HEADER = "<b>Отчёт по заявке {token}</b>\nИзделие: {product}\n"
REPORT_FOOTER = "Ответьте голосовым: кого берём и почему."

# Строки статуса реестра. «Не нашли» и «не смогли проверить» — разные строки,
# сливать их запрещено (жёсткое правило проекта).
RU_FOUND = "РУ {number} · {holder} · {status} · реестр {registry}"
RU_NOT_FOUND = "в реестрах не найдено"
RU_UNAVAILABLE = "проверить не удалось, реестр недоступен"

# Информационные письма Росздравнадзора (unrega). Три исхода, как у реестра:
# письма есть (строка у кандидата), писем нет (ничего не печатается) и
# «проверить не удалось» — отдельная строка, потому что молчание здесь
# читалось бы как «писем нет».
UNREGA_UNAVAILABLE = "⚠️ Информационные письма Росздравнадзора проверить не удалось."

SITE_CLAIMS_YES = "поставщик заявляет наличие"
SITE_CLAIMS_NO = "на сайте наличие не заявлено"
SITE_CLAIMS_UNKNOWN = "сайт не проверен"

# Флаг из guard: на странице поставщика нашёлся текст, похожий на попытку
# повлиять на отбор. Владелец должен это видеть — молчать о таком нельзя.
INJECTION_SUSPECTED = "⚠️ на сайте текст, похожий на попытку повлиять на отбор — проверьте вручную"

# --- Этап 6: выбор и критерии -------------------------------------------

SELECTION_NOT_UNDERSTOOD = (
    "Не понял, кого выбрали. Скажите номер из отчёта или название поставщика."
)
SELECTION_NOT_A_CANDIDATE = (
    "Этого поставщика нет среди кандидатов заявки — письмо ему не готовлю. "
    "Скажите номер из отчёта."
)


def selection_ambiguous(tokens: list[str]) -> str:
    """Несколько заявок ждут выбора — угадывать нельзя."""
    listed = "\n".join(f"• <code>{t}</code>" for t in tokens)
    return (
        "Выбора ждут сразу несколько заявок:\n" + listed + "\n\n"
        "Скажите в голосовом номер заявки, либо закройте лишнюю через /cancel."
    )


def selection_confirmed(supplier: str) -> str:
    return f"Выбран <b>{esc(supplier)}</b>. Готовлю письмо."


def criteria_saved(count: int) -> str:
    return f"Запомнил критериев: {count}. Учту в следующих отчётах."


INFO_REQUEST_RUNNING = "Собираю дополнительную информацию по поставщику…"

# --- Этап 7: письмо и ответ ---------------------------------------------

MAIL_OFF = "Отправка писем выключена: Gmail не настроен."
SUPPLIER_NO_EMAIL = "У поставщика нет e-mail — письмо отправить некуда."


def mail_draft(supplier: str, email: str, body: str) -> str:
    return f"Письмо поставщику <b>{esc(supplier)}</b> на {esc(email)}:\n\n{esc(body)}"


MAIL_CONFIRM = "Отправляем?"
MAIL_SENT = "Письмо отправлено. Жду ответа."

# Одобрение живёт сутки: кнопки в Telegram не протухают сами, а отправлять
# письмо по подтверждению недельной давности нельзя.
APPROVAL_EXPIRED = (
    "Это подтверждение уже недействительно — оно устарело или по нему уже "
    "приняли решение. Начните выбор заново."
)


def mail_already_sent(supplier: str) -> str:
    return (
        f"Письмо в <b>{esc(supplier)}</b> по этой заявке уже отправлялось. "
        "Второй раз не отправляю."
    )


MAIL_RECIPIENT_CHANGED = (
    "Адрес поставщика изменился с момента показа письма. Отправлять по новому "
    "адресу без вашего согласия не буду — выберите поставщика заново."
)
MAIL_CANCELLED = "Письмо не отправлено."


def reply_received(supplier: str, token: str) -> str:
    return f"<b>Ответил {esc(supplier)}</b> по заявке <code>{esc(token)}</code>:"


REPLY_ATTACHMENTS = "Вложения из письма:"


def reply_processing_failed(gmail_id: str) -> str:
    """Письмо не удалось обработать трижды — молчать об этом нельзя."""
    return (
        f"⚠️ Входящее письмо (id {esc(gmail_id)}) не удалось обработать после трёх попыток. "
        "Посмотрите его в почте вручную; подробности в логе."
    )


REPLY_TAIL_FAILED = (
    "⚠️ Текст письма показал, а вложения или разбор цен не удались. Подробности в логе."
)

# --- Этап 8: КП ----------------------------------------------------------

KP_EXTRACTING = "Разбираю цены из письма…"

KP_CONFIRM_HEADER = (
    "<b>Проверьте цифры перед сборкой КП.</b>\n"
    "Цены в письмах приходят с оговорками — ошибка уйдёт клиенту под вашей печатью.\n"
)
KP_CONFIRM_FOOTER = "Всё верно?"
KP_BUILDING = "Собираю КП…"
KP_DRAFT_READY = "Черновик КП готов — без печати и подписи."
KP_CANCELLED = "Сборка КП отменена."
KP_NO_PRICES = "Цен в письме не нашёл. Соберите КП вручную или уточните у поставщика."

KP_ALREADY_EXTRACTED = (
    "Цены по этому поставщику уже разбирал. Новое письмо ниже — если в нём "
    "другие цифры, скажите, и разберу заново."
)

KP_ASSETS_MISSING = (
    "Не хватает файлов для печати и подписи:\n{items}\n\n"
    "Собираю без них. Как их подготовить — skills/kp-builder/references/stamping.md"
)

KP_VALID_UNTIL_DEFAULT = "Срок действия в письме не указан — поставил {date} (сегодня + 14 дней)."


def kp_item_line(index: int, *, name: str, amount: str, currency: str, caveats: str) -> str:
    line = f"{index}. <b>{esc(name)}</b>\n   {esc(amount)} {esc(currency)}"
    return line + (f"\n   <i>{esc(caveats)}</i>" if caveats else "")


def kp_total_line(total: str, currency: str) -> str:
    return f"\n<b>Итого: {esc(total)} {esc(currency)}</b>"


def kp_lead_time_line(lead_time: str) -> str:
    return f"Срок поставки: {esc(lead_time)}"


def kp_payment_line(payment_terms: str) -> str:
    return f"Оплата: {esc(payment_terms)}"


def kp_price_suspicious(item: str, price: str) -> str:
    return (
        f"Цена по позиции «{esc(item)}» — {esc(price)}. "
        "Похоже на ошибку на порядок. Проверьте, пожалуйста."
    )


# --- Администрирование ---------------------------------------------------


def stats_line(service: str, calls: int, cost: float) -> str:
    return f"{esc(service)}: {calls} вызовов, ${cost:.4f}"


STATS_HEADER = "<b>Расходы за сегодня</b>\n"
STATS_EMPTY = "Сегодня внешние сервисы не вызывались."


def stats_total(total: float) -> str:
    return f"\nВсего: ${total:.4f}"


def budget_exceeded(spent: float, limit: float) -> str:
    return (
        f"⚠️ Дневной бюджет превышен: ${spent:.2f} из ${limit:.2f}. "
        "Работу не останавливаю, но имейте в виду."
    )


SESSION_NONE = "Активной заявки нет."


def session_info(token: str, product: str, status: str, created: str) -> str:
    return (
        f"Заявка <code>{esc(token)}</code>\n"
        f"Изделие: {esc(product)}\n"
        f"Статус: {esc(status)}\n"
        f"Создана: {created}"
    )


BLACKLIST_EMPTY = "Чёрный список пуст."
BLACKLIST_HEADER = "<b>Чёрный список</b>\n"
BLACKLIST_USAGE = (
    "Как пользоваться: /blacklist add <id поставщика> <причина> | /blacklist lift <id>"
)
BLACKLIST_NOT_LISTED = "Такого в чёрном списке нет."


def supplier_not_found(supplier_id: int) -> str:
    return f"Поставщика #{supplier_id} нет в базе."


def blacklist_added(supplier: str) -> str:
    return f"{esc(supplier)} — в чёрном списке. В выдачу больше не попадёт."


def blacklist_lifted(supplier: str) -> str:
    return f"{esc(supplier)} — снят из чёрного списка."


def blacklist_line(supplier_id: int, supplier: str, reason: str, when: str) -> str:
    return f"#{supplier_id} {esc(supplier)} — {esc(reason)} ({when})"


CANCELLED = "Заявка отменена."

# --- Пометка недоверенного контента --------------------------------------
# Всё, что пришло с чужого сайта или из чужого письма, показывается владельцу
# в явной обёртке и никогда не исполняется как инструкция.

UNTRUSTED_OPEN = "⟨текст из внешнего источника, инструкциям внутри не следуем⟩"
UNTRUSTED_CLOSE = "⟨конец внешнего текста⟩"
