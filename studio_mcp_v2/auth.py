from __future__ import annotations

from dataclasses import dataclass
from typing import FrozenSet, Iterable

from .errors import AuthorizationError


@dataclass(frozen=True)
class Principal:
    """Authorization identity. A studio ID never creates or expands this scope."""

    name: str
    allowed_studios: FrozenSet[str]
    allowed_tools: FrozenSet[str]

    @classmethod
    def create(
        cls,
        name: str,
        allowed_studios: Iterable[str] = ("*",),
        allowed_tools: Iterable[str] = ("*",),
    ) -> "Principal":
        return cls(name, frozenset(allowed_studios), frozenset(allowed_tools))


class AuthorizationPolicy:
    def authorize(self, principal: Principal, studio_id: str, tool_name: str) -> None:
        studio_allowed = (
            "*" in principal.allowed_studios or studio_id in principal.allowed_studios
        )
        tool_allowed = (
            "*" in principal.allowed_tools or tool_name in principal.allowed_tools
        )
        if not studio_allowed or not tool_allowed:
            raise AuthorizationError(
                "The authenticated client is not authorized for this Studio/tool"
            )

    def can_discover(self, principal: Principal, studio_id: str) -> bool:
        return "*" in principal.allowed_studios or studio_id in principal.allowed_studios

    def can_use_tool(self, principal: Principal, tool_name: str) -> bool:
        return "*" in principal.allowed_tools or tool_name in principal.allowed_tools
