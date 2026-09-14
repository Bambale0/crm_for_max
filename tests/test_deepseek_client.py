"""DeepSeek V4 Flash transport contract without live API credentials."""

import json

import httpx
import pytest
from pydantic import SecretStr

from app.integrations.deepseek.client import DeepSeekClassifier, DeepSeekFailure


async def test_deepseek_v4_flash_uses_max_thinking_and_json_output() -> None:
    requests: list[httpx.Request] = []

    def transport(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "reasoning_content": "private provider reasoning",
                            "content": json.dumps(
                                {
                                    "is_problem": True,
                                    "problem": "Не работает лифт.",
                                    "confidence": 0.96,
                                    "severity": "normal",
                                }
                            ),
                        }
                    }
                ]
            },
        )

    async with DeepSeekClassifier(
        SecretStr("synthetic-deepseek-key"),
        transport=httpx.MockTransport(transport),
    ) as client:
        result = await client.classify("Лифт не работает второй час")

    assert result.is_problem
    assert result.problem == "Не работает лифт."
    assert result.confidence == 0.96
    assert len(requests) == 1
    request = requests[0]
    body = json.loads(request.content)
    assert body["model"] == "deepseek-v4-flash"
    assert body["thinking"] == {"type": "enabled"}
    assert body["reasoning_effort"] == "max"
    assert body["response_format"] == {"type": "json_object"}
    assert body["max_tokens"] == 32768
    assert "temperature" not in body
    assert request.headers["Authorization"] == "Bearer synthetic-deepseek-key"


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, json={"choices": [{"message": {"content": ""}}]}),
        httpx.Response(200, json={"choices": [{"message": {"content": "not-json"}}]}),
        httpx.Response(200, json={"choices": []}),
    ],
)
async def test_deepseek_invalid_output_fails_closed(response: httpx.Response) -> None:
    async with DeepSeekClassifier(
        SecretStr("synthetic-deepseek-key"),
        transport=httpx.MockTransport(lambda request: response),
    ) as client:
        with pytest.raises(DeepSeekFailure) as caught:
            await client.classify("Обычное сообщение")
    assert caught.value.code == "invalid_response"


async def test_deepseek_http_failure_has_safe_error() -> None:
    async with DeepSeekClassifier(
        SecretStr("sensitive-deepseek-key"),
        transport=httpx.MockTransport(lambda request: httpx.Response(503)),
    ) as client:
        with pytest.raises(DeepSeekFailure) as caught:
            await client.classify("Лифт не работает")
    assert caught.value.code == "http_error"
    assert "sensitive-deepseek-key" not in str(caught.value)


async def test_deepseek_timeout_has_safe_error() -> None:
    def timeout(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("synthetic timeout", request=request)

    async with DeepSeekClassifier(
        SecretStr("sensitive-deepseek-key"),
        transport=httpx.MockTransport(timeout),
    ) as client:
        with pytest.raises(DeepSeekFailure) as caught:
            await client.classify("Лифт не работает")
    assert caught.value.code == "timeout"
