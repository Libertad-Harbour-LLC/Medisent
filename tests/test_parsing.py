"""Тесты на два места, где ошибка тихая и дорогая.

Первое — конверт ответа провайдера: код ошибки приезжает в теле с HTTP 200,
и «успешный» ответ может означать «закончились кредиты».
Второе — resultJson: JSON, упакованный в строку внутри JSON.
"""

from __future__ import annotations

import json

import httpx
import pytest

from bot.services.args import ArgError, parse
from bot.services.jobs import ImageParams, VideoParams, parse_callback
from bot.services.kie import KieClient, KieError


@pytest.fixture
def client() -> KieClient:
    return KieClient("test-key", "https://api.example.com")


def _response(payload: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, json=payload, request=httpx.Request("POST", "https://api.example.com/x")
    )


class TestErrorEnvelope:
    def test_ok_passes_through(self, client: KieClient) -> None:
        body = client._unwrap(_response({"code": 200, "msg": "success", "data": {"taskId": "t1"}}))
        assert body["data"]["taskId"] == "t1"

    def test_no_credits_raises_despite_http_200(self, client: KieClient) -> None:
        with pytest.raises(KieError) as exc:
            client._unwrap(_response({"code": 402, "msg": "Insufficient Quota"}))
        assert exc.value.code == 402

    def test_kling_nests_code_inside_data(self, client: KieClient) -> None:
        """У Kling код продублирован в data — эту ветку легко не заметить."""
        with pytest.raises(KieError) as exc:
            client._unwrap(_response({"data": {"code": "422", "msg": "bad params"}}))
        assert exc.value.code == 422

    def test_claude_response_without_code_is_fine(self, client: KieClient) -> None:
        """У /claude/v1/messages конверта с code нет вообще."""
        body = client._unwrap(_response({"role": "assistant", "content": []}))
        assert body["role"] == "assistant"

    def test_rate_limit_is_retryable(self) -> None:
        assert KieError(429, "slow down").retryable
        assert not KieError(401, "bad key").retryable


class TestCallbackParsing:
    def test_double_encoded_result_json(self) -> None:
        result = parse_callback(
            {
                "code": 200,
                "msg": "Playground task completed successfully.",
                "data": {
                    "taskId": "t1",
                    "state": "success",
                    "resultJson": json.dumps({"resultUrls": ["https://cdn/x.png"]}),
                    "creditsConsumed": 3,
                },
            }
        )
        assert result.success
        assert result.urls == ["https://cdn/x.png"]
        assert result.credits == 3.0

    def test_failure_collects_reason(self) -> None:
        result = parse_callback(
            {
                "code": 501,
                "msg": "Playground task failed.",
                "data": {
                    "taskId": "t2",
                    "state": "fail",
                    "resultJson": None,
                    "failCode": "GENERATION_FAILED",
                    "failMsg": "The generation task failed.",
                },
            }
        )
        assert not result.success
        assert "GENERATION_FAILED" in result.error

    def test_success_without_urls_is_failure(self) -> None:
        """state=success с пустым resultJson — не повод слать пользователю ничего."""
        result = parse_callback(
            {"code": 200, "data": {"taskId": "t3", "state": "success", "resultJson": "{}"}}
        )
        assert not result.success

    def test_broken_result_json_does_not_raise(self) -> None:
        result = parse_callback(
            {"code": 200, "data": {"taskId": "t4", "state": "success", "resultJson": "{не json"}}
        )
        assert not result.success

    def test_missing_task_id_raises(self) -> None:
        with pytest.raises(ValueError):
            parse_callback({"code": 200, "data": {"state": "success"}})


class TestParams:
    def test_one_k_only_ratio_downgrades_resolution(self) -> None:
        payload = ImageParams(prompt="кот", aspect_ratio="9:8", resolution="4K").as_input()
        assert payload["resolution"] == "1K"

    def test_video_duration_is_string(self) -> None:
        """В спеке duration — строка; число провайдер не примет."""
        payload = VideoParams(prompt="кот", duration=8).as_input()
        assert payload["duration"] == "8"

    def test_video_always_sends_multi_prompt(self) -> None:
        """Схема помечает multi_prompt обязательным даже для одиночной сцены."""
        assert VideoParams(prompt="кот").as_input()["multi_prompt"] == []

    def test_reference_frames_drop_aspect_ratio(self) -> None:
        payload = VideoParams(prompt="кот", image_urls=["https://cdn/a.png"]).as_input()
        assert "aspect_ratio" not in payload


class TestArgs:
    def test_flags_are_cut_from_prompt(self) -> None:
        parsed = parse("рыжий кот на окне --ar 16:9 --res 2K")
        assert parsed.prompt == "рыжий кот на окне"
        assert parsed.flags == {"ar": "16:9", "res": "2K"}

    def test_boolean_flag(self) -> None:
        assert parse("кот --sound").flag("sound")

    def test_unknown_choice_raises(self) -> None:
        with pytest.raises(ArgError):
            parse("кот --ar 5:4").choice("ar", ("16:9", "9:16"), "16:9")

    def test_out_of_range_integer_raises(self) -> None:
        with pytest.raises(ArgError):
            parse("кот --sec 99").integer("sec", 5, 3, 15)

    def test_prompt_without_flags(self) -> None:
        assert parse("просто описание").prompt == "просто описание"
