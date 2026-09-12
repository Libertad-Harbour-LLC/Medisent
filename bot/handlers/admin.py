"""Команды владельца: /start, /help, /stats, /session, /blacklist, /cancel."""

from __future__ import annotations

import logging

from aiogram import Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import Message

from bot import texts
from bot.db import repo
from bot.db.models import RequestStatus
from bot.db.session import session_scope
from bot.services import budget

logger = logging.getLogger(__name__)
router = Router(name="admin")


@router.message(CommandStart())
async def cmd_start(message: Message) -> None:
    await message.answer(texts.START)


@router.message(Command("help"))
async def cmd_help(message: Message) -> None:
    await message.answer(texts.HELP)


@router.message(Command("stats"))
async def cmd_stats(message: Message) -> None:
    """Расходы на внешние сервисы за сегодня."""
    async with session_scope() as session:
        rows = await repo.stats_today(session)
        total = await repo.spent_today(session)

    if not rows:
        await message.answer(texts.STATS_EMPTY)
        return

    lines = [texts.STATS_HEADER]
    lines.extend(
        texts.stats_line(str(row.service), int(row.calls), float(row.cost)) for row in rows
    )
    lines.append(f"\nВсего: ${float(total):.4f}")
    await message.answer("\n".join(lines), parse_mode="HTML")


@router.message(Command("session"))
async def cmd_session(message: Message) -> None:
    async with session_scope() as session:
        request = await repo.get_active_request(session)
    if request is None:
        await message.answer(texts.SESSION_NONE)
        return
    await message.answer(
        texts.session_info(
            token=request.token,
            product=request.product,
            status=request.status,
            created=request.created_at.strftime("%d.%m.%Y %H:%M"),
        ),
        parse_mode="HTML",
    )


@router.message(Command("cancel"))
async def cmd_cancel(message: Message) -> None:
    async with session_scope() as session:
        request = await repo.get_active_request(session)
        if request is None:
            await message.answer(texts.SESSION_NONE)
            return
        request_id = int(request.id)
        await repo.set_request_status(session, request_id, RequestStatus.CLOSED)
    budget.forget(request_id)
    await message.answer(texts.CANCELLED)


@router.message(Command("blacklist"))
async def cmd_blacklist(message: Message, command: CommandObject) -> None:
    """Показать список, добавить или снять.

    Чёрный список — отдельная таблица с причиной и датой, а не флажок:
    через полгода надо будет вспомнить, за что поставщика закрыли.
    """
    args = (command.args or "").split(maxsplit=2)

    if not args:
        async with session_scope() as session:
            rows = await repo.list_blacklist(session)
        if not rows:
            await message.answer(texts.BLACKLIST_EMPTY + "\n\n" + texts.BLACKLIST_USAGE)
            return
        lines = [texts.BLACKLIST_HEADER]
        for row in rows:
            when = row.added_at.strftime("%d.%m.%Y")
            lines.append(f"#{row.supplier_id} {row.supplier_name} — {row.reason} ({when})")
        lines.append("\n" + texts.BLACKLIST_USAGE)
        await message.answer("\n".join(lines), parse_mode="HTML")
        return

    action = args[0].lower()

    if action == "add":
        if len(args) < 3:
            await message.answer(texts.BLACKLIST_USAGE)
            return
        try:
            supplier_id = int(args[1])
        except ValueError:
            await message.answer(texts.BLACKLIST_USAGE)
            return
        async with session_scope() as session:
            supplier = await repo.get_supplier(session, supplier_id)
            if supplier is None:
                await message.answer(f"Поставщика #{supplier_id} нет в базе.")
                return
            await repo.add_to_blacklist(session, supplier_id, args[2])
            name = supplier.name
        await message.answer(texts.blacklist_added(name))
        return

    if action == "lift":
        if len(args) < 2:
            await message.answer(texts.BLACKLIST_USAGE)
            return
        try:
            supplier_id = int(args[1])
        except ValueError:
            await message.answer(texts.BLACKLIST_USAGE)
            return
        async with session_scope() as session:
            supplier = await repo.get_supplier(session, supplier_id)
            lifted = await repo.lift_from_blacklist(session, supplier_id)
            name = supplier.name if supplier else str(supplier_id)
        await message.answer(
            texts.blacklist_lifted(name) if lifted else "Такого в чёрном списке нет."
        )
        return

    await message.answer(texts.BLACKLIST_USAGE)
