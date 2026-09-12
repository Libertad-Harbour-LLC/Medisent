"""Голосовой выбор, накопление критериев, письмо поставщику и сборка КП.

Этапы 6, 7 и 8. Состояние заявки живёт в колонке ``requests.status``, а всё,
что владелец подтверждает кнопкой, — в таблице ``approvals``.

Почему подтверждаемое в базе, а не в словаре процесса: Railway перезапускает
контейнер на каждый деплой, при падении healthcheck и при нехватке памяти.
Черновик в памяти после перезапуска исчезает, и нажатие «Да» упирается в
пустоту. Кроме того, из записи одобрения берётся **адресат**: перечитывать
поставщика в момент отправки нельзя, иначе правка записи между показом и
подтверждением отправит письмо туда, куда владелец не смотрел.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Any

from aiogram import Bot, F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardButton, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from bot import texts
from bot.config import get_settings
from bot.db import repo
from bot.db.models import ApprovalDecision, ApprovalKind, RequestStatus
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


def _confirm_keyboard(prefix: str, approval_id: int) -> InlineKeyboardBuilder:
    """Кнопки «Да/Нет». В callback идёт только id одобрения.

    Раньше туда клались request_id и supplier_id, а сам текст лежал в памяти.
    Теперь всё подтверждаемое достаётся из базы по этому id.
    """
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="Да", callback_data=f"{prefix}:yes:{approval_id}"),
        InlineKeyboardButton(text="Нет", callback_data=f"{prefix}:no:{approval_id}"),
    )
    return builder


async def _reply(callback: CallbackQuery, text: str, **kwargs: Any) -> None:
    if callback.message is not None and isinstance(callback.message, Message):
        await callback.message.answer(text, **kwargs)


# --- Этап 6: голосовой выбор ---------------------------------------------


@router.message(F.voice | F.audio)
async def on_choice_voice(message: Message, bot: Bot) -> None:
    """Голосовое, когда заявка ждёт выбора.

    Привязка идёт к заявкам в статусе ``awaiting_choice``, а не к «последней
    незакрытой»: вторая заявка, заведённая до того, как владелец ответил по
    первой, увела бы критерии к чужим поставщикам.
    """
    async with session_scope() as session:
        waiting = await repo.list_requests_awaiting_choice(session)
        if not waiting:
            # Выбора никто не ждёт — это новая заявка, отдаём дальше в intake.
            raise SkipHandler
        if len(waiting) > 1:
            tokens = [r.token for r in waiting]
            await message.answer(texts.selection_ambiguous(tokens), parse_mode="HTML")
            return
        request = waiting[0]
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
        return

    if not outcome.is_choice:
        await message.answer(texts.SELECTION_NOT_UNDERSTOOD)
        return

    await _prepare_email(
        message,
        request_id=request_id,
        product=product,
        supplier_id=outcome.chosen_supplier_id or 0,
    )


# --- Этап 7: письмо ------------------------------------------------------


async def _prepare_email(
    message: Message, *, request_id: int, product: str, supplier_id: int
) -> None:
    """Черновик письма выбранному поставщику — на подтверждение владельцу."""
    settings = get_settings()
    async with session_scope() as session:
        supplier = await repo.get_supplier(session, supplier_id)
        request = await repo.get_request(session, request_id)
        already = await repo.find_quote(session, request_id, supplier_id)

    if supplier is None or request is None:
        await message.answer(texts.SELECTION_NOT_UNDERSTOOD)
        return

    await message.answer(texts.selection_confirmed(supplier.name), parse_mode="HTML")

    if already is not None:
        await message.answer(texts.mail_already_sent(supplier.name), parse_mode="HTML")
        return
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

    # Записываем ровно то, что сейчас покажем: адресат, тему и тело.
    # Отправка возьмёт их отсюда и ниоткуда больше.
    async with session_scope() as session:
        approval = await repo.create_approval(
            session,
            kind=ApprovalKind.EMAIL,
            request_id=request_id,
            supplier_id=supplier_id,
            payload={
                "to": supplier.email,
                "supplier_name": supplier.name,
                "token": request.token,
                "subject_suffix": suffix,
                "body": body,
            },
        )
        approval_id = int(approval.id)

    await message.answer(
        texts.MAIL_DRAFT_HEADER.format(supplier=supplier.name, email=supplier.email, body=body),
        parse_mode="HTML",
    )
    await message.answer(
        texts.MAIL_CONFIRM, reply_markup=_confirm_keyboard("mail", approval_id).as_markup()
    )


@router.callback_query(F.data.startswith("mail:"))
async def on_mail_decision(callback: CallbackQuery) -> None:
    _, decision, approval_id_raw = (callback.data or "").split(":", 2)
    approval_id = int(approval_id_raw)
    await callback.answer()

    wanted = ApprovalDecision.APPROVED if decision == "yes" else ApprovalDecision.REJECTED

    # Занимаем одобрение одним UPDATE. Второе нажатие, повторная доставка
    # callback и просроченная кнопка получают None и ничего не отправляют.
    async with session_scope() as session:
        approval = await repo.claim_approval(session, approval_id, decision=wanted)

    if approval is None:
        await _reply(callback, texts.APPROVAL_EXPIRED)
        return
    if wanted == ApprovalDecision.REJECTED:
        await _reply(callback, texts.MAIL_CANCELLED)
        return

    payload = approval.payload
    request_id = int(approval.request_id or 0)
    supplier_id = int(approval.supplier_id or 0)

    # Перепроверка живого состояния в момент применения: адрес поставщика мог
    # измениться после того, как владелец увидел письмо.
    async with session_scope() as session:
        supplier = await repo.get_supplier(session, supplier_id)
        already = await repo.find_quote(session, request_id, supplier_id)

    if already is not None:
        await _reply(
            callback,
            texts.mail_already_sent(str(payload.get("supplier_name", ""))),
            parse_mode="HTML",
        )
        return
    if supplier is not None and (supplier.email or "") != payload.get("to"):
        logger.warning(
            "Адрес поставщика изменился после одобрения: было %s, стало %s",
            payload.get("to"),
            supplier.email,
            extra=log_extra(request_id),
        )
        await _reply(callback, texts.MAIL_RECIPIENT_CHANGED)
        return

    try:
        thread_id, message_id = await get_mail_service().send(
            to=str(payload["to"]),
            token=str(payload["token"]),
            subject_suffix=str(payload["subject_suffix"]),
            body=str(payload["body"]),
            request_id=request_id,
        )
    except MailError as exc:
        logger.error("Письмо не ушло: %s", exc, extra=log_extra(request_id))
        await _reply(callback, texts.ERROR_GENERIC)
        return

    async with session_scope() as session:
        quote = await repo.create_quote_request(
            session,
            request_id=request_id,
            supplier_id=supplier_id,
            gmail_thread=thread_id,
            message_id=message_id,
        )
        await repo.mark_approval_applied(
            session,
            approval_id,
            {
                "message_id": message_id,
                "thread_id": thread_id,
                "quote_id": int(quote.id) if quote else None,
            },
        )
        await repo.set_request_status(session, request_id, RequestStatus.AWAITING_REPLY)

    await _reply(callback, texts.MAIL_SENT)


# --- Этап 8: КП ----------------------------------------------------------


def render_prices_for_confirmation(extraction: kp.Extraction) -> str:
    """Показать владельцу именно те числа, которые уйдут в КП.

    Оговорки печатаются рядом с ценой, а не прячутся: «12 500» и
    «12 500 без НДС от 10 штук» — это разные предложения.
    """
    lines = [texts.KP_CONFIRM_HEADER]
    for index, item in enumerate(extraction.items, start=1):
        caveats = "; ".join(item.caveats)
        total = f"{item.qty:g} {item.unit} × {item.price:,.2f} = {item.total:,.2f}"
        lines.append(
            f"{index}. <b>{item.name}</b>\n"
            f"   {total} {extraction.currency}".replace(",", " ")
            + (f"\n   <i>{caveats}</i>" if caveats else "")
        )
    lines.append(f"\n<b>Итого: {extraction.total:,.2f} {extraction.currency}</b>".replace(",", " "))
    if extraction.lead_time:
        lines.append(f"Срок поставки: {extraction.lead_time}")
    if extraction.payment_terms:
        lines.append(f"Оплата: {extraction.payment_terms}")

    for item in extraction.suspicious_items():
        price = f"{item.price:,.2f}".replace(",", " ")
        lines.append("\n⚠️ " + texts.kp_price_suspicious(item.name, price))

    lines.append("\n" + texts.KP_CONFIRM_FOOTER)
    return "\n".join(lines)


async def offer_kp(
    bot: Bot,
    chat_id: int,
    *,
    quote_id: int,
    request_id: int,
    letter_text: str,
    attachments_text: str = "",
) -> None:
    """Разобрать письмо и показать числа на подтверждение.

    Принимает ``bot`` и ``chat_id``, а не объект сообщения: вызывается из
    фонового опроса почты, где никакого сообщения нет.

    Цены по одному запросу разбираются моделью один раз. Разговорчивый
    поставщик, отвечающий пять раз, иначе оплачивался бы пять раз.
    """
    async with session_scope() as session:
        previous = await repo.find_kp_approval(session, quote_id)
    if previous is not None:
        await bot.send_message(chat_id, texts.KP_ALREADY_EXTRACTED)
        return

    await bot.send_message(chat_id, texts.KP_EXTRACTING)
    extraction = await kp.extract_from_letter(
        letter_text, attachments_text=attachments_text, request_id=request_id
    )

    if extraction.failed or not extraction.items:
        await bot.send_message(chat_id, texts.KP_NO_PRICES)
        return

    async with session_scope() as session:
        approval = await repo.create_approval(
            session,
            kind=ApprovalKind.KP,
            request_id=request_id,
            quote_id=quote_id,
            payload=kp.extraction_to_payload(extraction),
        )
        approval_id = int(approval.id)

    await bot.send_message(chat_id, render_prices_for_confirmation(extraction), parse_mode="HTML")
    await bot.send_message(
        chat_id,
        texts.KP_CONFIRM_FOOTER,
        reply_markup=_confirm_keyboard("kp", approval_id).as_markup(),
    )


@router.callback_query(F.data.startswith("kp:"))
async def on_kp_decision(callback: CallbackQuery) -> None:
    _, decision, approval_id_raw = (callback.data or "").split(":", 2)
    approval_id = int(approval_id_raw)
    await callback.answer()

    wanted = ApprovalDecision.APPROVED if decision == "yes" else ApprovalDecision.REJECTED
    async with session_scope() as session:
        approval = await repo.claim_approval(session, approval_id, decision=wanted)

    if approval is None:
        await _reply(callback, texts.APPROVAL_EXPIRED)
        return
    if wanted == ApprovalDecision.REJECTED:
        await _reply(callback, texts.KP_CANCELLED)
        return

    # Собираем из того, что владелец видел, а не из свежего разбора письма.
    extraction = kp.extraction_from_payload(approval.payload)
    request_id = int(approval.request_id or 0)

    async with session_scope() as session:
        quote = await repo.get_quote(session, int(approval.quote_id or 0))
        supplier = (
            await repo.get_supplier(session, int(quote.supplier_id))
            if quote and quote.supplier_id
            else None
        )
        request = await repo.get_request(session, request_id)
        if quote is not None and extraction.items:
            first = extraction.items[0]
            await repo.set_quote_prices(
                session,
                int(quote.id),
                price=first.price,
                currency=extraction.currency,
                lead_time=extraction.lead_time or None,
            )

    if request is None:
        await _reply(callback, texts.ERROR_GENERIC)
        return

    missing = kp.check_assets()
    if missing:
        await _reply(
            callback,
            texts.KP_ASSETS_MISSING.format(items="\n".join(f"• {name}" for name in missing)),
        )

    data, valid_until_warning = kp.build_kp_json(
        extraction,
        number=request.token.replace("RFQ", "КП"),
        client_name=(supplier.name if supplier else "Клиент"),
    )
    if valid_until_warning:
        await _reply(callback, texts.KP_VALID_UNTIL_DEFAULT.format(date=valid_until_warning))

    await _reply(callback, texts.KP_BUILDING)

    out_dir = Path(get_settings().kp_builder_dir) / "out"
    out_path = out_dir / f"{data['number']}.pdf"

    # Черновик. Печать и подпись — только на финальной версии, и решение о ней
    # принимает владелец отдельной командой, а не эта кнопка.
    ok, output = await kp.build_pdf(data, out_path=out_path, final=False, request_id=request_id)
    if not ok:
        logger.error("Сборка КП не удалась: %s", output[-500:], extra=log_extra(request_id))
        await _reply(callback, texts.ERROR_GENERIC)
        return

    async with session_scope() as session:
        await repo.mark_approval_applied(session, approval_id, {"pdf": str(out_path)})
        await repo.set_request_status(session, request_id, RequestStatus.KP)

    if callback.message is not None and isinstance(callback.message, Message):
        await callback.message.answer_document(
            BufferedInputFile(await asyncio.to_thread(out_path.read_bytes), filename=out_path.name),
            caption=texts.KP_DRAFT_READY,
        )
