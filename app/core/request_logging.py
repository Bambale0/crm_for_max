"""Access events contain no query strings, credentials or request/response bodies."""

import logging
import time
import uuid

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

logger = logging.getLogger("app.access")


class RequestLoggingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        # Generate IDs server-side: even an apparently valid incoming value can
        # contain a credential. Do not echo or record untrusted header values.
        request_id = uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started = time.monotonic()
        status_code = 500

        async def send_with_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                if scope["path"].startswith(("/api/auth/", "/api/admin/")):
                    headers["Cache-Control"] = "no-store"
            await send(message)

        try:
            await self.app(scope, receive, send_with_id)
        finally:
            route = scope.get("route")
            logger.info(
                "http_request request_id=%s method=%s route=%s status=%d duration_ms=%d",
                request_id,
                scope["method"],
                getattr(route, "path", "unmatched"),
                status_code,
                int((time.monotonic() - started) * 1000),
            )
