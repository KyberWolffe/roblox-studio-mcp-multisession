from __future__ import annotations

import argparse
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from typing import Any, Dict, Optional

from .frontend import _direct_loopback_opener


class MockStudioClient:
    """Disposable protocol peer. It never opens Roblox Studio or executes payloads."""

    def __init__(self, base_url: str, token: str, name: str, capabilities):
        parsed = urllib.parse.urlparse(base_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname not in {"127.0.0.1", "::1"}
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError(
                "The mock Studio only connects to an explicit loopback HTTP hub"
            )
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.name = name
        self.capabilities = list(capabilities)
        self.client_instance_id = str(uuid.uuid4())
        self.registration_secret = secrets.token_urlsafe(48)
        self.document_epoch = str(uuid.uuid4())
        self.studio_id: Optional[str] = None
        self.generation: Optional[int] = None
        self.resume_token: Optional[str] = None
        self._opener = _direct_loopback_opener()

    def _post(self, path: str, body: Dict[str, Any]) -> Any:
        encoded = json.dumps(body, separators=(",", ":")).encode("utf-8")
        request = urllib.request.Request(
            self.base_url + path,
            data=encoded,
            method="POST",
            headers={
                "Authorization": "Bearer " + self.token,
                "Content-Type": "application/json",
            },
        )
        with self._opener.open(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
        if payload.get("ok") is not True:
            raise RuntimeError("v2 hub rejected mock Studio")
        return payload.get("result")

    def connect(self) -> None:
        body: Dict[str, Any] = {
            "client_instance_id": self.client_instance_id,
            "registration_secret": self.registration_secret,
            "document_epoch": self.document_epoch,
            "metadata": {
                "name": self.name,
                "place_id": 0,
                "mode": "edit",
                "mock": True,
            },
            "capabilities": self.capabilities,
        }
        if self.studio_id is not None:
            body["studio_id"] = self.studio_id
            body["resume_token"] = self.resume_token
        registration = self._post("/v2/studios/connect", body)
        self.studio_id = registration["studio_id"]
        self.generation = registration["generation"]
        self.resume_token = registration["resume_token"]

    def run(self) -> None:
        self.connect()
        print(
            json.dumps(
                {
                    "mock_studio_id": self.studio_id,
                    "generation": self.generation,
                    "name": self.name,
                }
            ),
            flush=True,
        )
        while True:
            assert self.studio_id and self.generation and self.resume_token
            request = self._post(
                "/v2/studios/poll",
                {
                    "studio_id": self.studio_id,
                    "generation": self.generation,
                    "resume_token": self.resume_token,
                },
            )
            if request is None:
                continue
            arguments = request.get("args", {})
            # Payloads are treated strictly as inert JSON data.
            delay_ms = arguments.get("mock_delay_ms", 0)
            if isinstance(delay_ms, (int, float)) and 0 < delay_ms <= 10_000:
                time.sleep(delay_ms / 1000)
            self._post(
                "/v2/studios/response",
                {
                    "studio_id": self.studio_id,
                    "generation": self.generation,
                    "resume_token": self.resume_token,
                    "request_id": request["request_id"],
                    "success": True,
                    "result": {
                        "mock": True,
                        "studio_id": self.studio_id,
                        "operation": request["operation"],
                        "arguments": arguments,
                    },
                },
            )


def _catalog_remote_names(path: Path):
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [item["name"] for item in payload["tools"]]


def main() -> None:
    package_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument("--name", default="Disposable Mock Studio")
    parser.add_argument(
        "--url",
        default="http://127.0.0.1:"
        + os.environ.get("STUDIO_MCP_V2_PORT", "44756"),
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=package_root / "config" / "durable-tool-catalog.json",
    )
    args = parser.parse_args()
    token = os.environ.get("STUDIO_MCP_V2_STUDIO_TOKEN", "")
    if len(token) < 32:
        raise SystemExit("STUDIO_MCP_V2_STUDIO_TOKEN must be at least 32 chars")
    client = MockStudioClient(
        args.url,
        token,
        args.name,
        _catalog_remote_names(args.catalog),
    )
    try:
        client.run()
    except KeyboardInterrupt:
        pass
    except (urllib.error.URLError, OSError, RuntimeError) as exc:
        raise SystemExit("Mock Studio stopped: " + str(exc))


if __name__ == "__main__":
    main()
