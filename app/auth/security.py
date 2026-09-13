"""MAX signature verification and opaque token primitives, independent of HTTP."""

import hashlib
import hmac
import json
import re
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import unquote

_PARAMETER = re.compile(r"[A-Za-z0-9_]{1,64}\Z")
_BAD_PERCENT_ESCAPE = re.compile(r"%(?![0-9A-Fa-f]{2})")
_SIGNATURE = re.compile(r"[0-9a-f]{64}\Z")
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")


class InvalidInitData(Exception):
    """Untrusted, malformed, expired or future-dated MAX launch data."""


@dataclass(frozen=True)
class MaxIdentity:
    max_user_id: int
    display_name: str
    auth_date: int
    signature: str = field(repr=False)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise InvalidInitData
        result[key] = value
    return result


def verify_max_init_data(
    init_data: str,
    *,
    bot_token: str,
    ttl_seconds: int,
    future_skew_seconds: int,
    now: int | None = None,
) -> MaxIdentity:
    """Implement https://dev.max.ru/docs/webapps/validation plus local age limits.

    Values are decoded exactly once, matching MAX's decodeURIComponent example;
    literal '+' is preserved, and unknown signed fields participate in the MAC.
    """
    if not 1 <= len(init_data) <= 16384:
        raise InvalidInitData
    parts = init_data.split("&")
    if len(parts) > 32:
        raise InvalidInitData
    params: dict[str, str] = {}
    for part in parts:
        key, separator, value = part.partition("=")
        if not separator or not _PARAMETER.fullmatch(key) or key in params:
            raise InvalidInitData
        if _BAD_PERCENT_ESCAPE.search(value):
            raise InvalidInitData
        try:
            decoded = unquote(value, encoding="utf-8", errors="strict")
        except UnicodeError:
            raise InvalidInitData from None
        if "\n" in decoded or "\r" in decoded:
            raise InvalidInitData
        params[key] = decoded
    signature = params.pop("hash", "")
    if not _SIGNATURE.fullmatch(signature):
        raise InvalidInitData
    launch_params = "\n".join(f"{key}={value}" for key, value in sorted(params.items()))
    secret_key = hmac.digest(b"WebAppData", bot_token.encode("utf-8"), "sha256")
    try:
        calculated = hmac.new(secret_key, launch_params.encode("utf-8"), hashlib.sha256).hexdigest()
    except UnicodeError:
        raise InvalidInitData from None
    if not hmac.compare_digest(calculated, signature):
        raise InvalidInitData
    auth_date_value = params.get("auth_date", "")
    if not re.fullmatch(r"[0-9]{1,12}", auth_date_value):
        raise InvalidInitData
    auth_date = int(auth_date_value)
    current = int(time.time()) if now is None else now
    if current - auth_date > ttl_seconds or auth_date > current + future_skew_seconds:
        raise InvalidInitData
    try:
        user = json.loads(params.get("user", ""), object_pairs_hook=_unique_json_object)
    except (ValueError, RecursionError):
        raise InvalidInitData from None
    if not isinstance(user, dict):
        raise InvalidInitData
    max_user_id = user.get("id")
    if type(max_user_id) is not int or not 1 <= max_user_id <= 2**63 - 1:
        raise InvalidInitData
    names = (user.get("first_name"), user.get("last_name"))
    if not isinstance(names[0], str) or (names[1] is not None and not isinstance(names[1], str)):
        raise InvalidInitData
    display_name = " ".join(
        name.strip() for name in names if isinstance(name, str) and name.strip()
    )
    return MaxIdentity(
        max_user_id=max_user_id,
        display_name=(display_name or f"MAX {max_user_id}")[:200],
        auth_date=auth_date,
        signature=signature,
    )


def new_session_token() -> str:
    return secrets.token_urlsafe(32)


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def valid_token_shape(token: str) -> bool:
    return _TOKEN.fullmatch(token) is not None
