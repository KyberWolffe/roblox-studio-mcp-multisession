from __future__ import annotations

import argparse
import asyncio
import contextlib
import fcntl
import json
import os
import pwd
import socket
import stat
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, Mapping, Optional, Sequence, TextIO

from . import __version__
from .auth import Principal
from .catalog import DISCOVERY_TOOL, JOB_TOOLS, ToolCatalog
from .catalog_review import audit_installed_v1_cache
from .errors import ProxyError
from .frontend import HubClient, HubTransportError
from .http_api import HubRuntimeInfo, HubSecurityConfig
from .hub import serve_hub
from .mcp_stdio import serve_stdio


CONFIG_SCHEMA_VERSION = 1
STATE_SCHEMA_VERSION = 1
HEALTH_TIMEOUT_SECONDS = 0.75
STOP_TIMEOUT_SECONDS = 8.0
MAX_JSON_FILE_BYTES = 1_000_000
MIN_TOKEN_LENGTH = 32
MAX_TOKEN_LENGTH = 512
PRIVATE_FILE_MODE = 0o600
PRIVATE_DIRECTORY_MODE = 0o700
NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
CLOEXEC = getattr(os, "O_CLOEXEC", 0)


class LifecycleError(RuntimeError):
    """A sanitized, user-actionable local lifecycle failure."""


@dataclass(frozen=True)
class InstallPaths:
    root: Path
    config_dir: Path
    runtime_config: Path
    secrets_config: Path
    run_dir: Path
    lock_file: Path
    broker_state: Path
    logs_dir: Path
    broker_log: Path

    @classmethod
    def default(cls) -> "InstallPaths":
        try:
            user_home = Path(pwd.getpwuid(os.getuid()).pw_dir)
        except (KeyError, OSError) as exc:
            raise LifecycleError(
                "Could not resolve the current user's stable home directory"
            ) from exc
        return cls._from_root(
            user_home
            / "Library"
            / "Application Support"
            / "RobloxStudioMCPv2"
        )

    @classmethod
    def for_test(cls, root: Path) -> "InstallPaths":
        """Explicit dependency-injection seam; the installed no-arg path ignores env."""

        return cls._from_root(root)

    @classmethod
    def _from_root(cls, root: Path) -> "InstallPaths":
        if not root.is_absolute():
            raise LifecycleError("Lifecycle support root must be an absolute path")
        config_dir = root / "config"
        run_dir = root / "run"
        logs_dir = root / "logs"
        return cls(
            root=root,
            config_dir=config_dir,
            runtime_config=config_dir / "runtime.json",
            secrets_config=config_dir / "secrets.json",
            run_dir=run_dir,
            lock_file=run_dir / "lifecycle.lock",
            broker_state=run_dir / "broker.json",
            logs_dir=logs_dir,
            broker_log=logs_dir / "broker.log",
        )


@dataclass(frozen=True)
class RuntimeConfig:
    host: str
    port: int
    catalog: Path
    allowed_studios: tuple[str, ...]
    allowed_tools: tuple[str, ...]
    startup_timeout_seconds: float

    @property
    def base_url(self) -> str:
        rendered_host = f"[{self.host}]" if ":" in self.host else self.host
        return f"http://{rendered_host}:{self.port}"

    @classmethod
    def load(cls, paths: InstallPaths) -> "RuntimeConfig":
        payload = _read_json_object(paths.runtime_config, private=False)
        required = {
            "schema_version",
            "host",
            "port",
            "catalog",
            "allowed_studios",
            "allowed_tools",
            "startup_timeout_seconds",
        }
        _require_exact_keys(payload, required, "runtime.json")
        if payload["schema_version"] != CONFIG_SCHEMA_VERSION:
            raise LifecycleError("Unsupported runtime.json schema_version")
        host = payload["host"]
        if host not in {"127.0.0.1", "::1"}:
            raise LifecycleError("runtime.json host must be an explicit loopback address")
        port = payload["port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise LifecycleError("runtime.json port must be an integer from 1 to 65535")
        catalog_raw = payload["catalog"]
        if not isinstance(catalog_raw, str) or not catalog_raw:
            raise LifecycleError("runtime.json catalog must be an absolute path")
        catalog = Path(catalog_raw)
        if not catalog.is_absolute():
            raise LifecycleError("runtime.json catalog must be an absolute path")
        _require_contained(catalog, paths.config_dir, "runtime.json catalog")
        _validate_regular_file(catalog, private=False)
        allowed_studios = _validate_scope(payload["allowed_studios"], "allowed_studios")
        allowed_tools = _validate_scope(payload["allowed_tools"], "allowed_tools")
        startup_timeout = payload["startup_timeout_seconds"]
        if (
            isinstance(startup_timeout, bool)
            or not isinstance(startup_timeout, (int, float))
            or not 1.0 <= float(startup_timeout) <= 60.0
        ):
            raise LifecycleError(
                "runtime.json startup_timeout_seconds must be from 1 to 60"
            )
        return cls(
            host=host,
            port=port,
            catalog=catalog,
            allowed_studios=allowed_studios,
            allowed_tools=allowed_tools,
            startup_timeout_seconds=float(startup_timeout),
        )


@dataclass(frozen=True, repr=False)
class SecretsConfig:
    client_token: str
    studio_token: str

    def __repr__(self) -> str:
        return "SecretsConfig(client_token=<redacted>, studio_token=<redacted>)"

    @classmethod
    def load(cls, paths: InstallPaths) -> "SecretsConfig":
        payload = _read_json_object(paths.secrets_config, private=True)
        _require_exact_keys(
            payload,
            {"schema_version", "client_token", "studio_token"},
            "secrets.json",
        )
        if payload["schema_version"] != CONFIG_SCHEMA_VERSION:
            raise LifecycleError("Unsupported secrets.json schema_version")
        client_token = _validate_token(payload["client_token"], "client_token")
        studio_token = _validate_token(payload["studio_token"], "studio_token")
        if client_token == studio_token:
            raise LifecycleError("Studio and MCP client tokens must be different")
        return cls(client_token=client_token, studio_token=studio_token)


@dataclass(frozen=True)
class BrokerRecord:
    broker_instance_id: str
    pid: int
    started_at: float
    host: str
    port: int
    version: str
    catalog_sha256: str

    @classmethod
    def from_health(
        cls, health: Mapping[str, Any], config: RuntimeConfig
    ) -> "BrokerRecord":
        validated = _validate_health(health)
        return cls(
            broker_instance_id=validated["broker_instance_id"],
            pid=validated["pid"],
            started_at=validated["started_at"],
            host=config.host,
            port=config.port,
            version=validated["version"],
            catalog_sha256=validated["catalog_sha256"],
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": STATE_SCHEMA_VERSION,
            "broker_instance_id": self.broker_instance_id,
            "pid": self.pid,
            "started_at": self.started_at,
            "host": self.host,
            "port": self.port,
            "version": self.version,
            "catalog_sha256": self.catalog_sha256,
        }


class ManagedHubClient:
    """Recover broker availability without replaying uncertain operations."""

    def __init__(
        self,
        paths: InstallPaths,
        config: RuntimeConfig,
        secrets: SecretsConfig,
    ) -> None:
        self.paths = paths
        self.config = config
        self.secrets = secrets
        self._client = _hub_client(config, secrets, timeout_seconds=130.0)

    def _recover(self) -> None:
        ensure_broker(self.paths, self.config, self.secrets)
        self._client = _hub_client(
            self.config, self.secrets, timeout_seconds=130.0
        )

    def _safe_discovery(self, method_name: str) -> Any:
        try:
            return getattr(self._client, method_name)()
        except HubTransportError:
            self._recover()
            return getattr(self._client, method_name)()

    def _no_replay(self, method_name: str, *args: Any) -> Any:
        try:
            return getattr(self._client, method_name)(*args)
        except HubTransportError as exc:
            replacement_ready = False
            try:
                self._recover()
                replacement_ready = True
            except (LifecycleError, ProxyError, ValueError, OSError):
                pass
            raise HubTransportError(
                (
                    "The broker connection was lost. The operation was not replayed "
                    "because its dispatch outcome may be uncertain."
                ),
                details={"replacement_ready": replacement_ready},
            ) from exc

    def tools(self) -> Dict[str, Any]:
        return self._safe_discovery("tools")

    def list_studios(self) -> Dict[str, Any]:
        return self._safe_discovery("list_studios")

    def call(
        self,
        tool_name: str,
        arguments: Dict[str, Any],
        client_request_id: str,
    ) -> Any:
        return self._no_replay(
            "call", tool_name, arguments, client_request_id
        )

    def start_job(self, arguments: Dict[str, Any]) -> Any:
        return self._no_replay("start_job", arguments)

    def get_job(self, arguments: Dict[str, Any]) -> Any:
        return self._no_replay("get_job", arguments)

    def cancel_job(self, arguments: Dict[str, Any]) -> Any:
        return self._no_replay("cancel_job", arguments)


def _require_exact_keys(
    payload: Mapping[str, Any], required: set[str], label: str
) -> None:
    actual = set(payload)
    missing = required - actual
    extra = actual - required
    if missing:
        raise LifecycleError(
            f"{label} is missing fields: {','.join(sorted(missing))}"
        )
    if extra:
        raise LifecycleError(
            f"{label} has unexpected fields: {','.join(sorted(extra))}"
        )


def _validate_scope(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise LifecycleError(f"runtime.json {label} must be a non-empty array")
    result = []
    for item in value:
        if (
            not isinstance(item, str)
            or not item
            or item != item.strip()
            or any(ord(ch) < 32 for ch in item)
        ):
            raise LifecycleError(
                f"runtime.json {label} entries must be non-empty strings"
            )
        result.append(item)
    return tuple(result)


def _validate_token(value: Any, label: str) -> str:
    if (
        not isinstance(value, str)
        or not MIN_TOKEN_LENGTH <= len(value) <= MAX_TOKEN_LENGTH
        or any(not ch.isprintable() or ch.isspace() for ch in value)
    ):
        raise LifecycleError(
            f"secrets.json {label} must be a 32-512 character non-whitespace secret"
        )
    return value


def _require_contained(path: Path, directory: Path, label: str) -> None:
    try:
        resolved_directory = directory.resolve(strict=True)
        resolved_path = path.resolve(strict=True)
        resolved_path.relative_to(resolved_directory)
    except (FileNotFoundError, OSError, ValueError):
        raise LifecycleError(f"{label} must remain under {directory}")


def _validate_owner(file_stat: os.stat_result, label: str) -> None:
    getuid = getattr(os, "getuid", None)
    if getuid is not None and file_stat.st_uid != getuid():
        raise LifecycleError(f"{label} must be owned by the current user")


def _validate_regular_stat(
    file_stat: os.stat_result, label: str, private: bool
) -> None:
    if not stat.S_ISREG(file_stat.st_mode):
        raise LifecycleError(f"{label} must be a regular file")
    _validate_owner(file_stat, label)
    if private and stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise LifecycleError(f"{label} permissions must not allow group/other access")


def _validate_regular_file(path: Path, private: bool) -> None:
    try:
        file_stat = path.lstat()
    except FileNotFoundError:
        raise LifecycleError(f"Required file is missing: {path}")
    if stat.S_ISLNK(file_stat.st_mode):
        raise LifecycleError(f"Symlinks are not accepted for protected file: {path}")
    _validate_regular_stat(file_stat, str(path), private)


def _read_json_object(path: Path, private: bool) -> Dict[str, Any]:
    flags = os.O_RDONLY | CLOEXEC | NOFOLLOW
    try:
        descriptor = os.open(path, flags)
    except FileNotFoundError:
        raise LifecycleError(f"Required file is missing: {path}")
    except OSError as exc:
        raise LifecycleError(f"Could not securely open {path}: {exc.strerror}")
    try:
        file_stat = os.fstat(descriptor)
        _validate_regular_stat(file_stat, str(path), private)
        chunks = []
        remaining = MAX_JSON_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > MAX_JSON_FILE_BYTES:
            raise LifecycleError(f"{path.name} exceeds the local size limit")
    finally:
        os.close(descriptor)
    try:
        payload = json.loads(encoded.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise LifecycleError(f"{path.name} must contain valid UTF-8 JSON")
    if not isinstance(payload, dict):
        raise LifecycleError(f"{path.name} must contain a JSON object")
    return payload


def _ensure_private_directory(path: Path) -> None:
    try:
        path.mkdir(mode=PRIVATE_DIRECTORY_MODE, parents=True, exist_ok=True)
    except OSError as exc:
        raise LifecycleError(f"Could not create private directory {path}: {exc}")
    file_stat = path.lstat()
    if stat.S_ISLNK(file_stat.st_mode) or not stat.S_ISDIR(file_stat.st_mode):
        raise LifecycleError(f"Protected path is not a real directory: {path}")
    _validate_owner(file_stat, str(path))
    if stat.S_IMODE(file_stat.st_mode) & 0o077:
        raise LifecycleError(
            f"Protected directory permissions must be 0700 or stricter: {path}"
        )


def load_install_config(
    paths: InstallPaths,
) -> tuple[RuntimeConfig, SecretsConfig]:
    _ensure_private_directory(paths.root)
    _ensure_private_directory(paths.config_dir)
    _ensure_private_directory(paths.run_dir)
    _ensure_private_directory(paths.logs_dir)
    return RuntimeConfig.load(paths), SecretsConfig.load(paths)


@contextlib.contextmanager
def lifecycle_lock(paths: InstallPaths) -> Iterator[None]:
    _ensure_private_directory(paths.run_dir)
    flags = os.O_RDWR | os.O_CREAT | CLOEXEC | NOFOLLOW
    try:
        descriptor = os.open(paths.lock_file, flags, PRIVATE_FILE_MODE)
    except OSError as exc:
        raise LifecycleError(f"Could not open lifecycle lock: {exc.strerror}")
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        _validate_regular_stat(
            os.fstat(descriptor), str(paths.lock_file), private=True
        )
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _write_json_atomic(path: Path, payload: Mapping[str, Any]) -> None:
    encoded = (
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    ).encode("utf-8")
    temporary = path.with_name(
        f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    descriptor: Optional[int] = None
    try:
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | CLOEXEC | NOFOLLOW,
            PRIVATE_FILE_MODE,
        )
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        view = memoryview(encoded)
        while view:
            written = os.write(descriptor, view)
            view = view[written:]
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = None
        os.replace(temporary, path)
    finally:
        if descriptor is not None:
            os.close(descriptor)
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _read_broker_record(paths: InstallPaths) -> Optional[BrokerRecord]:
    if not paths.broker_state.exists():
        return None
    try:
        payload = _read_json_object(paths.broker_state, private=True)
        _require_exact_keys(
            payload,
            {
                "schema_version",
                "broker_instance_id",
                "pid",
                "started_at",
                "host",
                "port",
                "version",
                "catalog_sha256",
            },
            "broker.json",
        )
        if payload["schema_version"] != STATE_SCHEMA_VERSION:
            return None
        _validate_uuid(payload["broker_instance_id"], "broker_instance_id")
        health = _validate_health(
            {
                "api_version": 2,
                "service": "studio-mcp-v2",
                "version": payload["version"],
                "broker_instance_id": payload["broker_instance_id"],
                "pid": payload["pid"],
                "started_at": payload["started_at"],
                "catalog_sha256": payload["catalog_sha256"],
            }
        )
        host = payload["host"]
        port = payload["port"]
        if host not in {"127.0.0.1", "::1"}:
            return None
        if isinstance(port, bool) or not isinstance(port, int):
            return None
        return BrokerRecord(
            broker_instance_id=health["broker_instance_id"],
            pid=health["pid"],
            started_at=health["started_at"],
            host=host,
            port=port,
            version=health["version"],
            catalog_sha256=health["catalog_sha256"],
        )
    except LifecycleError:
        return None


def _remove_record_if_instance(paths: InstallPaths, instance_id: str) -> None:
    record = _read_broker_record(paths)
    if record is None or record.broker_instance_id != instance_id:
        return
    try:
        paths.broker_state.unlink()
    except FileNotFoundError:
        pass


def _validate_uuid(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise LifecycleError(f"{label} must be a canonical UUID")
    try:
        parsed = uuid.UUID(value)
    except ValueError:
        raise LifecycleError(f"{label} must be a canonical UUID")
    if str(parsed) != value:
        raise LifecycleError(f"{label} must be a canonical UUID")
    return value


def _validate_health(payload: Mapping[str, Any]) -> Dict[str, Any]:
    if not isinstance(payload, Mapping):
        raise LifecycleError("Broker returned malformed lifecycle status")
    if payload.get("api_version") != 2 or payload.get("service") != "studio-mcp-v2":
        raise LifecycleError("Loopback service is not the expected v2 broker")
    instance_id = _validate_uuid(
        payload.get("broker_instance_id"), "broker_instance_id"
    )
    pid = payload.get("pid")
    started_at = payload.get("started_at")
    version = payload.get("version")
    digest = payload.get("catalog_sha256")
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        raise LifecycleError("Broker returned an invalid pid")
    if (
        isinstance(started_at, bool)
        or not isinstance(started_at, (int, float))
        or started_at <= 0
    ):
        raise LifecycleError("Broker returned an invalid start time")
    if not isinstance(version, str) or not version:
        raise LifecycleError("Broker returned an invalid version")
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(ch not in "0123456789abcdef" for ch in digest)
    ):
        raise LifecycleError("Broker returned an invalid catalog checksum")
    result = {
        "api_version": 2,
        "service": "studio-mcp-v2",
        "broker_instance_id": instance_id,
        "pid": pid,
        "started_at": float(started_at),
        "version": version,
        "catalog_sha256": digest,
    }
    for integer_field in (
        "session_count",
        "connected_session_count",
        "unsafe_transition_count",
        "active_client_operation_count",
        "active_studio_mutation_count",
        "stop_blocker_count",
    ):
        value = payload.get(integer_field)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            result[integer_field] = value
    if isinstance(payload.get("stop_safe"), bool):
        result["stop_safe"] = payload["stop_safe"]
    if isinstance(payload.get("lifecycle_stopping"), bool):
        result["lifecycle_stopping"] = payload["lifecycle_stopping"]
    for boolean_field in (
        "sessions_truncated",
        "unsafe_transitions_truncated",
        "stop_blockers_truncated",
    ):
        if isinstance(payload.get(boolean_field), bool):
            result[boolean_field] = payload[boolean_field]
    for list_field in (
        "sessions",
        "stop_blockers",
        "unsafe_transitions",
        "lifecycle_blockers",
    ):
        value = payload.get(list_field)
        if isinstance(value, list):
            result[list_field] = value
    return result


def _catalog_digest(catalog: ToolCatalog) -> str:
    import hashlib

    encoded = json.dumps(
        catalog.tools_for_mcp(),
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _hub_client(
    config: RuntimeConfig,
    secrets: SecretsConfig,
    *,
    timeout_seconds: float,
) -> HubClient:
    return HubClient(
        config.base_url,
        secrets.client_token,
        timeout_seconds=timeout_seconds,
    )


def _probe_broker(
    config: RuntimeConfig, secrets: SecretsConfig
) -> Optional[Dict[str, Any]]:
    try:
        result = _hub_client(
            config, secrets, timeout_seconds=HEALTH_TIMEOUT_SECONDS
        ).lifecycle_status()
        return _validate_health(result)
    except (ProxyError, LifecycleError, ValueError, OSError):
        return None


def _configured_port_open(config: RuntimeConfig) -> bool:
    # Probe address ownership with the same reuse policy as the managed HTTP
    # server. A connect-based probe is not reliable here: an unresponsive
    # listener with a full accept backlog can reject a later connection even
    # though it still owns the address. Binding never sends data to the
    # listener and reliably distinguishes an address the broker can claim.
    family = socket.AF_INET6 if ":" in config.host else socket.AF_INET
    address: Any = (
        (config.host, config.port, 0, 0)
        if family == socket.AF_INET6
        else (config.host, config.port)
    )
    try:
        with socket.socket(family, socket.SOCK_STREAM) as probe:
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            probe.bind(address)
        return False
    except OSError:
        # Fail closed for address-in-use, permission, or platform-family
        # errors. None of those conditions permits a safe managed start.
        return True


def _health_matches_install(
    health: Mapping[str, Any], expected_catalog_sha256: str
) -> bool:
    return (
        health.get("version") == __version__
        and health.get("catalog_sha256") == expected_catalog_sha256
    )


def _wait_until_stopped(
    config: RuntimeConfig,
    secrets: SecretsConfig,
    instance_id: str,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        # Observe the listener before attempting authenticated HTTP. An
        # unresponsive listener can fill a small accept backlog with the HTTP
        # probe itself, making a subsequent connect incorrectly look closed.
        if not _configured_port_open(config):
            return True
        health = _probe_broker(config, secrets)
        if (
            health is not None
            and health["broker_instance_id"] != instance_id
        ):
            # A different authenticated instance already owns the address.
            # The requested instance may be gone, but the managed endpoint is
            # not stopped and must never be reported as such.
            return False
        time.sleep(0.05)
    return False


def _request_authenticated_stop(
    config: RuntimeConfig,
    secrets: SecretsConfig,
    health: Mapping[str, Any],
) -> None:
    instance_id = _validate_uuid(
        health.get("broker_instance_id"), "broker_instance_id"
    )
    result = _hub_client(
        config, secrets, timeout_seconds=HEALTH_TIMEOUT_SECONDS
    ).lifecycle_stop(instance_id)
    if (
        not isinstance(result, dict)
        or result.get("stopping") is not True
        or result.get("broker_instance_id") != instance_id
    ):
        raise LifecycleError("Broker did not acknowledge the exact lifecycle stop")
    if not _wait_until_stopped(
        config, secrets, instance_id, STOP_TIMEOUT_SECONDS
    ):
        raise LifecycleError(
            "Broker acknowledged stop but did not exit before the safety timeout"
        )


def _open_private_log(path: Path) -> TextIO:
    flags = os.O_WRONLY | os.O_APPEND | os.O_CREAT | CLOEXEC | NOFOLLOW
    try:
        descriptor = os.open(path, flags, PRIVATE_FILE_MODE)
    except OSError as exc:
        raise LifecycleError(f"Could not open broker log: {exc.strerror}")
    try:
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        _validate_regular_stat(os.fstat(descriptor), str(path), private=True)
        return os.fdopen(descriptor, "a", encoding="utf-8", buffering=1)
    except Exception:
        os.close(descriptor)
        raise


def _broker_environment() -> Dict[str, str]:
    # The detached broker needs no caller environment. A fixed utility PATH is
    # retained for predictable process diagnostics; Python itself is invoked by
    # absolute path and isolated mode below.
    return {"PATH": "/usr/bin:/bin"}


def _start_broker_process(
    paths: InstallPaths, instance_id: str
) -> subprocess.Popen[Any]:
    _ensure_private_directory(paths.logs_dir)
    log_handle = _open_private_log(paths.broker_log)
    package_root = str(Path(__file__).resolve().parent.parent)
    isolated_bootstrap = (
        "import sys;"
        "root=sys.argv[1];"
        "del sys.argv[1];"
        "sys.path.insert(0,root);"
        "from studio_mcp_v2.lifecycle import main;"
        "main(sys.argv[1:])"
    )
    command = [
        sys.executable,
        "-I",
        "-B",
        "-X",
        "utf8",
        "-c",
        isolated_bootstrap,
        package_root,
        "_broker",
        "--instance-id",
        instance_id,
    ]
    if paths.root != InstallPaths.default().root:
        command.extend(["--test-support-root", str(paths.root)])
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            close_fds=True,
            start_new_session=True,
            env=_broker_environment(),
        )
    except OSError as exc:
        raise LifecycleError(f"Could not start v2 broker: {exc.strerror}")
    finally:
        log_handle.close()
    return process


def _stop_spawned_process(process: subprocess.Popen[Any]) -> None:
    if process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=2)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=2)


def ensure_broker(
    paths: InstallPaths,
    config: Optional[RuntimeConfig] = None,
    secrets: Optional[SecretsConfig] = None,
) -> Dict[str, Any]:
    """Atomically reuse or start the authenticated per-user broker."""

    if config is None or secrets is None:
        config, secrets = load_install_config(paths)
    catalog = ToolCatalog.from_file(config.catalog)
    expected_digest = _catalog_digest(catalog)
    with lifecycle_lock(paths):
        port_open = _configured_port_open(config)
        health = _probe_broker(config, secrets)
        if health is not None and health.get("lifecycle_stopping") is True:
            stopping_id = health["broker_instance_id"]
            if not _wait_until_stopped(
                config, secrets, stopping_id, STOP_TIMEOUT_SECONDS
            ):
                raise LifecycleError(
                    "The prior v2 broker is fenced for shutdown but did not exit "
                    "before the safety timeout"
                )
            port_open = _configured_port_open(config)
            health = None
        if health is None and port_open:
            raise LifecycleError(
                "The configured loopback port is occupied by a service that "
                "did not pass authenticated v2 health"
            )
        if health is not None and _health_matches_install(health, expected_digest):
            _write_json_atomic(
                paths.broker_state,
                BrokerRecord.from_health(health, config).as_dict(),
            )
            return dict(health)
        if health is not None:
            try:
                _request_authenticated_stop(config, secrets, health)
            except (LifecycleError, ProxyError, ValueError, OSError) as exc:
                raise LifecycleError(
                    "An authenticated but incompatible v2 broker is already running; "
                    "it could not be stopped safely: " + str(exc)
                ) from exc

        instance_id = str(uuid.uuid4())
        process = _start_broker_process(paths, instance_id)
        deadline = time.monotonic() + config.startup_timeout_seconds
        try:
            while time.monotonic() < deadline:
                return_code = process.poll()
                if return_code is not None:
                    raise LifecycleError(
                        "The v2 broker exited during startup; inspect broker.log"
                    )
                health = _probe_broker(config, secrets)
                if (
                    health is not None
                    and health["broker_instance_id"] == instance_id
                    and _health_matches_install(health, expected_digest)
                ):
                    _write_json_atomic(
                        paths.broker_state,
                        BrokerRecord.from_health(health, config).as_dict(),
                    )
                    threading.Thread(
                        target=process.wait,
                        name="studio-mcp-v2-broker-reaper",
                        daemon=True,
                    ).start()
                    return dict(health)
                time.sleep(0.05)
            raise LifecycleError(
                "The v2 broker did not pass authenticated readiness before timeout; "
                "inspect broker.log"
            )
        except Exception:
            _stop_spawned_process(process)
            _remove_record_if_instance(paths, instance_id)
            raise


def stop_broker(
    paths: InstallPaths,
    config: Optional[RuntimeConfig] = None,
    secrets: Optional[SecretsConfig] = None,
) -> Dict[str, Any]:
    """Gracefully stop only the exact authenticated broker instance."""

    if config is None or secrets is None:
        config, secrets = load_install_config(paths)
    with lifecycle_lock(paths):
        port_open = _configured_port_open(config)
        health = _probe_broker(config, secrets)
        if health is None:
            if port_open:
                raise LifecycleError(
                    "The configured loopback port is occupied by a service that "
                    "cannot be authenticated; refusing to claim it is stopped"
                )
            return {"running": False, "stopped": False}
        instance_id = health["broker_instance_id"]
        _request_authenticated_stop(config, secrets, health)
        _remove_record_if_instance(paths, instance_id)
        return {
            "running": False,
            "stopped": True,
            "broker_instance_id": instance_id,
        }


def broker_status(
    paths: InstallPaths,
    config: RuntimeConfig,
    secrets: SecretsConfig,
) -> Dict[str, Any]:
    port_open = _configured_port_open(config)
    health = _probe_broker(config, secrets)
    record = _read_broker_record(paths)
    result: Dict[str, Any] = {
        "running": health is not None,
        "record_present": record is not None,
        "loopback_port_open": health is not None or port_open,
        "condition": (
            "unauthenticated_or_unexpected_listener"
            if health is None and port_open
            else "stopped"
            if health is None and record is None
            else "unreachable_with_state"
            if health is None
            else "shutting_down"
            if health.get("lifecycle_stopping") is True
            else "healthy_idle"
            if health.get("stop_safe") is True
            else "running_busy_or_unsafe"
        ),
    }
    if health is not None:
        result["broker"] = health
        result["record_matches"] = (
            record is not None
            and record.broker_instance_id == health["broker_instance_id"]
            and record.pid == health["pid"]
        )
    return result


def _catalog_diagnostics(config: RuntimeConfig) -> Dict[str, Any]:
    raw_catalog = _read_json_object(config.catalog, private=False)
    catalog = ToolCatalog.from_file(config.catalog)
    operational = catalog.tools_for_mcp() + [
        dict(tool) for tool in JOB_TOOLS
    ]
    missing_explicit_target = []
    forbidden_names = []
    for tool in operational:
        name = tool.get("name")
        if not isinstance(name, str):
            forbidden_names.append("<invalid>")
            continue
        lowered = name.lower()
        if (
            "set_active_studio" in lowered
            or "active_studio" in lowered
            or "default_studio" in lowered
        ):
            forbidden_names.append(name)
        schema = tool.get("inputSchema", {})
        required = schema.get("required", []) if isinstance(schema, dict) else []
        properties = schema.get("properties", {}) if isinstance(schema, dict) else {}
        if "studio_id" not in required or "studio_id" not in properties:
            missing_explicit_target.append(name)
    upstream_report: Optional[Dict[str, str]] = None
    upstream = raw_catalog.get("upstream")
    if isinstance(upstream, dict):
        version = upstream.get("version")
        source_sha256 = upstream.get("source_sha256")
        compatibility = upstream.get("compatibility")
        if (
            _safe_catalog_label(version)
            and isinstance(source_sha256, str)
            and len(source_sha256) == 64
            and all(ch in "0123456789abcdef" for ch in source_sha256)
            and _safe_catalog_label(compatibility)
        ):
            upstream_report = {
                "version": version,
                "source_sha256": source_sha256,
                "compatibility": compatibility,
            }
    catalog_format = raw_catalog.get("format")
    catalog_version = raw_catalog.get("catalog_version")
    return {
        "catalog_sha256": _catalog_digest(catalog),
        "format": (
            catalog_format
            if _safe_catalog_label(catalog_format)
            else None
        ),
        "catalog_version": (
            catalog_version
            if _safe_catalog_label(catalog_version)
            else None
        ),
        "upstream": upstream_report,
        "installed_v1_cache": audit_installed_v1_cache(
            baseline_path=(
                config.catalog.parent / "upstream-known-tool-catalog.json"
            ),
            compatibility_manifest_path=(
                config.catalog.parent / "upstream-compatibility-map.json"
            ),
            durable_catalog_path=config.catalog,
        ),
        "discovery_tool": DISCOVERY_TOOL["name"],
        "operational_tool_count": len(operational),
        "all_operations_require_studio_id": not missing_explicit_target,
        "missing_explicit_target": missing_explicit_target,
        "forbidden_active_or_default_tools": forbidden_names,
    }


def _safe_catalog_label(value: Any) -> bool:
    return (
        isinstance(value, str)
        and 0 < len(value) <= 160
        and all(ch.isprintable() and ch not in "\r\n" for ch in value)
    )


def diagnostics(
    paths: InstallPaths,
    config: RuntimeConfig,
    secrets: SecretsConfig,
) -> Dict[str, Any]:
    catalog_report = _catalog_diagnostics(config)
    status = broker_status(paths, config, secrets)
    checks = {
        "runtime_config_valid": True,
        "secrets_file_private": True,
        "loopback_only": config.host in {"127.0.0.1", "::1"},
        "broker_state_safe": status["condition"]
        in {"stopped", "healthy_idle"},
        "explicit_studio_targeting": (
            catalog_report["all_operations_require_studio_id"]
            and not catalog_report["forbidden_active_or_default_tools"]
        ),
    }
    return {
        "ok": all(checks.values()),
        "version": __version__,
        "support_root": str(paths.root),
        "runtime": {
            "host": config.host,
            "port": config.port,
            "catalog": str(config.catalog),
        },
        "paths": {
            "broker_log": str(paths.broker_log),
            "broker_state": str(paths.broker_state),
        },
        "observations": {
            "authenticated_health": status["running"],
        },
        "checks": checks,
        "catalog": catalog_report,
        "lifecycle": status,
    }


def _broker_main(paths: InstallPaths, instance_id: str) -> None:
    _validate_uuid(instance_id, "instance_id")
    config, secrets = load_install_config(paths)
    catalog = ToolCatalog.from_file(config.catalog)
    runtime_info = HubRuntimeInfo(
        instance_id=instance_id,
        pid=os.getpid(),
        started_at=time.time(),
        catalog_sha256=_catalog_digest(catalog),
        version=__version__,
    )
    security = HubSecurityConfig(
        studio_token=secrets.studio_token,
        client_token=secrets.client_token,
        client_principal=Principal.create(
            "local-codex-v2",
            config.allowed_studios,
            config.allowed_tools,
        ),
    )

    def ready_callback(server: Any) -> None:
        if server.server_address[1] != config.port:
            raise LifecycleError("Broker bound an unexpected loopback port")
        _write_json_atomic(
            paths.broker_state,
            BrokerRecord(
                broker_instance_id=instance_id,
                pid=os.getpid(),
                started_at=runtime_info.started_at,
                host=config.host,
                port=config.port,
                version=__version__,
                catalog_sha256=runtime_info.catalog_sha256,
            ).as_dict(),
        )

    try:
        asyncio.run(
            serve_hub(
                host=config.host,
                port=config.port,
                catalog=catalog,
                security=security,
                runtime_info=runtime_info,
                ready_callback=ready_callback,
                announce=True,
            )
        )
    finally:
        _remove_record_if_instance(paths, instance_id)


def _print_json(payload: Mapping[str, Any]) -> None:
    sys.stdout.write(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        + "\n"
    )
    sys.stdout.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="roblox-studio-mcp-v2",
        description="Durable side-by-side Roblox Studio MCP v2 lifecycle",
    )
    subparsers = parser.add_subparsers(dest="command")
    subparsers.add_parser("stdio", help="ensure the broker and serve MCP over stdio")
    start_parser = subparsers.add_parser(
        "start", help="ensure the authenticated broker is running"
    )
    start_parser.add_argument("--json", action="store_true")
    status_parser = subparsers.add_parser(
        "status", help="report sanitized installed broker status"
    )
    status_parser.add_argument("--json", action="store_true")
    diagnostics_parser = subparsers.add_parser(
        "diagnostics", help="run sanitized lifecycle and targeting checks"
    )
    diagnostics_parser.add_argument("--json", action="store_true")
    doctor_parser = subparsers.add_parser(
        "doctor", help="alias for sanitized lifecycle diagnostics"
    )
    doctor_parser.add_argument("--json", action="store_true")
    stop_parser = subparsers.add_parser(
        "stop", help="stop only the exact authenticated v2 broker"
    )
    stop_parser.add_argument("--json", action="store_true")
    broker_parser = subparsers.add_parser("_broker", help=argparse.SUPPRESS)
    broker_parser.add_argument("--instance-id", required=True)
    broker_parser.add_argument("--test-support-root", type=Path)
    return parser


def _main_with_paths(
    argv: Optional[Sequence[str]],
    pinned_paths: Optional[InstallPaths],
) -> None:
    arguments = list(sys.argv[1:] if argv is None else argv)
    if not arguments:
        arguments = ["stdio"]
    parser = build_parser()
    args = parser.parse_args(arguments)
    if pinned_paths is not None:
        if (
            args.command == "_broker"
            and args.test_support_root is not None
            and args.test_support_root != pinned_paths.root
        ):
            raise LifecycleError(
                "Broker support root does not match the pinned installation"
            )
        paths = pinned_paths
    else:
        paths = (
            InstallPaths.for_test(args.test_support_root)
            if args.command == "_broker" and args.test_support_root is not None
            else InstallPaths.default()
        )
    try:
        if args.command == "_broker":
            _broker_main(paths, args.instance_id)
            return
        config, secrets = load_install_config(paths)
        if args.command == "stdio":
            ensure_broker(paths, config, secrets)
            client = ManagedHubClient(paths, config, secrets)
            serve_stdio(client)  # type: ignore[arg-type]
            return
        if args.command == "start":
            health = ensure_broker(paths, config, secrets)
            _print_json(
                {
                    "ok": True,
                    "running": True,
                    "broker": health,
                }
            )
            return
        if args.command == "status":
            _print_json(
                {
                    "ok": True,
                    "version": __version__,
                    "support_root": str(paths.root),
                    "lifecycle": broker_status(paths, config, secrets),
                }
            )
            return
        if args.command in {"diagnostics", "doctor"}:
            _print_json(diagnostics(paths, config, secrets))
            return
        if args.command == "stop":
            _print_json({"ok": True, **stop_broker(paths, config, secrets)})
            return
        parser.error("a lifecycle command is required")
    except (LifecycleError, ProxyError, ValueError, OSError) as exc:
        message = str(exc)
        if args.command in {
            "start",
            "status",
            "diagnostics",
            "doctor",
            "stop",
        }:
            _print_json(
                {
                    "ok": False,
                    "error": {
                        "code": "lifecycle_error",
                        "message": message,
                    },
                }
            )
        else:
            sys.stderr.write("Roblox Studio MCP v2 refused to start: " + message + "\n")
            sys.stderr.flush()
        raise SystemExit(2)


def main_for_installed_support_root(
    support_root: Path,
    argv: Optional[Sequence[str]] = None,
) -> None:
    """Run through an exact support root embedded by the trusted launcher.

    This is intentionally an import-only entry point rather than a public CLI
    flag. The installed bootstrap supplies a fixed absolute path; caller
    environment variables and ambient HOME never participate in routing.
    """

    root = Path(support_root)
    if not root.is_absolute():
        raise LifecycleError("Pinned lifecycle support root must be absolute")
    try:
        resolved = root.resolve(strict=True)
    except OSError as exc:
        raise LifecycleError(
            "Pinned lifecycle support root does not exist"
        ) from exc
    if resolved != root:
        raise LifecycleError(
            "Pinned lifecycle support root must be its exact canonical path"
        )
    _main_with_paths(argv, InstallPaths._from_root(root))


def main(argv: Optional[Sequence[str]] = None) -> None:
    """Run the normal per-user entry point using only the passwd-resolved root."""

    _main_with_paths(argv, None)


if __name__ == "__main__":
    main()
