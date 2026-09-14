"""DeepSeek V4 Flash classifier with strict JSON parsing and safe failures."""

import json
from dataclasses import dataclass
from types import TracebackType
from typing import Literal, Self

import httpx
from pydantic import BaseModel, Field, SecretStr, model_validator

DEEPSEEK_API_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_MODEL = "deepseek-v4-flash"

SYSTEM_PROMPT = """Ты фильтр сообщений группового чата управляющей компании.
Определи, есть ли в ОДНОМ сообщении реальная проблема, требующая внимания оператора УК.
Игнорируй обычный разговор, споры, ругань, шутки, обсуждение без текущей проблемы,
а также сообщения о том, что проблему уже устранили.
Не выдумывай адрес, причину, ущерб или детали, которых нет в сообщении.

Проблема: коммунальная/домовая неисправность, авария, отсутствие услуги,
сломанное общее оборудование, мусор/уборка, протечка, отопление, свет, вода,
канализация, лифт, газ, дым, искрение и подобное.

severity="urgent" только при явной потенциальной опасности: пожар, дым, газ,
искрение/короткое замыкание, прорыв/активное затопление или застрявшие люди.
Иначе severity="normal".

Верни только JSON строго такого вида:
{"is_problem":true,"problem":"кратко и без домыслов","confidence":0.95,"severity":"normal"}
или
{"is_problem":false,"problem":"","confidence":0.95,"severity":"normal"}
"""


class DeepSeekResult(BaseModel):
    is_problem: bool
    problem: str
    confidence: float = Field(ge=0, le=1)
    severity: Literal["normal", "urgent"]

    @model_validator(mode="after")
    def valid_problem(self) -> Self:
        self.problem = " ".join(self.problem.split())[:1000]
        if self.is_problem and not self.problem:
            raise ValueError("problem is required when is_problem=true")
        if not self.is_problem:
            self.problem = ""
            self.severity = "normal"
        return self


@dataclass(frozen=True, slots=True)
class DeepSeekFailure(Exception):
    code: Literal[
        "http_error",
        "timeout",
        "network_error",
        "invalid_response",
        "invalid_configuration",
    ]

    def __str__(self) -> str:
        return f"DeepSeek classification: {self.code}"


class DeepSeekClassifier:
    def __init__(
        self,
        api_key: SecretStr,
        *,
        base_url: str = DEEPSEEK_API_BASE_URL,
        model: str = DEEPSEEK_MODEL,
        timeout_seconds: float = 3,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if base_url.rstrip("/") != DEEPSEEK_API_BASE_URL or model != DEEPSEEK_MODEL:
            raise DeepSeekFailure("invalid_configuration")
        self.model = model
        self.client = httpx.AsyncClient(
            base_url=DEEPSEEK_API_BASE_URL,
            headers={
                "Authorization": f"Bearer {api_key.get_secret_value()}",
                "Content-Type": "application/json",
            },
            timeout=httpx.Timeout(timeout_seconds),
            follow_redirects=False,
            transport=transport,
        )

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        await self.close()

    async def close(self) -> None:
        await self.client.aclose()

    async def classify(self, text: str) -> DeepSeekResult:
        message = " ".join(text.strip().split())[:4000]
        if not message:
            raise DeepSeekFailure("invalid_response")
        try:
            response = await self.client.post(
                "/chat/completions",
                json={
                    "model": self.model,
                    "messages": [
                        {"role": "system", "content": SYSTEM_PROMPT},
                        {"role": "user", "content": message},
                    ],
                    "thinking": {"type": "enabled"},
                    "reasoning_effort": "high",
                    "response_format": {"type": "json_object"},
                    "max_tokens": 2048,
                },
            )
        except httpx.TimeoutException:
            raise DeepSeekFailure("timeout") from None
        except httpx.RequestError:
            raise DeepSeekFailure("network_error") from None

        if response.status_code != 200:
            raise DeepSeekFailure("http_error")

        try:
            payload = response.json()
            content = payload["choices"][0]["message"]["content"]
            if not isinstance(content, str) or not content.strip():
                raise ValueError
            parsed = json.loads(content)
            return DeepSeekResult.model_validate(parsed)
        except (KeyError, IndexError, TypeError, ValueError):
            raise DeepSeekFailure("invalid_response") from None
