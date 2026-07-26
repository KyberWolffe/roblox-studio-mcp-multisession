from __future__ import annotations

import copy
from typing import Any, Dict, Optional

from .auth import AuthorizationPolicy, Principal
from .catalog import ToolCatalog
from .errors import ValidationError
from .registry import SessionRegistry
from .validation import (
    validate_arguments,
    validate_request_id,
    validate_studio_id,
    validate_timeout_ms,
)


class ProxyService:
    """The only operational router; target is mandatory in its public API."""

    def __init__(
        self,
        registry: SessionRegistry,
        catalog: ToolCatalog,
        policy: Optional[AuthorizationPolicy] = None,
    ) -> None:
        self.registry = registry
        self.catalog = catalog
        self.policy = policy or AuthorizationPolicy()

    def list_studios(self, principal: Principal) -> Dict[str, Any]:
        studios = [
            snapshot
            for snapshot in self.registry.snapshots()
            if self.policy.can_discover(principal, snapshot["studio_id"])
        ]
        return {"studios": studios}

    async def call_tool(
        self,
        principal: Principal,
        public_tool: str,
        arguments: Any,
        *,
        client_request_id: Optional[str] = None,
    ) -> Any:
        definition = self.catalog.get(public_tool)
        args = validate_arguments(arguments)
        if "studio_id" not in args:
            raise ValidationError("Missing required parameter: studio_id")
        studio_id = validate_studio_id(args["studio_id"])
        # Authorization is a separate decision and is repeated after queue wait
        # by keeping it outside the routing identity itself.
        self.policy.authorize(principal, studio_id, public_tool)
        session = self.registry.require(studio_id, connected=True)
        if definition.remote_name not in session.capabilities:
            from .errors import CapabilityError

            raise CapabilityError(
                "The targeted Studio does not advertise " + definition.remote_name
            )
        remote_arguments = copy.deepcopy(args)
        remote_arguments.pop("studio_id", None)
        timeout_ms = validate_timeout_ms(remote_arguments.pop("_timeout_ms", None))
        operation_request_id = (
            validate_request_id(client_request_id)
            if client_request_id is not None
            else None
        )
        return await session.invoke(
            definition.remote_name,
            remote_arguments,
            timeout_ms,
            request_id=operation_request_id,
            before_dispatch=lambda: self.policy.authorize(
                principal, studio_id, public_tool
            ),
        )

    def start_job(
        self,
        principal: Principal,
        studio_id_value: Any,
        public_tool: Any,
        tool_arguments: Any,
        timeout_ms_value: Any = None,
    ) -> Dict[str, Any]:
        studio_id = validate_studio_id(studio_id_value)
        if not isinstance(public_tool, str):
            raise ValidationError("tool_name must be a string")
        definition = self.catalog.get(public_tool)
        self.policy.authorize(
            principal, studio_id, "start_studio_job_v2"
        )
        self.policy.authorize(principal, studio_id, public_tool)
        session = self.registry.require(studio_id, connected=True)
        if definition.remote_name not in session.capabilities:
            from .errors import CapabilityError

            raise CapabilityError(
                "The targeted Studio does not advertise " + definition.remote_name
            )
        arguments = validate_arguments(tool_arguments)
        if "studio_id" in arguments:
            raise ValidationError(
                "tool_arguments must not contain a second studio_id"
            )
        timeout_ms = validate_timeout_ms(timeout_ms_value)

        def reauthorize_job() -> None:
            self.policy.authorize(
                principal, studio_id, "start_studio_job_v2"
            )
            self.policy.authorize(principal, studio_id, public_tool)

        job = session.start_job(
            public_tool,
            definition.remote_name,
            arguments,
            timeout_ms,
            before_dispatch=reauthorize_job,
        )
        return job.snapshot()

    def get_job(
        self, principal: Principal, studio_id_value: Any, job_id: Any
    ) -> Dict[str, Any]:
        studio_id = validate_studio_id(studio_id_value)
        if not isinstance(job_id, str) or not job_id:
            raise ValidationError("job_id must be a non-empty string")
        self.policy.authorize(principal, studio_id, "get_studio_job_v2")
        session = self.registry.require(studio_id, connected=False)
        return session.get_job(job_id).snapshot()

    def cancel_job(
        self, principal: Principal, studio_id_value: Any, job_id: Any
    ) -> Dict[str, Any]:
        studio_id = validate_studio_id(studio_id_value)
        if not isinstance(job_id, str) or not job_id:
            raise ValidationError("job_id must be a non-empty string")
        self.policy.authorize(principal, studio_id, "cancel_studio_job_v2")
        session = self.registry.require(studio_id, connected=False)
        return session.cancel_job(job_id).snapshot()
