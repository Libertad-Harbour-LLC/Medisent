"""Фоновые задачи: опрос почты, контроль бюджета, heartbeat, чистка кэша."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from decimal import Decimal
from typing import Any

from aiogram import Bot
from aiogram.types import BufferedInputFile

from bot import health, texts
from bot.config import get_settings
from bot.db import repo
from bot.db.models import RequestStatus
from bot.db.session import session_scope
from bot.handlers.selection import offer_kp
from bot.logging_setup import log_extra
from bot.services import guard
from bot.services.kp import read_pdf_attachment
from bot.services.mail import MailError, get_mail_service

logger = logging.getLogger(__name__)

BUDGET_CHECK_SECONDS = 900
CACHE_PURGE_SECONDS = 86_400


async def heartbeat_loop() -> None:
    """Отметка живости для HEALTHCHECK контейнера."""
    while True:
        health.touch()
        await asyncio.sleep(health.HEARTBEAT_INTERVAL)


async def budget_loop(bot: Bot) -> None:
    """Мягкий потолок расходов.

    При превышении бот предупреждает владельца и **продолжает работу** —
    блокировать его нельзя, иначе заявка застрянет на середине.
    """
    settings = get_settings()
    warned_for_day: str | None = None

    while True:
        await asyncio.sleep(BUDGET_CHECK_SECONDS)
        try:
            async with session_scope() as session:
                spent = await repo.spent_today(session)
            limit = Decimal(str(settings.daily_api_budget_usd))
            if limit > 0 and spent > limit:
                import datetime as dt

                today = dt.date.today().isoformat()
                if warned_for_day != today:
                    warned_for_day = today
                    await bot.send_message(
                        settings.telegram_owner_id,
                        texts.budget_exceeded(float(spent), float(limit)),
                    )
        except Exception:
            logger.exception("Проверка бюджета упала")


async def cache_purge_loop() -> None:
    """Чистка протухшего кэша реестра раз в сутки."""
    settings = get_settings()
    while True:
        await asyncio.sleep(CACHE_PURGE_SECONDS)
        try:
            async with session_scope() as session:
                removed = await repo.purge_registry_cache(session, settings.registry_cache_days * 2)
            if removed:
                logger.info("Из кэша реестра удалено записей: %s", removed)
            async with session_scope() as session:
                expired = await repo.expire_stale_approvals(session)
            if expired:
                logger.info("Просроченных одобрений помечено: %s", expired)
        except Exception:
            logger.exception("Чистка кэша упала")


async def mail_poll_loop(bot: Bot) -> None:
    """Опрос ответов поставщиков через ``history.list``.

    ТЗ: раз в 3–5 минут. ``users.watch`` с Pub/Sub прикручивается, когда
    заявок станет много.
    """
    settings = get_settings()
    if not settings.gmail_enabled:
        logger.info("Опрос почты не запущен: Gmail не настроен")
        return

    service = get_mail_service()

    # Первый запуск: точку отсчёта берём из профиля, иначе разберём всю почту
    # за всё время.
    async with session_scope() as session:
        history_id = await repo.get_gmail_history_id(session)
    if not history_id:
        try:
            history_id = await service.current_history_id()
        except MailError as exc:
            logger.error("Gmail недоступен, опрос не стартовал: %s", exc)
            return
        if history_id:
            async with session_scope() as session:
                await repo.set_gmail_history_id(session, history_id)
        logger.info("Опрос почты начат с historyId=%s", history_id)

    while True:
        await asyncio.sleep(settings.gmail_poll_seconds)
        try:
            async with session_scope() as session:
                history_id = await repo.get_gmail_history_id(session)
            if not history_id:
                history_id = await service.current_history_id()
                if not history_id:
                    continue
                async with session_scope() as session:
                    await repo.set_gmail_history_id(session, history_id)
                continue

            message_ids, latest = await service.new_message_ids(history_id)
            all_handled = await _handle_batch(bot, message_ids)

            # Курсор двигается только после того, как разобран весь срез.
            # Раньше он уходил вперёд до обработки, и письмо, на котором
            # обработка упала, не возвращалось уже никогда.
            if latest and all_handled:
                async with session_scope() as session:
                    await repo.set_gmail_history_id(session, latest)

        except Exception:
            logger.exception("Опрос почты упал, продолжаю со следующего цикла")


# Сколько раз пробовать разобрать одно письмо, прежде чем сдаться и пойти
# дальше: иначе одно «ядовитое» письмо остановило бы приём всех остальных.
MAX_MESSAGE_ATTEMPTS = 3
_attempts: dict[str, int] = {}
# Письма, разобранные в этом процессе: при повторе среза (курсор не сдвинулся
# из-за соседнего письма) их не надо показывать владельцу второй раз.
_handled: set[str] = set()


async def _handle_batch(bot: Bot, message_ids: list[str]) -> bool:
    """Разобрать срез писем. ``True`` — курсор можно двигать.

    Упавшее письмо повторяется на следующих циклах; после
    ``MAX_MESSAGE_ATTEMPTS`` о нём сообщается владельцу и оно пропускается.
    """
    settings = get_settings()
    for message_id in message_ids:
        if message_id in _handled:
            continue
        try:
            await _handle_incoming(bot, message_id)
        except Exception:
            attempts = _attempts.get(message_id, 0) + 1
            _attempts[message_id] = attempts
            logger.exception(
                "Письмо %s не обработано (попытка %s из %s)",
                message_id,
                attempts,
                MAX_MESSAGE_ATTEMPTS,
            )
            if attempts < MAX_MESSAGE_ATTEMPTS:
                return False
            _attempts.pop(message_id, None)
            with contextlib.suppress(Exception):
                await bot.send_message(
                    settings.telegram_owner_id, texts.reply_processing_failed(message_id)
                )
        _handled.add(message_id)
        _attempts.pop(message_id, None)
    return True


async def _handle_incoming(bot: Bot, gmail_message_id: str) -> None:
    """Разобрать входящее письмо и привязать к заявке.

    Порядок шагов выбран так, чтобы сбой не терял письмо: сначала владелец
    видит текст, потом в базе появляется «ответил», и только потом идут
    вложения и разбор цен. Упало до записи — придёт снова на следующем
    цикле; упало после — владелец уже всё видел.
    """
    settings = get_settings()
    service = get_mail_service()

    # Сначала одни заголовки: матчингу больше не нужно, а в ящик приходит
    # не только почта поставщиков. Тело качается для того, что привязалось.
    headers = await service.get_message(gmail_message_id, full=False)
    if headers is None:
        return
    # Собственные исходящие в ответы не записываем.
    if headers.from_email == settings.gmail_sender.lower():
        return

    from bot.services.mail import match_quote

    async with session_scope() as session:
        match = await match_quote(session, headers)
    if match.quote is None:
        logger.info("Письмо от %s ни к какой заявке не привязалось", headers.from_email)
        return

    quote_id = int(match.quote.id)
    request_id = int(match.quote.request_id or 0)
    logger.info(
        "Ответ привязан к заявке по признаку «%s»", match.method, extra=log_extra(request_id)
    )

    full = await service.get_message(gmail_message_id, full=True, request_id=request_id)
    if full is None:
        raise RuntimeError(f"письмо {gmail_message_id} привязалось, но не прочиталось")
    if match.quote.replied_at is not None and match.quote.reply_text == full.body:
        # Уже записано и показано — повтор среза после сбоя на соседнем письме.
        return

    async with session_scope() as session:
        request = await repo.get_request(session, request_id) if request_id else None
        supplier = await repo.get_supplier(session, int(match.quote.supplier_id or 0))

    supplier_name = supplier.name if supplier else headers.from_email
    token = request.token if request else "?"
    await bot.send_message(
        settings.telegram_owner_id,
        texts.reply_received(supplier_name, token),
        parse_mode="HTML",
    )
    # Текст письма — чужой. Показываем владельцу в явной рамке, экранированным.
    await bot.send_message(
        settings.telegram_owner_id, guard.for_owner(full.body), parse_mode="HTML"
    )

    async with session_scope() as session:
        await repo.mark_quote_replied(
            session, quote_id, reply_text=full.body, thread_id=full.thread_id
        )
        if request_id:
            await repo.set_request_status(session, request_id, RequestStatus.KP)

    # Хвост: вложения и разбор цен. Владелец уже видел письмо, поэтому сбой
    # здесь не повод разбирать письмо заново — только сказать об этом.
    try:
        await _handle_reply_tail(bot, service, gmail_message_id, full, quote_id, request_id)
    except Exception:
        logger.exception("Вложения или разбор цен не удались", extra=log_extra(request_id))
        with contextlib.suppress(Exception):
            await bot.send_message(settings.telegram_owner_id, texts.REPLY_TAIL_FAILED)


async def _handle_reply_tail(
    bot: Bot,
    service: Any,
    gmail_message_id: str,
    full: Any,
    quote_id: int,
    request_id: int,
) -> None:
    settings = get_settings()
    attachments_text = ""
    if full.attachments:
        await bot.send_message(settings.telegram_owner_id, texts.REPLY_ATTACHMENTS)
        for attachment in full.attachments:
            content = await service.download_attachment(
                gmail_message_id, attachment["attachment_id"], request_id=request_id
            )
            if content is None:
                continue
            await bot.send_document(
                settings.telegram_owner_id,
                BufferedInputFile(content, filename=attachment["filename"]),
            )
            if attachment["filename"].lower().endswith(".pdf"):
                # pdfplumber синхронный и на прайсе в 30 страниц занимает
                # десятки секунд — в отдельном потоке, чтобы бот не замирал.
                attachments_text += "\n" + await asyncio.to_thread(read_pdf_attachment, content)

    # Дальше — этап 8: разбор цен и подтверждение чисел владельцем.
    await offer_kp(
        bot,
        settings.telegram_owner_id,
        quote_id=quote_id,
        request_id=request_id,
        letter_text=full.body,
        attachments_text=attachments_text,
    )


def start_background_tasks(bot: Bot) -> list[asyncio.Task[None]]:
    """Запустить фоновые циклы и вернуть задачи, чтобы их можно было снять."""
    tasks = [
        asyncio.create_task(heartbeat_loop(), name="heartbeat"),
        asyncio.create_task(budget_loop(bot), name="budget"),
        asyncio.create_task(cache_purge_loop(), name="cache-purge"),
        asyncio.create_task(mail_poll_loop(bot), name="mail-poll"),
    ]
    return tasks


async def stop_background_tasks(tasks: list[asyncio.Task[None]]) -> None:
    for task in tasks:
        task.cancel()
    for task in tasks:
        with contextlib.suppress(asyncio.CancelledError):
            await task
