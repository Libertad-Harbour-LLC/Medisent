"""Приём запроса: текст, фото, голос, файл. Этапы 3–5 одной цепочкой."""

from __future__ import annotations

import logging

from aiogram import Bot, F, Router
from aiogram.filters import Command
from aiogram.types import Message

from bot import texts
from bot.config import get_settings
from bot.db import repo
from bot.db.models import RequestStatus
from bot.db.session import session_scope
from bot.pipeline import build_report, run_search
from bot.services import budget
from bot.services.gemini import GeminiError, ProductRequest, get_gemini_service
from bot.services.mail import MailError, get_mail_service
from bot.services.report import render

logger = logging.getLogger(__name__)
router = Router(name="intake")

MAX_FILE_BYTES = 20 * 1024 * 1024  # предел Telegram Bot API на скачивание


async def _download(bot: Bot, file_id: str) -> bytes | None:
    file = await bot.get_file(file_id)
    if file.file_size and file.file_size > MAX_FILE_BYTES:
        return None
    buffer = await bot.download_file(file.file_path or "")
    return buffer.read() if buffer else None


async def _start_pipeline(message: Message, parsed: ProductRequest, input_kind: str) -> None:
    """Общий хвост для всех видов входа: заявка → поиск → отчёт."""
    if not parsed.recognised:
        await message.answer(texts.INTAKE_NOT_RECOGNISED)
        return

    async with session_scope() as session:
        request = await repo.create_request(
            session,
            product=parsed.product,
            raw_input=parsed.raw_input,
            input_kind=input_kind,
        )
        request_id, token = int(request.id), request.token

    await message.answer(
        texts.intake_recognised(parsed.product, parsed.qty, token), parse_mode="HTML"
    )

    settings = get_settings()
    if not settings.search_enabled:
        await message.answer(texts.SEARCH_OFF)
        return

    await message.answer(texts.SEARCH_RUNNING)
    summary = await run_search(
        request_id=request_id,
        product=parsed.product,
        requirements=parsed.requirements,
    )

    if summary.total_found == 0:
        await message.answer(texts.SEARCH_NOTHING)
        async with session_scope() as session:
            await repo.set_request_status(session, request_id, RequestStatus.CLOSED)
        budget.forget(request_id)
        return

    await message.answer(texts.search_found(summary.total_found, summary.blacklisted))
    if summary.budget_exceeded:
        await message.answer(
            texts.BUDGET_PER_REQUEST_EXCEEDED.format(
                limit=f"{get_settings().max_cost_per_request_usd:.2f}"
            )
        )
    await message.answer(texts.REPORT_BUILDING)

    async with session_scope() as session:
        report = await build_report(
            session,
            request_id=request_id,
            product=parsed.product,
            qty=parsed.qty,
            requirements=parsed.requirements,
        )

    for chunk in render(report):
        await message.answer(chunk, parse_mode="HTML", disable_web_page_preview=True)


@router.message(Command("forward"))
async def forward_file(message: Message, bot: Bot) -> None:
    """Переслать приложенный файл на почту без всякой обработки."""
    settings = get_settings()
    if not settings.gmail_enabled or not settings.forward_to_email:
        await message.answer(texts.FORWARD_OFF)
        return

    source = message.reply_to_message or message
    document = source.document
    if document is None:
        await message.answer(texts.FORWARD_NO_FILE)
        return

    content = await _download(bot, document.file_id)
    if content is None:
        await message.answer(texts.ERROR_GENERIC)
        return

    try:
        await get_mail_service().forward_file(
            to=settings.forward_to_email,
            filename=document.file_name or "file",
            content=content,
            mime_type=document.mime_type or "application/octet-stream",
        )
    except MailError as exc:
        logger.error("Пересылка не удалась: %s", exc)
        await message.answer(texts.ERROR_GENERIC)
        return

    await message.answer(texts.FORWARD_OK.format(email=settings.forward_to_email))


@router.message(F.photo)
async def on_photo(message: Message, bot: Bot) -> None:
    if not get_settings().gemini_enabled:
        await message.answer(texts.INTAKE_GEMINI_OFF)
        return
    await message.answer(texts.INTAKE_PHOTO)

    photo = message.photo[-1] if message.photo else None
    if photo is None:
        return
    content = await _download(bot, photo.file_id)
    if content is None:
        await message.answer(texts.ERROR_GENERIC)
        return

    try:
        parsed = await get_gemini_service().parse_photo(content)
    except GeminiError as exc:
        logger.error("Распознавание фото не удалось: %s", exc)
        await message.answer(texts.INTAKE_NOT_RECOGNISED)
        return
    await _start_pipeline(message, parsed, "photo")


@router.message(F.voice | F.audio)
async def on_voice(message: Message, bot: Bot) -> None:
    """Голосовое на входе — это новая заявка.

    Голосовое в ответ на отчёт обрабатывает selection.py: он стоит раньше в
    цепочке роутеров и перехватывает сообщение, когда заявка ждёт выбора.
    """
    if not get_settings().gemini_enabled:
        await message.answer(texts.INTAKE_GEMINI_OFF)
        return
    await message.answer(texts.INTAKE_VOICE)

    media = message.voice or message.audio
    if media is None:
        return
    content = await _download(bot, media.file_id)
    if content is None:
        await message.answer(texts.ERROR_GENERIC)
        return

    try:
        parsed = await get_gemini_service().parse_voice(
            content, mime_type=media.mime_type or "audio/ogg"
        )
    except GeminiError as exc:
        logger.error("Распознавание голосового не удалось: %s", exc)
        await message.answer(texts.INTAKE_NOT_RECOGNISED)
        return
    await _start_pipeline(message, parsed, "voice")


@router.message(F.document)
async def on_document(message: Message, bot: Bot) -> None:
    if not get_settings().gemini_enabled:
        await message.answer(texts.INTAKE_GEMINI_OFF)
        return
    await message.answer(texts.INTAKE_FILE)

    document = message.document
    if document is None:
        return
    content = await _download(bot, document.file_id)
    if content is None:
        await message.answer(texts.ERROR_GENERIC)
        return

    try:
        parsed = await get_gemini_service().parse_document(
            content,
            mime_type=document.mime_type or "application/pdf",
            filename=document.file_name or "",
        )
    except GeminiError as exc:
        logger.error("Разбор файла не удался: %s", exc)
        await message.answer(texts.INTAKE_NOT_RECOGNISED)
        return
    await _start_pipeline(message, parsed, "file")


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message) -> None:
    text = (message.text or "").strip()
    if not text:
        return
    await message.answer(texts.INTAKE_ACCEPTED)

    settings = get_settings()
    if settings.gemini_enabled:
        try:
            parsed = await get_gemini_service().parse_text(text)
        except GeminiError as exc:
            logger.warning("Разбор текста моделью не удался (%s) — беру как есть", exc)
            parsed = ProductRequest(product=text, raw_input=text)
    else:
        # Без ключа Gemini текстовый запрос всё равно работает: берём строку
        # как название изделия. Это единственный вид входа, который не требует
        # модели, и терять его из-за отсутствия ключа незачем.
        parsed = ProductRequest(product=text, raw_input=text)

    await _start_pipeline(message, parsed, "text")
