"""Голосовой выбор, накопление критериев, письмо поставщику и сборка КП.

Этапы 6, 7 и 8. Состояние заявки живёт в колонке ``requests.status``, а не в
словаре в памяти процесса: перезапуск бота не должен терять, на чём
остановился разговор.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot import texts
from bot.config import get_settings
from bot.db import repo
from bot.db.models import RequestStatus
from bot.db.session import session_scope
from bot.logging_setup import log_extra
from bot.services import criteria as criteria_service
from bot.services import kp
from bot.services.gemini import GeminiError, get_gemini_service
from bot.services.mail import MailError, get_mail_service

logger = logging.getLogger(__name__)
router = Router(name="selection")

EMAIL_SCHEMA = {
    "type": "OBJECT",
    "properties": {
        "subject_suffix": {"type": "STRING"},
        "body": {"type": "STRING"},
        "questions": {"type": "ARRAY", "items": {"type": "STRING"}},
    },
    "required": ["subject_suffix", "body"],
}


def _confirm_keyboard(prefix: str, payload: str) -> InlineKeyboardBuilder:
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Да", callback_data=f"{prefix}:yes:{payload}"),
        InlineKeyboardButton(text="Нет", callback_data=f"{prefix}:no:{payload}"),
    )
    return builder


@router.message(F.voice | F.audio)
async def on_choice_voice(message: Message, bot: Bot) -> None:
    """Голосовое, когда заявка ждёт выбора."""
    async with session_scope() as session:
        request = await repo.get_active_request(session)
        if request is None or request.status != RequestStatus.AWAITING_CHOICE:
            # Не выбор, а новая заявка. SkipHandler передаёт сообщение дальше
            # по цепочке роутеров; простой return съел бы его молча.
            raise SkipHandler
        request_id, product = int(request.id), request.product
        rows = await repo.list_candidates_for_report(session, request_id)
        known = await criteria_service.for_prompt(session)

    media = message.voice or message.audio
    if media is None:
        return

    file = await bot.get_file(media.file_id)
    buffer = await bot.download_file(file.file_path or "")
    if buffer is None:
        await message.answer(texts.ERROR_GENERIC)
        return

    try:
        transcript = await get_gemini_service().transcribe(
            buffer.read(), mime_type=media.mime_type or "audio/ogg", request_id=request_id
        )
    except GeminiError as exc:
        logger.error("Расшифровка не удалась: %s", exc, extra=log_extra(request_id))
        await message.answer(texts.ERROR_GENERIC)
        return

    candidates = [
        {"id": int(row.supplier_id or 0), "supplier": str(row.supplier_name), "rank": index}
        for index, row in enumerate(rows, start=1)
    ]
    outcome = await criteria_service.extract(
        transcript, candidates=candidates, known_criteria=known, request_id=request_id
    )
    if outcome.failed:
        await message.answer(texts.SELECTION_NOT_UNDERSTOOD)
        return

    async with session_scope() as session:
        total, _ = await criteria_service.persist(session, outcome, request_id=request_id)
    if total:
        await message.answer(texts.criteria_saved(total))

    if outcome.wants_more_info_about and not outcome.is_choice:
        await message.answer(texts.INFO_REQUEST_RUNNING)
        # Заявка остаётся в awaiting_choice: владелец ещё не выбрал.
        return

    if not outcome.is_choice:
        await message.answer(texts.SELECTION_NOT_UNDERSTOOD)
        return

    await _prepare_email(
        message, request_id=request_id, product=product, supplier_id=outcome.chosen_supplier_id or 0
    )


async def _prepare_email(
    message: Message, *, request_id: int, product: str, supplier_id: int
) -> None:
    """Черновик письма выбранному поставщику — на подтверждение владельцу."""
    settings = get_settings()
    async with session_scope() as session:
        supplier = await repo.get_supplier(session, supplier_id)
        request = await repo.get_request(session, request_id)
    if supplier is None or request is None:
        await message.answer(texts.SELECTION_NOT_UNDERSTOOD)
        return

    await message.answer(texts.selection_confirmed(supplier.name), parse_mode="HTML")

    if not settings.gmail_enabled:
        await message.answer(texts.MAIL_OFF)
        return
    if not supplier.email:
        await message.answer("У поставщика нет e-mail — письмо отправить некуда.")
        return

    try:
        drafted = await get_gemini_service().run_prompt_file(
            "email",
            {
                "token": request.token,
                "product": product,
                "qty": "не указано",
                "requirements": [],
                "supplier": {"name": supplier.name, "email": supplier.email},
                "site_claims": None,
                "site_url": None,
                "sender_name": settings.gmail_sender,
            },
            schema=EMAIL_SCHEMA,
            model=settings.llm_email_model,
            request_id=request_id,
            operation="email.draft",
        )
    except GeminiError as exc:
        logger.error("Письмо не составилось: %s", exc, extra=log_extra(request_id))
        await message.answer(texts.ERROR_GENERIC)
        return

    body = str(drafted.get("body") or "").strip()
    suffix = str(drafted.get("subject_suffix") or f"Запрос цены — {product}").strip()

    await message.answer(
        texts.MAIL_DRAFT_HEADER.format(supplier=supplier.name, email=supplier.email, body=body),
        parse_mode="HTML",
    )
    keyboard = _confirm_keyboard("mail", f"{request_id}:{supplier_id}")
    await message.answer(texts.MAIL_CONFIRM, reply_markup=keyboard.as_markup())

    # Черновик держим в кэше сообщения: следующий шаг — только подтверждение.
    _DRAFTS[(request_id, supplier_id)] = (suffix, body)


# Черновики писем между показом и подтверждением. Живут минуты; при
# перезапуске теряются, и владелец просто повторит выбор.
_DRAFTS: dict[tuple[int, int], tuple[str, str]] = {}


@router.callback_query(F.data.startswith("mail:"))
async def on_mail_decision(callback: CallbackQuery) -> None:
    _, decision, request_id_raw, supplier_id_raw = (callback.data or "").split(":", 3)
    request_id, supplier_id = int(request_id_raw), int(supplier_id_raw)
    await callback.answer()

    if decision != "yes":
        _DRAFTS.pop((request_id, supplier_id), None)
        if callback.message:
            await callback.message.answer(texts.MAIL_CANCELLED)
        return

    draft = _DRAFTS.pop((request_id, supplier_id), None)
    if draft is None:
        if callback.message:
            await callback.message.answer(texts.ERROR_GENERIC)
        return
    suffix, body = draft

    async with session_scope() as session:
        supplier = await repo.get_supplier(session, supplier_id)
        request = await repo.get_request(session, request_id)
    if supplier is None or request is None or not supplier.email:
        return

    try:
        thread_id, message_id = await get_mail_service().send(
            to=supplier.email,
            token=request.token,
            subject_suffix=suffix,
            body=body,
            request_id=request_id,
        )
    except MailError as exc:
        logger.error("Письмо не ушло: %s", exc, extra=log_extra(request_id))
        if callback.message:
            await callback.message.answer(texts.ERROR_GENERIC)
        return

    async with session_scope() as session:
        await repo.create_quote_request(
            session,
            request_id=request_id,
            supplier_id=supplier_id,
            gmail_thread=thread_id,
            message_id=message_id,
        )
        await repo.set_request_status(session, request_id, RequestStatus.AWAITING_REPLY)

    if callback.message:
        await callback.message.answer(texts.MAIL_SENT)


# --- Этап 8: КП ----------------------------------------------------------

# Разобранные из письма позиции между показом и подтверждением.
_EXTRACTIONS: dict[int, kp.Extraction] = {}


def render_prices_for_confirmation(extraction: kp.Extraction) -> str:
    """Показать владельцу именно те числа, которые уйдут в КП.

    Оговорки печатаются рядом с ценой, а не прячутся: «12 500» и
    «12 500 без НДС от 10 штук» — это разные предложения.
    """
    lines = [texts.KP_CONFIRM_HEADER]
    for index, item in enumerate(extraction.items, start=1):
        caveats = "; ".join(item.caveats)
        lines.append(
            f"{index}. <b>{item.name}</b>\n"
            f"   {item.qty:g} {item.unit} × {item.price:,.2f} = "
            f"{item.total:,.2f} {extraction.currency}".replace(",", " ")
            + (f"\n   <i>{caveats}</i>" if caveats else "")
        )
    lines.append(f"\n<b>Итого: {extraction.total:,.2f} {extraction.currency}</b>".replace(",", " "))
    if extraction.lead_time:
        lines.append(f"Срок поставки: {extraction.lead_time}")
    if extraction.payment_terms:
        lines.append(f"Оплата: {extraction.payment_terms}")

    for item in extraction.suspicious_items():
        lines.append(
            "\n⚠️ " + texts.kp_price_suspicious(item.name, f"{item.price:,.2f}".replace(",", " "))
        )

    lines.append("\n" + texts.KP_CONFIRM_FOOTER)
    return "\n".join(lines)


async def offer_kp(
    message: Message, *, quote_id: int, letter_text: str, attachments_text: str = ""
) -> None:
    """Разобрать письмо и показать числа на подтверждение."""
    await message.answer(texts.KP_EXTRACTING)
    extraction = await kp.extract_from_letter(letter_text, attachments_text=attachments_text)

    if extraction.failed or not extraction.items:
        await message.answer(texts.KP_NO_PRICES)
        return

    _EXTRACTIONS[quote_id] = extraction
    await message.answer(render_prices_for_confirmation(extraction), parse_mode="HTML")
    keyboard = _confirm_keyboard("kp", str(quote_id))
    await message.answer(texts.KP_CONFIRM_FOOTER, reply_markup=keyboard.as_markup())


@router.callback_query(F.data.startswith("kp:"))
async def on_kp_decision(callback: CallbackQuery) -> None:
    _, decision, quote_id_raw = (callback.data or "").split(":", 2)
    quote_id = int(quote_id_raw)
    await callback.answer()

    extraction = _EXTRACTIONS.pop(quote_id, None)
    if decision != "yes" or extraction is None or callback.message is None:
        if callback.message:
            await callback.message.answer(texts.KP_CANCELLED)
        return

    async with session_scope() as session:
        quote = await repo.get_quote(session, quote_id)
        supplier = (
            await repo.get_supplier(session, int(quote.supplier_id))
            if quote and quote.supplier_id
            else None
        )
        request = (
            await repo.get_request(session, int(quote.request_id))
            if quote and quote.request_id
            else None
        )
        if quote is not None:
            first = extraction.items[0]
            await repo.set_quote_prices(
                session,
                quote_id,
                price=first.price,
                currency=extraction.currency,
                lead_time=extraction.lead_time or None,
            )

    if request is None:
        await callback.message.answer(texts.ERROR_GENERIC)
        return

    missing = kp.check_assets()
    if missing:
        await callback.message.answer(
            texts.KP_ASSETS_MISSING.format(items="\n".join(f"• {name}" for name in missing))
        )

    data, valid_until_warning = kp.build_kp_json(
        extraction,
        number=request.token.replace("RFQ", "КП"),
        client_name=(supplier.name if supplier else "Клиент"),
    )
    if valid_until_warning:
        await callback.message.answer(texts.KP_VALID_UNTIL_DEFAULT.format(date=valid_until_warning))

    await callback.message.answer(texts.KP_BUILDING)

    out_dir = Path(get_settings().kp_builder_dir) / "out"
    out_path = out_dir / f"{data['number']}.pdf"

    # Черновик. Печать и подпись — только на финальной версии, и решение о
    # ней принимает владелец отдельной командой, а не эта кнопка.
    ok, output = await kp.build_pdf(
        data, out_path=out_path, final=False, request_id=int(request.id)
    )
    if not ok:
        logger.error("Сборка КП не удалась: %s", output[-500:])
        await callback.message.answer(texts.ERROR_GENERIC)
        return

    await callback.message.answer_document(
        BufferedInputFile(await asyncio.to_thread(out_path.read_bytes), filename=out_path.name),
        caption=texts.KP_DRAFT_READY,
    )
    async with session_scope() as session:
        await repo.set_request_status(session, int(request.id), RequestStatus.KP)
