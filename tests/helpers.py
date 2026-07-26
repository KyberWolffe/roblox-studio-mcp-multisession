from __future__ import annotations

import secrets
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Optional

from studio_mcp_v2.auth import Principal
from studio_mcp_v2.catalog import ToolCatalog
from studio_mcp_v2.registry import Registration, SessionRegistry
from studio_mcp_v2.service import ProxyService
from studio_mcp_v2.session import LongPollTransport, StudioSession


PROJECT_ROOT = Path(__file__).resolve().parent.parent
CATALOG_PATH = PROJECT_ROOT / "config" / "tool-catalog.json"
ALLOW_ALL = Principal.create("test-client")


@dataclass
class FakeStudio:
    registry: SessionRegistry
    session: StudioSession
    registration: Registration
    transport: LongPollTransport
    name: str
    capabilities: frozenset
    client_instance_id: str
    registration_secret: str

    @classmethod
    async def create(
        cls,
        registry: SessionRegistry,
        name: str,
        capabilities: Iterable[str],
        *,
        document_epoch: Optional[str] = None,
    ) -> "FakeStudio":
        transport = LongPollTransport()
        capability_set = frozenset(capabilities)
        client_instance_id = str(uuid.uuid4())
        registration_secret = secrets.token_urlsafe(48)
        session, registration = await registry.register(
            client_instance_id=client_instance_id,
            registration_secret=registration_secret,
            document_epoch=document_epoch or str(uuid.uuid4()),
            metadata={"name": name, "mode": "edit", "mock": True},
            capabilities=capability_set,
            transport=transport,
        )
        return cls(
            registry,
            session,
            registration,
            transport,
            name,
            capability_set,
            client_instance_id,
            registration_secret,
        )

    @property
    def studio_id(self) -> str:
        return self.registration.studio_id

    @property
    def generation(self) -> int:
        return self.registration.generation

    @property
    def resume_token(self) -> str:
        return self.registration.resume_token

    async def next_request(self, timeout: float = 1.0) -> Dict[str, Any]:
        result = await self.transport.poll(timeout)
        assert result is not None, "expected a Studio request, got poll timeout"
        return result

    def respond(
        self,
        request: Dict[str, Any],
        result: Any,
        *,
        success: bool = True,
        error: Any = None,
    ) -> bool:
        return self.registry.receive_response(
            self.studio_id,
            self.generation,
            self.resume_token,
            request["request_id"],
            success=success,
            result=result,
            error=error,
        )

    def event(self, event_type: str, payload: Dict[str, Any]) -> bool:
        return self.registry.receive_event(
            self.studio_id,
            self.generation,
            self.resume_token,
            event_type,
            payload,
        )

    def disconnect(self) -> bool:
        return self.registry.disconnect(
            self.studio_id,
            self.generation,
            self.resume_token,
            "fake disconnect",
        )

    async def reconnect(self, settled_request_ids=()) -> "FakeStudio":
        next_transport = LongPollTransport()
        session, registration = await self.registry.register(
            client_instance_id=self.client_instance_id,
            registration_secret=self.registration_secret,
            document_epoch=self.registration.document_epoch,
            metadata={"name": self.name, "mode": "edit", "mock": True},
            capabilities=self.capabilities,
            studio_id=self.studio_id,
            resume_token=self.resume_token,
            reconnect_id=str(uuid.uuid4()),
            settled_request_ids=settled_request_ids,
            transport=next_transport,
        )
        old = FakeStudio(
            self.registry,
            self.session,
            self.registration,
            self.transport,
            self.name,
            self.capabilities,
            self.client_instance_id,
            self.registration_secret,
        )
        self.session = session
        self.registration = registration
        self.transport = next_transport
        self.old_connection = old
        return old


def make_service():
    registry = SessionRegistry()
    catalog = ToolCatalog.from_file(CATALOG_PATH)
    return registry, catalog, ProxyService(registry, catalog)
