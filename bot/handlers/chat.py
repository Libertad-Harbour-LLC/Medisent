"""Свободный текст — вопрос к модели."""

from __future__ import annotations

import logging

from aiogram import F, Router
from aiogram.enums import ChatAction
from aiogram.types import Message

from .. import texts
from ..services.claude import ClaudeService
from ..services.errors import explain
from ..services.kie import KieError
from ..storage import Storage

log = logging.getLogger(__name__)

router = Router(name="chat")

SYSTEM_PROMPT = (
    "Ты помощник в Telegram. Отвечай по-русски, по делу и коротко — "
    "длинные ответы в мессенджере не читают. Не используй markdown-разметку, "
    "кроме простых списков."
)

# Telegram режет сообщения длиннее 4096 символов.
TELEGRAM_TEXT_LIMIT = 4000
HISTORY_TURNS = 20


@router.message(F.text & ~F.text.startswith("/"))
async def on_text(message: Message, storage: Storage, claude: ClaudeService) -> None:
    if message.from_user is None or not message.text:
        return

    user_id = message.from_user.id
    history = await storage.recent_history(user_id, HISTORY_TURNS)
    history.append({"role": "user", "content": message.text})

    await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)

    try:
        reply = await claude.ask(history, system=SYSTEM_PROMPT)
    except KieError as error:
        log.warning("чат не удался для %s: %s", user_id, error)
        await message.answer(explain(error))
        return

    if not reply.text:
        await message.answer(texts.ERROR_GENERIC.format(code="—", detail="пустой ответ"))
        return

    log.info(
        "чат %s: %d→%d токенов, %.3f кредита",
        user_id, reply.input_tokens, reply.output_tokens, reply.credits,
    )

    await storage.append_history(user_id, "user", message.text)
    await storage.append_history(user_id, "assistant", reply.text)

    for chunk in _split(reply.text, TELEGRAM_TEXT_LIMIT):
        await message.answer(chunk)


def _split(text: str, limit: int) -> list[str]:
    """Режет длинный ответ по абзацам, а не посреди слова."""
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""
    for paragraph in text.split("\n"):
        if len(current) + len(paragraph) + 1 > limit and current:
            chunks.append(current.rstrip())
            current = ""
        while len(paragraph) > limit:
            chunks.append(paragraph[:limit])
            paragraph = paragraph[limit:]
        current += paragraph + "\n"

    if current.strip():
        chunks.append(current.rstrip())
    return chunks
