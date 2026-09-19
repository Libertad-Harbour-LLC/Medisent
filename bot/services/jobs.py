"""Создание задач генерации и разбор их результата.

Картинка и видео идут в один эндпоинт /api/v1/jobs/createTask и различаются
только полем model. Ответ асинхронный: приходит taskId, готовый результат
провайдер присылает POST-ом на callBackUrl.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, Final

from .kie import KieClient

log = logging.getLogger(__name__)

CREATE_TASK_PATH = "/api/v1/jobs/createTask"

IMAGE_ASPECT_RATIOS: Final = (
    "auto", "1:1", "3:2", "2:3", "4:3", "3:4",
    "16:9", "9:16", "21:9", "27:16", "16:27", "9:8", "8:9",
)
# Эти соотношения провайдер поддерживает только в 1K.
ONE_K_ONLY: Final = frozenset({"27:16", "16:27", "9:8", "8:9"})
IMAGE_RESOLUTIONS: Final = ("1K", "2K", "4K")
IMAGE_BACKGROUNDS: Final = ("transparent", "opaque", "auto")
IMAGE_PROMPT_LIMIT: Final = 20_000

VIDEO_ASPECT_RATIOS: Final = ("16:9", "9:16", "1:1")
VIDEO_MODES: Final = ("std", "pro", "4K")
VIDEO_DURATION_MIN: Final = 3
VIDEO_DURATION_MAX: Final = 15
VIDEO_SHOT_PROMPT_LIMIT: Final = 500


@dataclass(slots=True)
class ImageParams:
    prompt: str
    aspect_ratio: str = "auto"
    resolution: str = "1K"
    background: str = "auto"

    def as_input(self) -> dict[str, Any]:
        resolution = self.resolution
        if self.aspect_ratio in ONE_K_ONLY and resolution != "1K":
            log.info("%s поддерживает только 1K, понижаю с %s", self.aspect_ratio, resolution)
            resolution = "1K"
        return {
            "prompt": self.prompt,
            "aspect_ratio": self.aspect_ratio,
            "resolution": resolution,
            "background": self.background,
        }


@dataclass(slots=True)
class VideoParams:
    prompt: str
    aspect_ratio: str = "16:9"
    duration: int = 5
    mode: str = "pro"
    sound: bool = False
    image_urls: list[str] | None = None

    def as_input(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "prompt": self.prompt,
            "sound": self.sound,
            "duration": str(self.duration),  # в спеке строка, не число
            "mode": self.mode,
            "multi_shots": False,
            # Схема провайдера помечает multi_prompt обязательным даже для
            # одиночной сцены — отправляем пустой список, чтобы не словить 422.
            "multi_prompt": [],
        }
        if self.image_urls:
            payload["image_urls"] = self.image_urls
        else:
            # При переданных кадрах соотношение подбирается по ним автоматически.
            payload["aspect_ratio"] = self.aspect_ratio
        return payload


@dataclass(slots=True)
class TaskResult:
    """Разобранный колбэк провайдера."""

    task_id: str
    success: bool
    urls: list[str]
    error: str
    credits: float


class JobsService:
    def __init__(self, client: KieClient, *, image_model: str, video_model: str) -> None:
        self._client = client
        self._image_model = image_model
        self._video_model = video_model

    async def create_image(self, params: ImageParams, callback_url: str = "") -> str:
        return await self._create(self._image_model, params.as_input(), callback_url)

    async def create_video(self, params: VideoParams, callback_url: str = "") -> str:
        return await self._create(self._video_model, params.as_input(), callback_url)

    async def _create(
        self, model: str, task_input: dict[str, Any], callback_url: str
    ) -> str:
        payload: dict[str, Any] = {"model": model, "input": task_input}
        if callback_url:
            # Адрес свой у каждой задачи: в нём зашито, в какой чат вернуть
            # результат. Поэтому запоминать задачу где-то ещё не нужно.
            payload["callBackUrl"] = callback_url

        body = await self._client.post(CREATE_TASK_PATH, payload)
        data = body.get("data")
        if not isinstance(data, dict) or not data.get("taskId"):
            raise ValueError(f"Провайдер не вернул taskId: {body}")

        task_id = str(data["taskId"])
        log.info("создана задача %s (%s)", task_id, model)
        return task_id


def parse_callback(body: dict[str, Any]) -> TaskResult:
    """Разбирает тело колбэка.

    Ловушка формата: resultJson — это JSON, упакованный в строку внутри JSON,
    парсить приходится дважды.
    """
    data = body.get("data")
    if not isinstance(data, dict):
        raise ValueError("в колбэке нет объекта data")

    task_id = str(data.get("taskId") or "")
    if not task_id:
        raise ValueError("в колбэке нет taskId")

    code = int(body.get("code") or 0)
    state = str(data.get("state") or "")
    success = code == 200 and state == "success"

    urls: list[str] = []
    if success:
        urls = _extract_urls(data.get("resultJson"))
        if not urls:
            success = False

    error = ""
    if not success:
        error = " ".join(
            str(part)
            for part in (data.get("failMsg"), data.get("failCode"), body.get("msg"))
            if part
        ).strip() or "провайдер не объяснил причину"

    return TaskResult(
        task_id=task_id,
        success=success,
        urls=urls,
        error=error,
        credits=float(data.get("creditsConsumed") or 0.0),
    )


def _extract_urls(result_json: Any) -> list[str]:
    if not result_json:
        return []

    if isinstance(result_json, str):
        try:
            result_json = json.loads(result_json)
        except json.JSONDecodeError:
            log.warning("resultJson не распарсился: %.200s", result_json)
            return []

    if not isinstance(result_json, dict):
        return []

    urls = result_json.get("resultUrls")
    if isinstance(urls, str):
        return [urls]
    if isinstance(urls, list):
        return [str(url) for url in urls if url]
    return []
