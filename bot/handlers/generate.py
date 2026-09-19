"""Команды /img и /video — постановка задач генерации."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from .. import texts
from ..config import Config
from ..services import jobs
from ..services.args import ArgError, ParsedCommand, parse
from ..services.errors import explain
from ..services.jobs import ImageParams, JobsService, VideoParams
from ..services.kie import KieError
from ..storage import Storage, Task

log = logging.getLogger(__name__)

router = Router(name="generate")


@router.message(Command("img"))
async def on_image(
    message: Message,
    command: CommandObject,
    storage: Storage,
    jobs_service: JobsService,
    config: Config,
) -> None:
    parsed = await _parse_command(message, command, jobs.IMAGE_PROMPT_LIMIT)
    if parsed is None:
        return

    try:
        params = ImageParams(
            prompt=parsed.prompt,
            aspect_ratio=parsed.choice("ar", jobs.IMAGE_ASPECT_RATIOS, "auto"),
            resolution=parsed.choice("res", jobs.IMAGE_RESOLUTIONS, "1K"),
            background=parsed.choice("bg", jobs.IMAGE_BACKGROUNDS, "auto"),
        )
    except ArgError as exc:
        await message.answer(_bad_arg(str(exc)))
        return

    await _submit(
        message,
        storage,
        config,
        kind="image",
        prompt=params.prompt,
        queued_text=texts.IMAGE_QUEUED,
        create=lambda: jobs_service.create_image(params),
    )


@router.message(Command("video"))
async def on_video(
    message: Message,
    command: CommandObject,
    storage: Storage,
    jobs_service: JobsService,
    config: Config,
) -> None:
    parsed = await _parse_command(message, command, jobs.VIDEO_SHOT_PROMPT_LIMIT)
    if parsed is None:
        return

    try:
        params = VideoParams(
            prompt=parsed.prompt,
            aspect_ratio=parsed.choice("ar", jobs.VIDEO_ASPECT_RATIOS, "16:9"),
            mode=parsed.choice("mode", jobs.VIDEO_MODES, "pro"),
            duration=parsed.integer(
                "sec", 5, jobs.VIDEO_DURATION_MIN, jobs.VIDEO_DURATION_MAX
            ),
            sound=parsed.flag("sound"),
        )
    except ArgError as exc:
        await message.answer(_bad_arg(str(exc)))
        return

    await _submit(
        message,
        storage,
        config,
        kind="video",
        prompt=params.prompt,
        queued_text=texts.VIDEO_QUEUED,
        create=lambda: jobs_service.create_video(params),
    )


async def _parse_command(
    message: Message, command: CommandObject, limit: int
) -> ParsedCommand | None:
    """Проверяет аргументы команды, сам отвечает пользователю при ошибке."""
    raw = (command.args or "").strip()
    if not raw:
        await message.answer(texts.EMPTY_PROMPT)
        return None

    parsed = parse(raw)
    if not parsed.prompt:
        await message.answer(texts.EMPTY_PROMPT)
        return None
    if len(parsed.prompt) > limit:
        await message.answer(texts.PROMPT_TOO_LONG.format(limit=limit))
        return None

    return parsed


async def _submit(
    message: Message,
    storage: Storage,
    config: Config,
    *,
    kind: str,
    prompt: str,
    queued_text: str,
    create: Callable[[], Awaitable[str]],
) -> None:
    if message.from_user is None:
        return

    try:
        task_id = await create()
    except KieError as error:
        log.warning("не удалось создать задачу %s: %s", kind, error)
        await message.answer(explain(error))
        return
    except ValueError as error:
        log.error("неожиданный ответ провайдера: %s", error)
        await message.answer(texts.ERROR_GENERIC.format(code="—", detail=str(error)))
        return

    if not config.callbacks_enabled:
        # Без публичного адреса забрать результат нечем: спецификации опроса
        # статуса задачи у нас нет, только колбэк.
        await message.answer(texts.TASK_ACCEPTED_NO_CALLBACK.format(task_id=task_id))
        return

    status = await message.answer(queued_text)
    await storage.add_task(
        Task(
            task_id=task_id,
            kind=kind,
            chat_id=message.chat.id,
            user_id=message.from_user.id,
            status_msg=status.message_id,
            prompt=prompt,
        )
    )


def _bad_arg(raw: str) -> str:
    key, _, value = raw.partition(":")
    match key:
        case "ar":
            allowed = ", ".join(
                dict.fromkeys(jobs.IMAGE_ASPECT_RATIOS + jobs.VIDEO_ASPECT_RATIOS)
            )
            return texts.BAD_ASPECT_RATIO.format(value=value, allowed=allowed)
        case "res":
            return texts.BAD_RESOLUTION.format(
                value=value, allowed=", ".join(jobs.IMAGE_RESOLUTIONS)
            )
        case "mode":
            return texts.BAD_MODE.format(allowed=", ".join(jobs.VIDEO_MODES))
        case "sec":
            return texts.BAD_DURATION.format(
                lo=jobs.VIDEO_DURATION_MIN, hi=jobs.VIDEO_DURATION_MAX
            )
        case _:
            return texts.ERROR_VALIDATION.format(detail=f"неизвестный ключ --{key}")
