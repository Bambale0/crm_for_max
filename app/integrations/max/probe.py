"""Run with python -m app.integrations.max.probe --bot observer|staff."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass

from pydantic import ValidationError

from app.core.config import Settings
from app.integrations.max.client import MaxAPIError, MaxReadOnlyClient, Permission


@dataclass(frozen=True, slots=True)
class ProbeResult:
    """Local report fields, not a provider payload or a readiness guarantee."""

    bot_id: int
    is_bot: bool
    has_subscriptions: bool
    membership_checked: bool
    is_admin: bool | None
    permissions: tuple[Permission, ...] | None
    read_all_messages: bool | None
    end_to_end_verified: bool = False


async def probe_bot(client: MaxReadOnlyClient, *, chat_id: int | None = None) -> ProbeResult:
    bot = await client.get_me()
    has_subscriptions = await client.has_subscriptions()
    membership = await client.get_membership(chat_id) if chat_id is not None else None
    if membership is not None and membership.user_id != bot.user_id:
        raise MaxAPIError("invalid_response")
    return ProbeResult(
        bot_id=bot.user_id,
        is_bot=bot.is_bot,
        has_subscriptions=has_subscriptions,
        membership_checked=membership is not None,
        is_admin=membership.is_admin if membership is not None else None,
        permissions=(
            tuple(membership.permissions)
            if membership is not None and membership.permissions is not None
            else None
        ),
        read_all_messages=membership.read_all_messages if membership is not None else None,
    )


async def _run(settings: Settings, *, bot: str, chat_id: int | None) -> ProbeResult:
    token = settings.max_observer_token if bot == "observer" else settings.max_staff_token
    if token is None or not token.get_secret_value():
        raise MaxAPIError("invalid_configuration")
    async with MaxReadOnlyClient(
        token=token,
        base_url=str(settings.max_api_base_url),
        timeout_seconds=settings.max_timeout_seconds,
    ) as client:
        return await probe_bot(client, chat_id=chat_id)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Read-only MAX bot configuration diagnostic")
    parser.add_argument("--bot", choices=("observer", "staff"), required=True)
    parser.add_argument("--chat-id", type=int, help="Optional test chat ID for membership check")
    args = parser.parse_args(argv)
    try:
        settings = Settings()
        token = settings.max_observer_token if args.bot == "observer" else settings.max_staff_token
        if token is None or not token.get_secret_value():
            print(json.dumps({"error": "missing_token"}), file=sys.stderr)
            return 2
        report = asyncio.run(_run(settings, bot=args.bot, chat_id=args.chat_id))
    except (ValidationError, OSError):
        print(json.dumps({"error": "invalid_configuration"}), file=sys.stderr)
        return 2
    except MaxAPIError as error:
        print(json.dumps({"error": error.code}), file=sys.stderr)
        return 2 if error.code == "invalid_configuration" else 1
    print(json.dumps(asdict(report)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
