from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from pathlib import Path
from typing import Callable, Iterable, Optional

from .auth import Principal
from .catalog import ToolCatalog
from .http_api import HubRuntimeInfo, HubSecurityConfig, V2HTTPServer, create_http_server
from .registry import SessionRegistry
from .service import ProxyService


def _split_scope(value: str) -> Iterable[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    return items or ["*"]


def _default_catalog() -> Path:
    return (
        Path(__file__).resolve().parent.parent
        / "config"
        / "durable-tool-catalog.json"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Run the isolated Roblox Studio MCP Multisession broker. "
            "This does not discover, stop, or modify the v1 server."
        )
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("STUDIO_MCP_V2_PORT", "44756")),
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=_default_catalog(),
    )
    return parser


async def serve_hub(
    *,
    host: str,
    port: int,
    catalog: ToolCatalog,
    security: HubSecurityConfig,
    runtime_info: Optional[HubRuntimeInfo] = None,
    ready_callback: Optional[Callable[[V2HTTPServer], None]] = None,
    announce: bool = True,
) -> None:
    """Serve one broker process until a signal or authenticated stop request."""

    registry = SessionRegistry()
    service = ProxyService(registry, catalog)
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def request_stop() -> None:
        loop.call_soon_threadsafe(stop.set)

    server = create_http_server(
        host,
        port,
        loop=loop,
        registry=registry,
        service=service,
        catalog=catalog,
        security=security,
        runtime_info=runtime_info,
        shutdown_callback=request_stop,
    )

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, request_stop)
        except NotImplementedError:
            pass

    thread = asyncio.create_task(asyncio.to_thread(server.serve_forever))
    try:
        if ready_callback is not None:
            ready_callback(server)
        if announce:
            sys.stderr.write(
                "Studio MCP Multisession broker listening on "
                f"http://{host}:{server.server_address[1]}.\n"
            )
            sys.stderr.flush()
        await stop.wait()
    finally:
        server.shutdown()
        server.server_close()
        await thread


async def run(args: argparse.Namespace) -> None:
    studio_token = os.environ.get("STUDIO_MCP_V2_STUDIO_TOKEN", "")
    client_token = os.environ.get("STUDIO_MCP_V2_CLIENT_TOKEN", "")
    principal = Principal.create(
        "local-codex-v2",
        _split_scope(os.environ.get("STUDIO_MCP_V2_ALLOWED_STUDIOS", "*")),
        _split_scope(os.environ.get("STUDIO_MCP_V2_ALLOWED_TOOLS", "*")),
    )
    security = HubSecurityConfig(
        studio_token=studio_token,
        client_token=client_token,
        client_principal=principal,
    )
    catalog = ToolCatalog.from_file(args.catalog)
    await serve_hub(
        host=args.host,
        port=args.port,
        catalog=catalog,
        security=security,
    )


def main() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(run(args))
    except (ValueError, OSError) as exc:
        sys.stderr.write(
            "Studio MCP Multisession hub refused to start: "
            + str(exc)
            + "\n"
        )
        raise SystemExit(2)


if __name__ == "__main__":
    main()
