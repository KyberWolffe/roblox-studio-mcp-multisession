from __future__ import annotations

import asyncio
import concurrent.futures
import http.server
import json
import os
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from unittest import mock

from studio_mcp_v2.auth import Principal
from studio_mcp_v2.frontend import (
    HubClient,
    HubClientError,
    _NoRedirectHandler,
)
from studio_mcp_v2.http_api import (
    HubSecurityConfig,
    create_http_server,
    submit_to_loop,
)
from studio_mcp_v2.mcp_stdio import MCPStdioServer
from studio_mcp_v2.mock_studio import MockStudioClient
from studio_mcp_v2.service import ProxyService

from .helpers import CATALOG_PATH
from studio_mcp_v2.catalog import ToolCatalog
from studio_mcp_v2.registry import SessionRegistry


STUDIO_TOKEN = "s" * 48
CLIENT_TOKEN = "c" * 48


class HTTPBoundaryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.registry = SessionRegistry()
        self.catalog = ToolCatalog.from_file(CATALOG_PATH)
        self.service = ProxyService(self.registry, self.catalog)
        self.lifecycle_stop_requested = threading.Event()
        self.server = create_http_server(
            "127.0.0.1",
            0,
            loop=asyncio.get_running_loop(),
            registry=self.registry,
            service=self.service,
            catalog=self.catalog,
            security=HubSecurityConfig(
                studio_token=STUDIO_TOKEN,
                client_token=CLIENT_TOKEN,
                client_principal=Principal.create("http-test"),
                poll_timeout_seconds=0.1,
            ),
            shutdown_callback=self.lifecycle_stop_requested.set,
        )
        self.thread = threading.Thread(
            target=self.server.serve_forever, daemon=True
        )
        self.thread.start()
        port = self.server.server_address[1]
        self.url = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        await asyncio.to_thread(self.server.shutdown)
        self.server.server_close()
        self.thread.join(timeout=2)

    async def _request(self, path, token, payload, origin=None):
        encoded = json.dumps(payload).encode("utf-8")
        headers = {
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        }
        if origin is not None:
            headers["Origin"] = origin
        request = urllib.request.Request(
            self.url + path, data=encoded, method="POST", headers=headers
        )

        def send():
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    return response.status, json.loads(response.read())
            except urllib.error.HTTPError as exc:
                return exc.code, json.loads(exc.read())

        return await asyncio.to_thread(send)

    async def test_browser_origin_is_rejected_before_dispatch(self):
        status, payload = await self._request(
            "/v2/client/list",
            CLIENT_TOKEN,
            {},
            origin="http://evil.example",
        )
        self.assertEqual(401, status)
        self.assertEqual("authentication_failed", payload["error"]["code"])

    async def test_server_bind_never_resolves_loopback_hostname(self):
        with mock.patch(
            "socket.getfqdn",
            side_effect=AssertionError("loopback DNS lookup is forbidden"),
        ) as lookup:
            server = create_http_server(
                "127.0.0.1",
                0,
                loop=asyncio.get_running_loop(),
                registry=self.registry,
                service=self.service,
                catalog=self.catalog,
                security=HubSecurityConfig(
                    studio_token=STUDIO_TOKEN,
                    client_token=CLIENT_TOKEN,
                    client_principal=Principal.create("dns-free-test"),
                ),
            )
        try:
            self.assertEqual("127.0.0.1", server.server_name)
            self.assertGreater(server.server_port, 0)
            lookup.assert_not_called()
        finally:
            server.server_close()

    async def test_studio_and_client_tokens_are_separate(self):
        status, _ = await self._request(
            "/v2/client/tools", STUDIO_TOKEN, {}
        )
        self.assertEqual(401, status)
        status, payload = await self._request(
            "/v2/client/tools", CLIENT_TOKEN, {}
        )
        self.assertEqual(200, status)
        names = {tool["name"] for tool in payload["result"]["tools"]}
        self.assertIn("list_roblox_studios_v2", names)
        self.assertNotIn("set_active_studio", names)

    async def test_frontend_loopback_auth_ignores_ambient_proxies(self):
        hostile_proxy = "http://127.0.0.1:9"
        with mock.patch.dict(
            os.environ,
            {
                "HTTP_PROXY": hostile_proxy,
                "HTTPS_PROXY": hostile_proxy,
                "ALL_PROXY": hostile_proxy,
                "NO_PROXY": "",
                "http_proxy": hostile_proxy,
                "https_proxy": hostile_proxy,
                "all_proxy": hostile_proxy,
                "no_proxy": "",
            },
            clear=False,
        ):
            client = HubClient(self.url, CLIENT_TOKEN, timeout_seconds=3)
            result = await asyncio.to_thread(client.lifecycle_status)
        self.assertEqual("studio-mcp-v2", result["service"])
        self.assertTrue(
            any(
                isinstance(handler, _NoRedirectHandler)
                for handler in client._opener.handlers
            )
        )

    async def test_mock_studio_loopback_auth_ignores_ambient_proxies(self):
        hostile_proxy = "http://127.0.0.1:9"
        with mock.patch.dict(
            os.environ,
            {
                "HTTP_PROXY": hostile_proxy,
                "HTTPS_PROXY": hostile_proxy,
                "ALL_PROXY": hostile_proxy,
                "NO_PROXY": "",
            },
            clear=False,
        ):
            client = MockStudioClient(
                self.url,
                STUDIO_TOKEN,
                "Proxy-isolated mock",
                [],
            )
            await asyncio.to_thread(client.connect)
        self.assertIsNotNone(client.studio_id)

    async def test_mock_studio_rejects_non_loopback_hub(self):
        with self.assertRaisesRegex(ValueError, "explicit loopback"):
            MockStudioClient(
                "https://example.invalid",
                STUDIO_TOKEN,
                "unsafe mock",
                [],
            )

    async def test_frontend_never_redirects_bearer_request(self):
        redirected = threading.Event()

        class Sink(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                redirected.set()
                self.send_response(204)
                self.end_headers()

            do_GET = do_POST

            def log_message(self, *_args):
                return

        sink = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Sink)
        sink_thread = threading.Thread(target=sink.serve_forever, daemon=True)
        sink_thread.start()
        sink_port = sink.server_address[1]

        class Redirector(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                self.send_response(302)
                self.send_header(
                    "Location",
                    f"http://127.0.0.1:{sink_port}/capture",
                )
                self.end_headers()

            def log_message(self, *_args):
                return

        redirector = http.server.ThreadingHTTPServer(
            ("127.0.0.1", 0), Redirector
        )
        redirect_thread = threading.Thread(
            target=redirector.serve_forever, daemon=True
        )
        redirect_thread.start()
        try:
            client = HubClient(
                "http://127.0.0.1:"
                + str(redirector.server_address[1]),
                CLIENT_TOKEN,
                timeout_seconds=3,
            )
            with self.assertRaises(HubClientError):
                await asyncio.to_thread(client.lifecycle_status)
            self.assertFalse(redirected.wait(0.1))
        finally:
            redirector.shutdown()
            redirector.server_close()
            redirect_thread.join(timeout=2)
            sink.shutdown()
            sink.server_close()
            sink_thread.join(timeout=2)

    async def test_frontend_bounds_malformed_http_error_envelopes(self):
        malformed_bodies = [
            b"[]",
            b"null",
            b'{"error":"bad"}',
            b'{"error":null}',
        ]

        for body in malformed_bodies:
            with self.subTest(body=body):
                class MalformedError(http.server.BaseHTTPRequestHandler):
                    def do_POST(self):
                        self.send_response(400)
                        self.send_header("Content-Type", "application/json")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)

                    def log_message(self, *_args):
                        return

                server = http.server.ThreadingHTTPServer(
                    ("127.0.0.1", 0), MalformedError
                )
                thread = threading.Thread(
                    target=server.serve_forever, daemon=True
                )
                thread.start()
                try:
                    client = HubClient(
                        "http://127.0.0.1:"
                        + str(server.server_address[1]),
                        CLIENT_TOKEN,
                        timeout_seconds=3,
                    )
                    with self.assertRaises(HubClientError) as raised:
                        await asyncio.to_thread(client.lifecycle_status)
                    self.assertEqual(
                        "v2 hub returned an HTTP error",
                        raised.exception.message,
                    )
                    self.assertNotIn(
                        body.decode("utf-8"), str(raised.exception)
                    )
                    self.assertNotIn(CLIENT_TOKEN, str(raised.exception))
                finally:
                    server.shutdown()
                    server.server_close()
                    thread.join(timeout=2)

    async def test_lifecycle_status_is_authenticated_and_secret_free(self):
        status, _ = await self._request(
            "/v2/client/lifecycle/status", STUDIO_TOKEN, {}
        )
        self.assertEqual(401, status)
        status, payload = await self._request(
            "/v2/client/lifecycle/status", CLIENT_TOKEN, {}
        )
        self.assertEqual(200, status)
        result = payload["result"]
        self.assertEqual("studio-mcp-v2", result["service"])
        self.assertEqual(0, result["connected_session_count"])
        self.assertTrue(result["stop_safe"])
        rendered = json.dumps(payload)
        self.assertNotIn(STUDIO_TOKEN, rendered)
        self.assertNotIn(CLIENT_TOKEN, rendered)

    async def test_lifecycle_stop_refuses_non_edit_session(self):
        status, registration = await self._request(
            "/v2/studios/connect",
            STUDIO_TOKEN,
            {
                "client_instance_id": str(uuid.uuid4()),
                "registration_secret": "r" * 48,
                "document_epoch": "play-document",
                "metadata": {"name": "play", "mode": "play"},
                "capabilities": [],
            },
        )
        self.assertEqual(200, status)
        status, current = await self._request(
            "/v2/client/lifecycle/status", CLIENT_TOKEN, {}
        )
        self.assertEqual(200, status)
        self.assertFalse(current["result"]["stop_safe"])
        self.assertEqual(
            current["result"]["broker_instance_id"],
            registration["result"]["broker_instance_id"],
        )
        self.assertEqual(
            ["mode_not_edit"],
            current["result"]["stop_blockers"][0]["reasons"],
        )
        status, payload = await self._request(
            "/v2/client/lifecycle/stop",
            CLIENT_TOKEN,
            {
                "broker_instance_id": current["result"][
                    "broker_instance_id"
                ]
            },
        )
        self.assertEqual(409, status)
        self.assertEqual("studio_conflict", payload["error"]["code"])
        self.assertFalse(self.lifecycle_stop_requested.is_set())
        self.assertEqual(
            registration["result"]["studio_id"],
            payload["error"]["details"]["stop_blockers"][0]["studio_id"],
        )

    async def test_lifecycle_stop_accepts_clean_connected_edit_session(self):
        status, registration_payload = await self._request(
            "/v2/studios/connect",
            STUDIO_TOKEN,
            {
                "client_instance_id": str(uuid.uuid4()),
                "registration_secret": "e" * 48,
                "document_epoch": "edit-document",
                "metadata": {"name": "edit", "mode": "edit"},
                "capabilities": [],
            },
        )
        self.assertEqual(200, status)
        registration = registration_payload["result"]
        _, current = await self._request(
            "/v2/client/lifecycle/status", CLIENT_TOKEN, {}
        )
        self.assertTrue(current["result"]["stop_safe"])
        status, payload = await self._request(
            "/v2/client/lifecycle/stop",
            CLIENT_TOKEN,
            {
                "broker_instance_id": current["result"][
                    "broker_instance_id"
                ]
            },
        )
        self.assertEqual(200, status)
        self.assertTrue(payload["result"]["stopping"])
        self.assertTrue(self.lifecycle_stop_requested.is_set())
        status, payload = await self._request(
            "/v2/client/call",
            CLIENT_TOKEN,
            {
                "tool_name": "get_console_output_v2",
                "arguments": {
                    "studio_id": "00000000-0000-4000-8000-000000000001"
                },
                "client_request_id": "after-stop",
            },
        )
        self.assertEqual(409, status)
        self.assertIn("shutdown is fenced", payload["error"]["message"])
        status, payload = await self._request(
            "/v2/studios/connect",
            STUDIO_TOKEN,
            {
                "client_instance_id": str(uuid.uuid4()),
                "registration_secret": "n" * 48,
                "document_epoch": "after-stop-document",
                "metadata": {"name": "late", "mode": "edit"},
                "capabilities": [],
            },
        )
        self.assertEqual(409, status)
        self.assertIn("shutdown is fenced", payload["error"]["message"])
        status, payload = await self._request(
            "/v2/studios/event",
            STUDIO_TOKEN,
            {
                "studio_id": registration["studio_id"],
                "generation": registration["generation"],
                "resume_token": registration["resume_token"],
                "event_type": "mode_changed",
                "payload": {"mode": "play"},
            },
        )
        self.assertEqual(409, status)
        self.assertIn("Studio state changes", payload["error"]["message"])

    async def test_lifecycle_stop_refuses_active_client_admission(self):
        self.server.begin_client_operation()
        try:
            _, current = await self._request(
                "/v2/client/lifecycle/status", CLIENT_TOKEN, {}
            )
            self.assertEqual(
                1, current["result"]["active_client_operation_count"]
            )
            status, payload = await self._request(
                "/v2/client/lifecycle/stop",
                CLIENT_TOKEN,
                {
                    "broker_instance_id": current["result"][
                        "broker_instance_id"
                    ]
                },
            )
        finally:
            self.server.end_client_operation()
        self.assertEqual(409, status)
        self.assertEqual(
            1,
            payload["error"]["details"]["active_client_operation_count"],
        )
        self.assertFalse(self.lifecycle_stop_requested.is_set())
        _, after = await self._request(
            "/v2/client/lifecycle/status", CLIENT_TOKEN, {}
        )
        self.assertFalse(after["result"]["lifecycle_stopping"])

    async def test_lifecycle_stop_refuses_active_studio_mutation(self):
        self.server.begin_studio_mutation()
        try:
            _, current = await self._request(
                "/v2/client/lifecycle/status", CLIENT_TOKEN, {}
            )
            self.assertEqual(
                1, current["result"]["active_studio_mutation_count"]
            )
            self.assertIn(
                "active_studio_mutations",
                current["result"]["lifecycle_blockers"],
            )
            status, payload = await self._request(
                "/v2/client/lifecycle/stop",
                CLIENT_TOKEN,
                {
                    "broker_instance_id": current["result"][
                        "broker_instance_id"
                    ]
                },
            )
        finally:
            self.server.end_studio_mutation()
        self.assertEqual(409, status)
        self.assertEqual(
            1,
            payload["error"]["details"]["active_studio_mutation_count"],
        )
        self.assertFalse(self.lifecycle_stop_requested.is_set())
        _, after = await self._request(
            "/v2/client/lifecycle/status", CLIENT_TOKEN, {}
        )
        self.assertFalse(after["result"]["lifecycle_stopping"])

    async def test_two_http_mock_sessions_complete_concurrently(self):
        async def register(name):
            status, payload = await self._request(
                "/v2/studios/connect",
                STUDIO_TOKEN,
                {
                    "client_instance_id": str(
                        uuid.uuid5(uuid.NAMESPACE_DNS, "http-mock-" + name)
                    ),
                    "registration_secret": name * 48,
                    "document_epoch": name + "-document",
                    "metadata": {"name": name, "mode": "edit", "mock": True},
                    "capabilities": ["get_console_output"],
                },
            )
            self.assertEqual(200, status)
            return payload["result"]

        a, b = await asyncio.gather(register("A"), register("B"))

        def connection_payload(registration):
            return {
                "studio_id": registration["studio_id"],
                "generation": registration["generation"],
                "resume_token": registration["resume_token"],
            }

        call_a = asyncio.create_task(
            self._request(
                "/v2/client/call",
                CLIENT_TOKEN,
                {
                    "tool_name": "get_console_output_v2",
                    "arguments": {"studio_id": a["studio_id"]},
                    "client_request_id": "http-A",
                },
            )
        )
        call_b = asyncio.create_task(
            self._request(
                "/v2/client/call",
                CLIENT_TOKEN,
                {
                    "tool_name": "get_console_output_v2",
                    "arguments": {"studio_id": b["studio_id"]},
                    "client_request_id": "http-B",
                },
            )
        )
        (_, poll_a), (_, poll_b) = await asyncio.gather(
            self._request(
                "/v2/studios/poll",
                STUDIO_TOKEN,
                connection_payload(a),
            ),
            self._request(
                "/v2/studios/poll",
                STUDIO_TOKEN,
                connection_payload(b),
            ),
        )
        request_a = poll_a["result"]
        request_b = poll_b["result"]
        self.assertEqual(a["studio_id"], request_a["studio_id"])
        self.assertEqual(b["studio_id"], request_b["studio_id"])
        # Reverse response order to exercise composite correlation.
        await self._request(
            "/v2/studios/response",
            STUDIO_TOKEN,
            {
                **connection_payload(b),
                "request_id": request_b["request_id"],
                "success": True,
                "result": "from B",
            },
        )
        await self._request(
            "/v2/studios/response",
            STUDIO_TOKEN,
            {
                **connection_payload(a),
                "request_id": request_a["request_id"],
                "success": True,
                "result": "from A",
            },
        )
        (status_a, result_a), (status_b, result_b) = await asyncio.gather(
            call_a, call_b
        )
        self.assertEqual((200, "from A"), (status_a, result_a["result"]))
        self.assertEqual((200, "from B"), (status_b, result_b["result"]))

    async def test_non_loopback_frontend_url_is_refused(self):
        with self.assertRaises(ValueError):
            HubClient("http://example.com:44756", CLIENT_TOKEN)

    async def test_local_api_deadline_cancels_scheduled_coroutine(self):
        cancelled = asyncio.Event()

        async def never_finishes():
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with self.assertRaises(
            (TimeoutError, asyncio.TimeoutError, concurrent.futures.TimeoutError)
        ):
            await asyncio.to_thread(
                submit_to_loop,
                asyncio.get_running_loop(),
                never_finishes(),
                0.01,
            )
        await asyncio.wait_for(cancelled.wait(), 1)


class FakeHubClient:
    def tools(self):
        return {"tools": []}

    def list_studios(self):
        return {"studios": []}

    def call(self, name, arguments, client_request_id):
        return {
            "name": name,
            "arguments": arguments,
            "request_id": client_request_id,
        }

    def start_job(self, arguments):
        return {"started": arguments}

    def get_job(self, arguments):
        return {"job": arguments}

    def cancel_job(self, arguments):
        return {"cancelled": arguments}


class MCPFrontendTests(unittest.TestCase):
    def test_initialize_keeps_studio_ids_out_of_the_user_workflow(self):
        server = MCPStdioServer(FakeHubClient())
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {},
            }
        )
        self.assertEqual(
            "roblox-studio-mcp-multisession",
            response["result"]["serverInfo"]["name"],
        )
        self.assertEqual("0.4.0-rc.6", response["result"]["serverInfo"]["version"])
        instructions = response["result"]["instructions"]
        self.assertIn("Roblox_Studio_Multisession", instructions)
        self.assertNotIn("Roblox_Studio_v2", instructions)
        self.assertIn("ordinary project/place name", instructions)
        self.assertIn("list_roblox_studios_v2", instructions)
        self.assertIn("metadata.name", instructions)
        self.assertIn("metadata.place_id", instructions)
        self.assertIn("metadata.game_id", instructions)
        self.assertIn("never ask them to copy", instructions)
        self.assertIn("Parallel tasks", instructions)
        self.assertIn("never fall back silently", instructions)
        self.assertIn("cannot safely multiplex", instructions)

    def test_frontend_exposes_no_selection_and_generates_correlation(self):
        server = MCPStdioServer(FakeHubClient())
        response = server.handle(
            {
                "jsonrpc": "2.0",
                "id": 7,
                "method": "tools/call",
                "params": {
                    "name": "get_console_output_v2",
                    "arguments": {
                        "studio_id": "00000000-0000-0000-0000-000000000001"
                    },
                },
            }
        )
        self.assertEqual(7, response["id"])
        content = json.loads(response["result"]["content"][0]["text"])
        self.assertEqual("get_console_output_v2", content["name"])
        self.assertEqual(36, len(content["request_id"]))


if __name__ == "__main__":
    unittest.main()
