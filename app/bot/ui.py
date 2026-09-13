"""Shared native MAX bot UI primitives."""

from sqlalchemy.ext.asyncio import AsyncSession

from app.auth.actor import Actor
from app.bot.identity import access_stamp
from app.models.bot import BotDelivery

Buttons = list[list[dict[str, str]]]


def button(text: str, payload: str) -> dict[str, str]:
    return {"text": text[:128], "payload": payload}


async def reply(
    db: AsyncSession,
    actor: Actor,
    text: str,
    buttons: Buttons | None = None,
    *,
    callback_id: str | None = None,
) -> None:
    db.add(
        BotDelivery(
            max_user_id=actor.user.max_user_id,
            access_stamp=await access_stamp(db, actor),
            text=text[:4000],
            buttons=buttons,
            callback_id=callback_id,
        )
    )
