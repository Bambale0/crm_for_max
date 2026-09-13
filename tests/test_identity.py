"""Synthetic MAX signature vectors; no real token or external network needed."""

import hashlib
import hmac
import json
from urllib.parse import quote, urlencode

import pytest

from app.auth.security import InvalidInitData, verify_max_init_data

TEST_TOKEN = "synthetic-max-staff-token"
NOW = 2_000_000_000


def _sign(fields: dict[str, str]) -> str:
    data = "\n".join(f"{key}={fields[key]}" for key in sorted(fields))
    key = hmac.digest(b"WebAppData", TEST_TOKEN.encode(), "sha256")
    signature = hmac.new(key, data.encode(), hashlib.sha256).hexdigest()
    return urlencode({**fields, "hash": signature}, quote_via=quote)


def _fields(*, auth_date: int = NOW, user: object = None) -> dict[str, str]:
    if user is None:
        user = {"id": 101, "first_name": "Тест + = &", "last_name": "User"}
    return {
        "auth_date": str(auth_date),
        "query_id": "synthetic-query",
        "user": json.dumps(user, ensure_ascii=False, separators=(",", ":")),
    }


def _verify(value: str) -> int:
    return verify_max_init_data(
        value, bot_token=TEST_TOKEN, ttl_seconds=300, future_skew_seconds=30, now=NOW
    ).max_user_id


def test_known_signature_from_independent_node_crypto_vector() -> None:
    # Independently calculated with Node crypto.createHmac, not the helper above.
    signature = "9780e887b2a477e0ceb55f5827934e3010ac3abfc0347c950c6cb7770aa3fb65"
    encoded = urlencode({**_fields(), "hash": signature}, quote_via=quote)
    identity = verify_max_init_data(
        encoded, bot_token=TEST_TOKEN, ttl_seconds=300, future_skew_seconds=30, now=NOW
    )
    assert identity.max_user_id == 101
    assert identity.display_name == "Тест + = & User"
    assert signature not in repr(identity)


def test_percent_decoding_once_and_literal_plus_match_max_example() -> None:
    fields = {**_fields(), "start_param": "a+b%26c=value"}
    # MAX's decodeURIComponent leaves literal '+' unchanged.
    signed = _sign(fields).replace("%2B", "+")
    assert _verify(signed) == 101


def test_unknown_signed_fields_are_included_in_mac() -> None:
    signed = _sign({**_fields(), "future_field": "supported"})
    assert _verify(signed) == 101
    with pytest.raises(InvalidInitData):
        _verify(signed.replace("future_field=supported", "future_field=tampered"))


@pytest.mark.parametrize("age", [-30, 0, 300])
def test_launch_age_boundaries_are_accepted(age: int) -> None:
    assert _verify(_sign(_fields(auth_date=NOW - age))) == 101


@pytest.mark.parametrize("age", [-31, 301])
def test_stale_and_future_launches_are_rejected(age: int) -> None:
    with pytest.raises(InvalidInitData):
        _verify(_sign(_fields(auth_date=NOW - age)))


@pytest.mark.parametrize(
    "suffix",
    ["&auth_date=2000000000", "&user=%7B%7D", "&hash=" + "0" * 64, "&user%00=x", "&x=%ZZ"],
)
def test_duplicate_and_malformed_fields_are_rejected(suffix: str) -> None:
    with pytest.raises(InvalidInitData):
        _verify(_sign(_fields()) + suffix)


@pytest.mark.parametrize("user_id", [True, "101", -1, 0, 2**63, 1.1])
def test_user_id_must_be_positive_signed_64_bit_integer(user_id: object) -> None:
    with pytest.raises(InvalidInitData):
        _verify(_sign(_fields(user={"id": user_id, "first_name": "Test"})))


@pytest.mark.parametrize(
    "user_json",
    ['{"id":101,"id":202,"first_name":"Test"}', "[]", '"user"', '{"id":101}'],
)
def test_ambiguous_or_incomplete_user_json_is_rejected(user_json: str) -> None:
    with pytest.raises(InvalidInitData):
        _verify(_sign({**_fields(), "user": user_json}))


@pytest.mark.parametrize("value", ["", "user=value", "hash=" + "0" * 64, "x" * 16385])
def test_invalid_launch_shapes_are_rejected(value: str) -> None:
    with pytest.raises(InvalidInitData):
        _verify(value)


def test_wrong_bot_token_and_unsigned_changes_are_rejected() -> None:
    signed = _sign(_fields())
    with pytest.raises(InvalidInitData):
        verify_max_init_data(
            signed,
            bot_token="another-synthetic-bot",
            ttl_seconds=300,
            future_skew_seconds=30,
            now=NOW,
        )
    with pytest.raises(InvalidInitData):
        _verify(signed.replace("synthetic-query", "tampered-query"))
