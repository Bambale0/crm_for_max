"""Identity needed by use cases, independent of the authentication transport."""

from dataclasses import dataclass
from typing import Protocol

from app.models.identity import User


class ActorContext(Protocol):
    @property
    def user(self) -> User: ...

    @property
    def is_owner(self) -> bool: ...


@dataclass(frozen=True)
class Actor:
    user: User
    is_owner: bool
