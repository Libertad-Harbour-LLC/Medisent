"""Тесты подписанного маршрута в адресе колбэка.

Это замена хранилища: раз «кому отправить результат» едет в URL, подпись —
единственное, что мешает постороннему прислать боту чужую картинку или
завалить чужой чат. Ошибка здесь тихая и дорогая.
"""

from __future__ import annotations

import base64
import json

import pytest

from bot.config import Config
from bot.services.tokens import BadToken, Route, decode, encode

SECRET = "секрет-из-токена-бота"


def _route(**overrides) -> Route:
    base = dict(chat_id=777, status_msg=42, kind="video", prompt="рыжий кот")
    base.update(overrides)
    return Route(**base)


class TestRoundTrip:
    def test_route_survives(self) -> None:
        route = decode(encode(_route(), SECRET), SECRET)
        assert (route.chat_id, route.status_msg, route.kind) == (777, 42, "video")
        assert route.prompt == "рыжий кот"

    def test_missing_status_message(self) -> None:
        assert decode(encode(_route(status_msg=None), SECRET), SECRET).status_msg is None

    def test_long_prompt_is_cut(self) -> None:
        """Длинный промпт в адресе не нужен: он только подпись к файлу."""
        route = decode(encode(_route(prompt="х" * 5000), SECRET), SECRET)
        assert len(route.prompt) == 200

    def test_token_is_url_safe(self) -> None:
        token = encode(_route(prompt="кот / пёс + ёж"), SECRET)
        assert "/" not in token and "+" not in token and "=" not in token


class TestTampering:
    def test_other_secret_rejected(self) -> None:
        with pytest.raises(BadToken):
            decode(encode(_route(), SECRET), "другой-секрет")

    def test_changed_chat_id_rejected(self) -> None:
        """Главная атака: подменить чат и увести чужой результат себе."""
        token = encode(_route(chat_id=777), SECRET)
        body, _, signature = token.partition(".")

        payload = json.loads(base64.urlsafe_b64decode(body + "=="))
        payload["chat_id"] = 999
        forged = base64.urlsafe_b64encode(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        ).decode().rstrip("=")

        with pytest.raises(BadToken):
            decode(f"{forged}.{signature}", SECRET)

    def test_signature_only_rejected(self) -> None:
        with pytest.raises(BadToken):
            decode(".подпись", SECRET)

    def test_garbage_rejected(self) -> None:
        with pytest.raises(BadToken):
            decode("совсем-не-токен", SECRET)

    def test_empty_rejected(self) -> None:
        with pytest.raises(BadToken):
            decode("", SECRET)


class TestDerivedSecrets:
    """Секреты считаются из токена бота — отдельных переменных нет."""

    @staticmethod
    def _config(token: str) -> Config:
        return Config(
            telegram_token=token,
            allowed_user_ids=frozenset(),
            kie_api_key="k",
            kie_base_url="https://api.kie.ai",
            chat_model="claude-sonnet-5",
            image_model="img",
            video_model="vid",
            public_url="https://bot.example",
            web_host="0.0.0.0",
            web_port=8080,
            database_path="data/t.sqlite3",
            log_level="INFO",
            request_timeout=60.0,
            max_reply_tokens=4096,
            user_rate_limit_per_minute=6,
        )

    def test_stable_for_same_token(self) -> None:
        assert (
            self._config("123:ABC").callback_secret
            == self._config("123:ABC").callback_secret
        )

    def test_different_for_different_tokens(self) -> None:
        assert (
            self._config("123:ABC").callback_secret
            != self._config("456:XYZ").callback_secret
        )

    def test_webhook_and_callback_differ(self) -> None:
        """Один секрет на оба маршрута дал бы доступ к одному через другой."""
        config = self._config("123:ABC")
        assert config.callback_secret != config.telegram_webhook_secret

    def test_callback_url_carries_token(self) -> None:
        config = self._config("123:ABC")
        url = config.callback_url("ТОКЕН")
        assert url.startswith("https://bot.example/callback/kie/")
        assert url.endswith("/ТОКЕН")
