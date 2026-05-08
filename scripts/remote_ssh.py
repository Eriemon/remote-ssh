#!/usr/bin/env python3
"""Erie Remote SSH helper for Codex skills.

This tool reads a structured server list, validates local prerequisites,
generates SSH commands, executes explicit remote commands, and gathers a small
server inventory report.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import shlex
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from posixpath import basename as posix_basename
from posixpath import dirname as posix_dirname
from pathlib import PurePosixPath
from typing import Any, Iterable


DEFAULT_TIMEOUT = 20
DEFAULT_SOFTWARE_KEYS = ["python", "conda", "cuda", "nvidia_driver", "gcc", "gpp", "cmake", "vivado", "vitis"]
REDACTED = "<redacted>"
SKILL_DIR = Path(__file__).resolve().parents[1]
PROJECT_ROOT = SKILL_DIR.parent
DEFAULT_SETTINGS_PATH = SKILL_DIR / "config" / "defaults.json"
SAFE_SSH_OPTIONS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=yes",
    "-o",
    "UpdateHostKeys=no",
    "-o",
    "ConnectTimeout=10",
]
ACCEPT_NEW_HOST_KEY_OPTIONS = [
    "-o",
    "BatchMode=yes",
    "-o",
    "StrictHostKeyChecking=accept-new",
    "-o",
    "UpdateHostKeys=no",
    "-o",
    "ConnectTimeout=10",
]
class RemoteSshError(Exception):
    """Expected user-facing error."""


def load_json_file(path: Path, label: str) -> dict[str, Any]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise RemoteSshError(f"{label} not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise RemoteSshError(f"{label} is not valid JSON: {path}: {exc}") from exc
    if not isinstance(data, dict):
        raise RemoteSshError(f"{label} root must be a JSON object.")
    return data


def settings_context(settings_path: Path) -> dict[str, str]:
    settings_dir = settings_path.resolve().parent
    return {
        "project_root": str(PROJECT_ROOT),
        "skill_dir": str(SKILL_DIR),
        "settings_dir": str(settings_dir),
        "home": str(Path.home()),
    }


def expand_placeholders(value: str, context: dict[str, str], extra: dict[str, str] | None = None) -> str:
    replacements = dict(context)
    if extra:
        replacements.update(extra)

    def replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key.startswith("env:"):
            return os.environ.get(key[4:], "")
        return replacements.get(key, match.group(0))

    return re.sub(r"\$\{([^}]+)\}", replace, value)


def resolve_config_path(value: str | Path, settings_path: Path, context: dict[str, str]) -> Path:
    raw = expand_placeholders(str(value), context)
    expanded = Path(os.path.expandvars(os.path.expanduser(raw)))
    if not expanded.is_absolute():
        expanded = settings_path.resolve().parent / expanded
    return expanded.resolve()


def load_settings(path: Path | None = None) -> dict[str, Any]:
    settings_path = (path or DEFAULT_SETTINGS_PATH).resolve()
    settings = load_json_file(settings_path, "Settings")
    if settings.get("version") != 1:
        raise RemoteSshError(f"Unsupported settings version: {settings.get('version')!r}")
    settings["_settings_path"] = str(settings_path)
    settings["_context"] = settings_context(settings_path)
    return settings


def settings_path(settings: dict[str, Any]) -> Path:
    return Path(str(settings["_settings_path"]))


def settings_value(settings: dict[str, Any], *keys: str, default: Any = None) -> Any:
    current: Any = settings
    for key in keys:
        if not isinstance(current, dict) or key not in current:
            return default
        current = current[key]
    return current


def resolve_server_list_path(settings: dict[str, Any], config_override: Path | None = None) -> Path:
    if config_override is not None:
        return config_override.resolve()
    configured = settings_value(settings, "paths", "default_server_list")
    if not configured:
        raise RemoteSshError("No server list configured. Provide --config or set paths.default_server_list.")
    return resolve_config_path(str(configured), settings_path(settings), settings["_context"])


def ssh_client(settings: dict[str, Any]) -> str:
    return str(settings_value(settings, "tools", "ssh_client", default="ssh") or "ssh")


def scp_client(settings: dict[str, Any]) -> str:
    return str(settings_value(settings, "tools", "scp_client", default="scp") or "scp")


def ssh_options(settings: dict[str, Any], accept_new_host_key: bool = False) -> list[str]:
    key = "accept_new_host_key_options" if accept_new_host_key else "safe_options"
    configured = settings_value(settings, "ssh", key)
    if not configured:
        configured = ACCEPT_NEW_HOST_KEY_OPTIONS if accept_new_host_key else SAFE_SSH_OPTIONS
    if not isinstance(configured, list) or not all(isinstance(item, str) for item in configured):
        raise RemoteSshError(f"Settings field ssh.{key} must be a list of strings.")
    connect_timeout = str(settings_value(settings, "ssh", "connect_timeout", default=10))
    return [expand_placeholders(item, settings["_context"], {"connect_timeout": connect_timeout}) for item in configured]


def default_timeout(settings: dict[str, Any]) -> int:
    value = settings_value(settings, "ssh", "default_timeout", default=DEFAULT_TIMEOUT)
    if isinstance(value, bool):
        raise RemoteSshError("Settings field ssh.default_timeout must be a positive integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RemoteSshError("Settings field ssh.default_timeout must be a positive integer.") from exc
    if parsed < 1:
        raise RemoteSshError("Settings field ssh.default_timeout must be a positive integer.")
    return parsed


def default_workdir(settings: dict[str, Any]) -> str:
    value = settings_value(settings, "ssh", "default_workdir", default="~/workspace")
    if not isinstance(value, str) or not value.strip():
        raise RemoteSshError("Settings field ssh.default_workdir must be a non-empty string.")
    return value.strip()


def software_catalog(settings: dict[str, Any]) -> list[dict[str, Any]]:
    catalog = settings_value(settings, "inventory", "software_catalog", default=[])
    if not isinstance(catalog, list) or not catalog:
        raise RemoteSshError("Settings field inventory.software_catalog must be a non-empty list.")
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for index, item in enumerate(catalog):
        if not isinstance(item, dict):
            raise RemoteSshError(f"Settings field inventory.software_catalog[{index}] must be an object.")
        tool_id = str(item.get("id", "")).strip()
        if not tool_id:
            raise RemoteSshError(f"Settings field inventory.software_catalog[{index}].id is required.")
        if not re.fullmatch(r"[A-Za-z0-9_.-]+", tool_id):
            raise RemoteSshError(f"Software catalog id contains unsupported characters: {tool_id!r}")
        folded = tool_id.casefold()
        if folded in seen:
            raise RemoteSshError(f"Duplicate software catalog id: {tool_id}")
        seen.add(folded)

        commands = item.get("commands", [])
        if commands is None:
            commands = []
        if not isinstance(commands, list) or not all(isinstance(command, str) and command.strip() for command in commands):
            raise RemoteSshError(f"Software catalog {tool_id} commands must be a list of non-empty strings.")
        scans = item.get("directory_scans", [])
        if scans is None:
            scans = []
        if not isinstance(scans, list):
            raise RemoteSshError(f"Software catalog {tool_id} directory_scans must be a list.")
        for scan_index, scan in enumerate(scans):
            if not isinstance(scan, dict):
                raise RemoteSshError(f"Software catalog {tool_id} directory_scans[{scan_index}] must be an object.")
            base_dirs = scan.get("base_dirs", [])
            if not isinstance(base_dirs, list) or not all(isinstance(path, str) and path.startswith("/") for path in base_dirs):
                raise RemoteSshError(f"Software catalog {tool_id} directory_scans[{scan_index}].base_dirs must contain absolute POSIX paths.")
            for required in ["subdir", "executable"]:
                value = scan.get(required)
                if not isinstance(value, str) or not value.strip() or value.startswith("/") or ".." in PurePosixPath(value).parts:
                    raise RemoteSshError(f"Software catalog {tool_id} directory_scans[{scan_index}].{required} is invalid.")
        if not commands and not scans:
            raise RemoteSshError(f"Software catalog {tool_id} must define commands or directory_scans.")
        normalized.append(item)
    return normalized


def software_catalog_version(settings: dict[str, Any]) -> int:
    value = settings_value(settings, "inventory", "catalog_version", default=1)
    if isinstance(value, bool):
        raise RemoteSshError("Settings field inventory.catalog_version must be a positive integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RemoteSshError("Settings field inventory.catalog_version must be a positive integer.") from exc
    if parsed < 1:
        raise RemoteSshError("Settings field inventory.catalog_version must be a positive integer.")
    return parsed


def positive_int_setting(settings: dict[str, Any], section: str, key: str, default: int) -> int:
    value = settings_value(settings, section, key, default=default)
    if isinstance(value, bool):
        raise RemoteSshError(f"Settings field {section}.{key} must be a positive integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RemoteSshError(f"Settings field {section}.{key} must be a positive integer.") from exc
    if parsed < 1:
        raise RemoteSshError(f"Settings field {section}.{key} must be a positive integer.")
    return parsed


def resolve_settings_path(settings: dict[str, Any], section: str, key: str, default: str) -> Path:
    configured = settings_value(settings, section, key, default=default)
    if not configured:
        raise RemoteSshError(f"Settings field {section}.{key} is required.")
    return resolve_config_path(str(configured), settings_path(settings), settings["_context"])


@dataclass(frozen=True)
class Server:
    raw: dict[str, Any]

    @property
    def id(self) -> str:
        return str(self.raw.get("id", ""))

    @property
    def legacy_server_id(self) -> str:
        return str(self.raw.get("legacy_server_id", ""))

    @property
    def name(self) -> str:
        return str(self.raw.get("name", ""))

    @property
    def host(self) -> str:
        return str(self.raw.get("host", ""))

    @property
    def port(self) -> int:
        value = self.raw.get("port")
        if isinstance(value, bool) or not isinstance(value, int):
            raise RemoteSshError(f"Server {self.label} has invalid port: {value!r}")
        if value < 1 or value > 65535:
            raise RemoteSshError(f"Server {self.label} has port outside 1..65535: {value}")
        return value

    @property
    def username(self) -> str:
        return str(self.raw.get("username", ""))

    @property
    def key_name(self) -> str:
        return str(self.raw.get("key_name", ""))

    @property
    def workdir(self) -> str:
        return str(self.raw.get("workdir", ""))

    @property
    def enabled(self) -> bool:
        value = self.raw.get("enabled")
        if not isinstance(value, bool):
            raise RemoteSshError(f"Server {self.label} has invalid enabled value: {value!r}")
        return value

    @property
    def validation_status(self) -> str:
        validation = self.raw.get("validation") or {}
        if not isinstance(validation, dict):
            return "unknown"
        return str(validation.get("status", "unknown"))

    @property
    def workspace_status(self) -> str:
        workspace = self.raw.get("workspace_check") or {}
        if not isinstance(workspace, dict):
            return "unknown"
        return str(workspace.get("status", "unknown"))

    @property
    def validation_error(self) -> str:
        validation = self.raw.get("validation") or {}
        if not isinstance(validation, dict):
            return ""
        value = validation.get("last_error")
        return "" if value is None else str(value)

    @property
    def label(self) -> str:
        return self.id or self.name or "<unnamed>"


def load_config(path: Path) -> dict[str, Any]:
    data = load_json_file(path, "Config")
    if data.get("version") != 1:
        raise RemoteSshError(f"Unsupported config version: {data.get('version')!r}")
    servers = data.get("servers")
    if not isinstance(servers, list):
        raise RemoteSshError("Config field 'servers' must be a list.")
    seen_ids: dict[str, int] = {}
    seen_names: dict[str, int] = {}
    for index, server in enumerate(servers):
        if not isinstance(server, dict):
            raise RemoteSshError(f"Config field 'servers[{index}]' must be an object.")
        server_id = str(server.get("id", "")).strip().casefold()
        server_name = str(server.get("name", "")).strip().casefold()
        if server_id:
            if server_id in seen_ids:
                raise RemoteSshError(f"Duplicate server id in config: servers[{seen_ids[server_id]}] and servers[{index}].")
            seen_ids[server_id] = index
        if server_name:
            if server_name in seen_names:
                raise RemoteSshError(f"Duplicate server name in config: servers[{seen_names[server_name]}] and servers[{index}].")
            seen_names[server_name] = index
    return data


def empty_config() -> dict[str, Any]:
    return {"version": 1, "default_key_dir": "~/.ssh", "servers": []}


def write_json_atomic(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def backup_file(path: Path) -> Path | None:
    if not path.exists():
        return None
    timestamp = dt.datetime.now(dt.UTC).strftime("%Y%m%d%H%M%S")
    backup = path.with_name(f"{path.name}.bak.{timestamp}")
    shutil.copy2(path, backup)
    return backup


def load_config_for_args(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], Path]:
    settings = load_settings(args.settings)
    config_path = resolve_server_list_path(settings, args.config)
    return load_config(config_path), settings, config_path


def get_servers(config: dict[str, Any]) -> list[Server]:
    return [Server(server) for server in config.get("servers", [])]


def select_server(config: dict[str, Any], selector: str, allow_disabled: bool = False) -> Server:
    matches = []
    selector_folded = selector.casefold()
    for server in get_servers(config):
        candidates = {
            server.id.casefold(),
            server.name.casefold(),
            server.legacy_server_id.casefold(),
        }
        if selector_folded in candidates:
            matches.append(server)

    if not matches:
        raise RemoteSshError(f"No server matched selector: {selector}")
    if len(matches) > 1:
        labels = ", ".join(server.label for server in matches)
        raise RemoteSshError(f"Selector matched multiple servers: {labels}")

    server = matches[0]
    try:
        enabled = server.enabled
    except RemoteSshError as exc:
        raise RemoteSshError(str(exc)) from exc
    if not enabled and not allow_disabled:
        raise RemoteSshError(f"Server {server.label} is disabled. Re-run with --allow-disabled to override.")
    return server


def expand_key_path(config: dict[str, Any], server: Server) -> Path:
    key_name = server.key_name
    if not key_name:
        raise RemoteSshError(f"Server {server.label} is missing key_name.")

    key_path = Path(os.path.expandvars(os.path.expanduser(key_name)))
    if not key_path.is_absolute():
        default_key_dir = str(config.get("default_key_dir") or "~/.ssh")
        base = Path(os.path.expandvars(os.path.expanduser(default_key_dir)))
        key_path = base / key_name
    return key_path


def expand_public_key_path(config: dict[str, Any], server: Server) -> Path:
    key_path = expand_key_path(config, server)
    return Path(str(key_path) + ".pub")


def quote_remote_path(path: str) -> str:
    if path == "~":
        return "~"
    if path.startswith("~/"):
        suffix = path[2:]
        if not suffix:
            return "~/"
        return "~/" + shlex.quote(suffix)
    return shlex.quote(path)


def validate_server(config: dict[str, Any], server: Server) -> list[str]:
    return validate_server_shape(config, server, require_key_file=True)


def validate_server_shape(config: dict[str, Any], server: Server, require_key_file: bool = False) -> list[str]:
    errors: list[str] = []
    required_fields = ["id", "name", "type", "host", "port", "username", "key_name", "workdir", "enabled"]

    for field in required_fields:
        if field not in server.raw:
            errors.append(f"missing field: {field}")

    if server.raw.get("type") != "ssh":
        errors.append(f"unsupported type: {server.raw.get('type')!r}")

    if not isinstance(server.raw.get("enabled"), bool):
        errors.append(f"invalid enabled value: {server.raw.get('enabled')!r}")

    for field in ["id", "name", "host", "username", "key_name", "workdir"]:
        if not str(server.raw.get(field, "")).strip():
            errors.append(f"empty field: {field}")

    try:
        _ = server.port
    except RemoteSshError as exc:
        errors.append(str(exc))

    try:
        key_path = expand_key_path(config, server)
        if require_key_file and not key_path.exists():
            errors.append("key file not found")
    except RemoteSshError as exc:
        errors.append(str(exc))

    return errors


def redact(value: str) -> str:
    if not value:
        return ""
    return REDACTED


def redact_text(config: dict[str, Any], value: str) -> str:
    redacted = value
    sensitive_values: set[str] = set()
    for server in get_servers(config):
        for field in ["host", "username", "key_name"]:
            item = str(server.raw.get(field, ""))
            if item:
                sensitive_values.add(item)
        port = server.raw.get("port")
        if isinstance(port, int):
            sensitive_values.add(str(port))
        try:
            sensitive_values.add(str(expand_key_path(config, server)))
        except RemoteSshError:
            pass
    for item in sorted(sensitive_values, key=len, reverse=True):
        redacted = redacted.replace(item, REDACTED)
    return redacted


def user_host(server: Server) -> str:
    if not server.username or not server.host:
        raise RemoteSshError(f"Server {server.label} is missing username or host.")
    return f"{server.username}@{server.host}"


def build_ssh_args(
    config: dict[str, Any],
    settings: dict[str, Any],
    server: Server,
    remote_command: str | None = None,
    accept_new_host_key: bool = False,
) -> list[str]:
    key_path = expand_key_path(config, server)
    args = [
        ssh_client(settings),
        "-i",
        str(key_path),
        "-p",
        str(server.port),
        *ssh_options(settings, accept_new_host_key),
        user_host(server),
    ]
    if server.workdir and remote_command:
        remote_command = f"cd {quote_remote_path(server.workdir)} && {remote_command}"
    if remote_command:
        args.append(remote_command)
    return args


def display_command(args: Iterable[str]) -> str:
    if os.name == "nt":
        return subprocess.list2cmdline(list(args))
    return " ".join(shlex.quote(arg) for arg in args)


def redact_command(config: dict[str, Any], command: str) -> str:
    return redact_text(config, command)


def remote_command_from_tokens(tokens: list[str]) -> str:
    if tokens and tokens[0] == "--":
        tokens = tokens[1:]
    if not tokens:
        raise RemoteSshError("Remote command is required after --.")
    if len(tokens) == 1:
        return tokens[0]
    return " ".join(shlex.quote(token) for token in tokens)


def run_ssh(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    if timeout < 1:
        raise RemoteSshError("Timeout must be a positive integer.")
    try:
        return subprocess.run(
            args,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        client = args[0] if args else "ssh"
        raise RemoteSshError(f"OpenSSH client '{client}' was not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RemoteSshError(f"SSH command timed out after {timeout} seconds.") from exc


def run_scp(args: list[str], timeout: int) -> subprocess.CompletedProcess[str]:
    if timeout < 1:
        raise RemoteSshError("Timeout must be a positive integer.")
    try:
        return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=False)
    except FileNotFoundError as exc:
        client = args[0] if args else "scp"
        raise RemoteSshError(f"OpenSSH scp client '{client}' was not found on PATH.") from exc
    except subprocess.TimeoutExpired as exc:
        raise RemoteSshError(f"SCP command timed out after {timeout} seconds.") from exc


def summarize_error(stderr: str) -> str:
    lines = [line.strip() for line in stderr.splitlines() if line.strip()]
    if not lines:
        return ""
    return "\n".join(lines[-6:])


REMOTE_RESOLVE_SCRIPT = r"""
import json
import os
import sys

rel = sys.argv[1]
allow_missing = sys.argv[2] == "1"
parent_must_exist = sys.argv[3] == "1"
base = os.path.realpath(os.getcwd())
target = os.path.realpath(os.path.join(base, rel))

def inside(child, root):
    return child == root or child.startswith(root + os.sep)

if not inside(target, base):
    print("target escapes workdir", file=sys.stderr)
    sys.exit(31)
exists = os.path.exists(target)
if not exists and not allow_missing:
    print("target does not exist", file=sys.stderr)
    sys.exit(32)
if parent_must_exist:
    parent = os.path.realpath(os.path.dirname(target))
    if not inside(parent, base):
        print("parent escapes workdir", file=sys.stderr)
        sys.exit(33)
    if not os.path.isdir(parent):
        print("target parent does not exist", file=sys.stderr)
        sys.exit(34)
info = {
    "workdir": base,
    "target": target,
    "exists": exists,
    "is_dir": os.path.isdir(target) if exists else False,
    "is_file": os.path.isfile(target) if exists else False,
    "size": os.path.getsize(target) if exists and os.path.isfile(target) else None,
}
print(json.dumps(info, sort_keys=True))
"""

REMOTE_LIST_SCRIPT = r"""
import json
import os
import sys

rel = sys.argv[1]
base = os.path.realpath(os.getcwd())
target = os.path.realpath(os.path.join(base, rel))

def inside(child, root):
    return child == root or child.startswith(root + os.sep)

if not inside(target, base):
    print("target escapes workdir", file=sys.stderr)
    sys.exit(31)
if not os.path.exists(target):
    print("target does not exist", file=sys.stderr)
    sys.exit(32)
if os.path.isdir(target):
    entries = []
    for name in sorted(os.listdir(target)):
        path = os.path.join(target, name)
        entries.append({
            "name": name,
            "type": "dir" if os.path.isdir(path) else "file",
            "size": os.path.getsize(path) if os.path.isfile(path) else None,
        })
    print(json.dumps({"path": rel, "type": "dir", "entries": entries}, sort_keys=True))
else:
    print(json.dumps({"path": rel, "type": "file", "size": os.path.getsize(target)}, sort_keys=True))
"""

REMOTE_STAT_SCRIPT = r"""
import json
import os
import stat
import sys

rel = sys.argv[1]
base = os.path.realpath(os.getcwd())
target = os.path.realpath(os.path.join(base, rel))

def inside(child, root):
    return child == root or child.startswith(root + os.sep)

if not inside(target, base):
    print("target escapes workdir", file=sys.stderr)
    sys.exit(31)
try:
    st = os.stat(target)
except FileNotFoundError:
    print("target does not exist", file=sys.stderr)
    sys.exit(32)
mode = st.st_mode
kind = "dir" if stat.S_ISDIR(mode) else "file" if stat.S_ISREG(mode) else "other"
print(json.dumps({
    "path": rel,
    "type": kind,
    "size": st.st_size,
    "mode": oct(stat.S_IMODE(mode)),
    "mtime": int(st.st_mtime),
}, sort_keys=True))
"""


def remote_python_command(script: str, args: list[str]) -> str:
    quoted_args = " ".join(shlex.quote(arg) for arg in args)
    return (
        "if command -v python3 >/dev/null 2>&1; then "
        f"python3 -c {shlex.quote(script)} {quoted_args}; "
        "elif command -v python >/dev/null 2>&1; then "
        f"python -c {shlex.quote(script)} {quoted_args}; "
        "else echo 'python not found on remote host' >&2; exit 127; fi"
    )


def remote_relative_path(value: str, allow_dot: bool = True) -> str:
    raw = str(value).strip()
    if not raw:
        raise RemoteSshError("Remote path must not be empty.")
    if "\\" in raw:
        raise RemoteSshError("Remote path must use POSIX separators and stay relative to workdir.")
    if raw.startswith("/") or raw.startswith("~") or re.match(r"^[A-Za-z]:", raw):
        raise RemoteSshError("Remote path must be relative to workdir.")
    path = PurePosixPath(raw)
    if path.is_absolute():
        raise RemoteSshError("Remote path must be relative to workdir.")
    if any(part in {"", ".."} for part in path.parts):
        raise RemoteSshError("Remote path must not contain empty or parent traversal segments.")
    normalized = path.as_posix()
    if normalized == "." and not allow_dot:
        raise RemoteSshError("Remote path must identify a child path, not the workdir root.")
    return normalized


def resolve_remote_path(
    config: dict[str, Any],
    settings: dict[str, Any],
    server: Server,
    relative_path: str,
    allow_missing: bool = False,
    parent_must_exist: bool = False,
) -> dict[str, Any]:
    command = remote_python_command(
        REMOTE_RESOLVE_SCRIPT,
        [relative_path, "1" if allow_missing else "0", "1" if parent_must_exist else "0"],
    )
    result = run_ssh(build_ssh_args(config, settings, server, command), default_timeout(settings))
    if result.returncode != 0:
        summary = summarize_error(result.stderr)
        raise RemoteSshError(summary or "Remote path resolution failed.")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RemoteSshError("Remote path resolution returned invalid JSON.") from exc
    if not isinstance(data, dict) or not isinstance(data.get("target"), str):
        raise RemoteSshError("Remote path resolution returned an invalid shape.")
    return data


def remote_json_operation(
    config: dict[str, Any],
    settings: dict[str, Any],
    server: Server,
    script: str,
    relative_path: str,
) -> dict[str, Any]:
    result = run_ssh(build_ssh_args(config, settings, server, remote_python_command(script, [relative_path])), default_timeout(settings))
    if result.returncode != 0:
        summary = summarize_error(result.stderr)
        raise RemoteSshError(summary or "Remote operation failed.")
    try:
        data = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RemoteSshError("Remote operation returned invalid JSON.") from exc
    if not isinstance(data, dict):
        raise RemoteSshError("Remote operation returned an invalid shape.")
    return data


def requests_dir(settings: dict[str, Any]) -> Path:
    return resolve_settings_path(settings, "paths", "requests_dir", "${project_root}/requests")


def downloads_dir(settings: dict[str, Any]) -> Path:
    return resolve_settings_path(settings, "paths", "downloads_dir", "${project_root}/downloads")


def relative_to_project(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT.resolve()).as_posix()
    except ValueError as exc:
        raise RemoteSshError("Local path must stay inside the current project root.") from exc


def resolve_local_project_path(value: str | Path, must_exist: bool = True) -> Path:
    raw = Path(os.path.expandvars(os.path.expanduser(str(value))))
    path = raw if raw.is_absolute() else PROJECT_ROOT / raw
    path = path.resolve()
    try:
        path.relative_to(PROJECT_ROOT.resolve())
    except ValueError as exc:
        raise RemoteSshError("Local path must stay inside the current project root.") from exc
    if must_exist and not path.exists():
        raise RemoteSshError(f"Local path not found: {path}")
    return path


def resolve_download_target(settings: dict[str, Any], value: str | Path) -> Path:
    base = downloads_dir(settings).resolve()
    raw = Path(os.path.expandvars(os.path.expanduser(str(value))))
    path = raw if raw.is_absolute() else base / raw
    path = path.resolve()
    try:
        path.relative_to(base)
    except ValueError as exc:
        raise RemoteSshError("Download target must stay inside paths.downloads_dir.") from exc
    return path


def request_id(operation: str) -> str:
    now = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{now}-{operation}-{uuid.uuid4().hex[:8]}"


def command_risks(command: str) -> list[str]:
    risks: list[str] = []
    checks = [
        (r"(^|\s)sudo(\s|$)", "uses sudo"),
        (r"(^|\s)rm(\s|$)", "contains rm"),
        (r"(^|\s)chmod(\s|$)", "contains chmod"),
        (r"(^|\s)chown(\s|$)", "contains chown"),
        (r"(^|\s)/[A-Za-z0-9_.\-/]+", "mentions absolute paths"),
        (r"[|;&]", "uses shell control operators"),
        (r">|<", "uses redirection"),
        (r"&\s*$", "may run in background"),
    ]
    for pattern, label in checks:
        if re.search(pattern, command):
            risks.append(label)
    return risks or ["no obvious high-risk token detected"]


def write_request(settings: dict[str, Any], request: dict[str, Any]) -> Path:
    directory = requests_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{request['request_id']}.json"
    write_json_atomic(path, request)
    return path


def create_request(settings: dict[str, Any], operation: str, server: Server, payload: dict[str, Any], reason: str = "") -> Path:
    request = {
        "version": 1,
        "request_id": request_id(operation),
        "operation": operation,
        "server": server.id,
        "reason": reason,
        "created_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "payload": payload,
    }
    if operation == "command":
        request["risk_summary"] = command_risks(str(payload.get("command", "")))
    else:
        request["risk_summary"] = ["modifies remote workdir content"]
    return write_request(settings, request)


def load_request(path: Path) -> dict[str, Any]:
    request = load_json_file(path.resolve(), "Request")
    if request.get("version") != 1:
        raise RemoteSshError(f"Unsupported request version: {request.get('version')!r}")
    if not isinstance(request.get("payload"), dict):
        raise RemoteSshError("Request payload must be an object.")
    if not request.get("operation") or not request.get("server"):
        raise RemoteSshError("Request must include operation and server.")
    return request


def print_request_created(path: Path, request: dict[str, Any]) -> None:
    print(f"request: {path}")
    print(f"operation: {request['operation']}")
    print(f"server: {request['server']}")
    for risk in request.get("risk_summary", []):
        print(f"risk: {risk}")


def build_scp_options(settings: dict[str, Any], accept_new_host_key: bool = False) -> list[str]:
    return ssh_options(settings, accept_new_host_key)


def build_scp_download_args(
    config: dict[str, Any],
    settings: dict[str, Any],
    server: Server,
    remote_target: str,
    local_target: Path,
    recursive: bool = False,
) -> list[str]:
    key_path = expand_key_path(config, server)
    args = [
        scp_client(settings),
        "-i",
        str(key_path),
        "-P",
        str(server.port),
        *build_scp_options(settings),
    ]
    if recursive:
        args.append("-r")
    args.extend([f"{user_host(server)}:{shlex.quote(remote_target)}", str(local_target)])
    return args


def build_scp_upload_args(
    config: dict[str, Any],
    settings: dict[str, Any],
    server: Server,
    local_source: Path,
    remote_target: str,
    recursive: bool = False,
) -> list[str]:
    key_path = expand_key_path(config, server)
    args = [
        scp_client(settings),
        "-i",
        str(key_path),
        "-P",
        str(server.port),
        *build_scp_options(settings),
    ]
    if recursive:
        args.append("-r")
    args.extend([str(local_source), f"{user_host(server)}:{shlex.quote(remote_target)}"])
    return args


def enabled_ssh_servers(config: dict[str, Any]) -> list[Server]:
    servers: list[Server] = []
    for server in get_servers(config):
        if server.raw.get("type") == "ssh" and server.enabled:
            servers.append(server)
    return servers


def server_category(server: Server) -> str:
    value = server.raw.get("category")
    if isinstance(value, str) and value.strip():
        return value.strip()
    text_parts = [server.name, " ".join(server_functions(server)), " ".join(software_summary(server))]
    notes = server.raw.get("notes")
    if isinstance(notes, str):
        text_parts.append(notes)
    snapshot = server.raw.get("inventory_snapshot")
    if isinstance(snapshot, dict):
        description = snapshot.get("description")
        if isinstance(description, str):
            text_parts.append(description)
    text = " ".join(text_parts).casefold()
    if any(token in text for token in ["fpga", "alveo", "vivado", "vitis", "xilinx"]):
        return "FPGA"
    if any(token in text for token in ["gpu", "cuda", "nvidia", "rtx", "nvcc"]):
        return "GPU"
    if any(token in text for token in ["cpu", "general", "universal"]):
        return "CPU"
    return "Uncategorized"


def software_summary(server: Server) -> list[str]:
    snapshot = server.raw.get("software_scan")
    if not isinstance(snapshot, dict):
        return []
    tools = snapshot.get("tools")
    if not isinstance(tools, dict):
        return []
    summary: list[str] = []
    for key in DEFAULT_SOFTWARE_KEYS:
        tool = tools.get(key)
        if isinstance(tool, dict) and tool.get("status") == "installed":
            summary.append(f"{key} installed")
    for key in sorted(tools):
        if key in DEFAULT_SOFTWARE_KEYS:
            continue
        tool = tools.get(key)
        if isinstance(tool, dict) and tool.get("status") == "installed":
            summary.append(f"{key} installed")
    return summary


def server_functions(server: Server) -> list[str]:
    value = server.raw.get("functions")
    if isinstance(value, list):
        functions = [str(item).strip() for item in value if isinstance(item, str) and item.strip()]
        if functions:
            return functions

    inferred: list[str] = []
    notes = server.raw.get("notes")
    if isinstance(notes, str) and notes.strip():
        inferred.append(notes.strip())
    snapshot = server.raw.get("inventory_snapshot")
    if isinstance(snapshot, dict):
        description = snapshot.get("description")
        if isinstance(description, str) and description.strip():
            inferred.append(description.strip())
    inferred.extend(software_summary(server))
    return list(dict.fromkeys(inferred))


def server_choice_record(config: dict[str, Any], server: Server, show_sensitive: bool = False) -> dict[str, Any]:
    name = server.name
    category = server_category(server)
    functions = server_functions(server)
    if not show_sensitive:
        name = redact_text(config, name)
        category = redact_text(config, category)
        functions = [redact_text(config, item) for item in functions]
    record: dict[str, Any] = {
        "id": server.id,
        "name": name,
        "category": category,
        "functions": functions,
        "enabled": server.enabled,
        "availability": "enabled" if server.enabled else "disabled",
        "validation_status": server.validation_status,
        "workspace_status": server.workspace_status,
        "software": software_summary(server),
    }
    if show_sensitive:
        record["target"] = user_host(server)
        record["key_path"] = str(expand_key_path(config, server))
    return record


def grouped_choice_records(records: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        category = str(record.get("category") or "Uncategorized")
        grouped.setdefault(category, []).append(record)
    return grouped


def discover_summary(settings: dict[str, Any], config_path: Path) -> tuple[dict[str, Any], int]:
    if not config_path.exists():
        return (
            {
                "status": "not_configured",
                "server_list_exists": False,
                "server_list_path": REDACTED,
                "server_count": 0,
                "enabled_ssh_count": 0,
                "next_steps": [
                    "Run scripts/bat/config/configure_remote_ssh.bat, scripts/shell/config/configure_remote_ssh.sh, or scripts/powershell/config/configure_remote_ssh.ps1",
                    "Or run remote_ssh.py init-config then remote_ssh.py add-server --interactive",
                ],
            },
            3,
        )

    config = load_config(config_path)
    servers = get_servers(config)
    enabled_servers = enabled_ssh_servers(config)
    if not enabled_servers:
        status = "no_enabled_ssh"
        exit_code = 4
        next_steps = ["Run remote_ssh.py add-server --interactive to add or enable an SSH server."]
    else:
        status = "available"
        exit_code = 0
        next_steps = ["Run list, check, command, exec, or inventory for a selected server."]

    return (
        {
            "status": status,
            "server_list_exists": True,
            "server_list_path": REDACTED,
            "server_count": len(servers),
            "enabled_ssh_count": len(enabled_servers),
            "enabled_ssh_servers": [{"id": server.id, "name": server.name} for server in enabled_servers],
            "next_steps": next_steps,
        },
        exit_code,
    )


def cmd_discover(args: argparse.Namespace) -> int:
    settings = load_settings(args.settings)
    config_path = resolve_server_list_path(settings, args.config)
    summary, exit_code = discover_summary(settings, config_path)
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return exit_code

    print(f"status: {summary['status']}")
    print(f"server_list_exists: {summary['server_list_exists']}")
    print(f"server_count: {summary['server_count']}")
    print(f"enabled_ssh_count: {summary['enabled_ssh_count']}")
    for server in summary.get("enabled_ssh_servers", []):
        print(f"- {server['id']}: {server['name']}")
    for step in summary["next_steps"]:
        print(f"next: {step}")
    return exit_code


def cmd_choices(args: argparse.Namespace) -> int:
    settings = load_settings(args.settings)
    config_path = resolve_server_list_path(settings, args.config)
    if not config_path.exists():
        summary, exit_code = discover_summary(settings, config_path)
        if args.json:
            print(json.dumps(summary, indent=2, ensure_ascii=False))
            return exit_code
        print(f"status: {summary['status']}")
        print(f"server_list_exists: {summary['server_list_exists']}")
        print(f"server_count: {summary['server_count']}")
        print(f"enabled_ssh_count: {summary['enabled_ssh_count']}")
        for step in summary["next_steps"]:
            print(f"next: {step}")
        return exit_code

    config = load_config(config_path)
    servers = get_servers(config)
    enabled_servers = enabled_ssh_servers(config)
    if args.all:
        selected = servers
    elif enabled_servers:
        selected = enabled_servers
    else:
        selected = servers

    status = "available" if enabled_servers else "no_enabled_ssh"
    exit_code = 0 if enabled_servers else 4
    records = [server_choice_record(config, server, args.show_sensitive) for server in selected]
    grouped = grouped_choice_records(records)
    summary = {
        "status": status,
        "server_list_exists": True,
        "server_list_path": str(config_path) if args.show_sensitive else REDACTED,
        "server_count": len(servers),
        "enabled_ssh_count": len(enabled_servers),
        "servers": records,
        "groups": grouped,
        "next_steps": (
            ["Reply with the server id or name to select a target before any remote access."]
            if enabled_servers
            else ["Enable an existing server or run remote_ssh.py add-server --interactive to add an SSH server."]
        ),
    }
    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
        return exit_code

    print(f"status: {status}")
    print("server_list_exists: True")
    print(f"server_count: {len(servers)}")
    print(f"enabled_ssh_count: {len(enabled_servers)}")
    if not records:
        print("No servers found.")
    for category, category_records in grouped.items():
        print(f"Category: {category}")
        for record in category_records:
            print(f"- id: {record['id']}")
            print(f"  name: {record['name']}")
            print(f"  enabled: {record['enabled']}")
            if record["enabled"]:
                print("  availability: enabled")
            else:
                print("  availability: disabled - requires explicit enablement before remote access")
            print(f"  validation_status: {record['validation_status']}")
            print(f"  workspace_status: {record['workspace_status']}")
            functions = record.get("functions") or []
            print(f"  functions: {'; '.join(functions) if functions else 'unspecified'}")
            software = record.get("software") or []
            print(f"  software: {'; '.join(software) if software else 'not scanned or not detected'}")
            if args.show_sensitive:
                print(f"  target: {record.get('target', '')}")
                print(f"  key_path: {record.get('key_path', '')}")
    for step in summary["next_steps"]:
        print(f"next: {step[:1].lower() + step[1:] if step else step}")
    return exit_code


def cmd_init_config(args: argparse.Namespace) -> int:
    settings = load_settings(args.settings)
    config_path = resolve_server_list_path(settings, args.config)
    if config_path.exists() and not args.force:
        raise RemoteSshError(f"Server list already exists: {config_path}. Use --force to overwrite.")
    backup = backup_file(config_path) if args.force else None
    write_json_atomic(config_path, empty_config())
    print(f"created: {config_path}")
    if backup:
        print(f"backup: {backup}")
    return 0


def next_server_id(config: dict[str, Any]) -> str:
    highest = 0
    for server in get_servers(config):
        match = re.fullmatch(r"server_(\d+)", server.id)
        if match:
            highest = max(highest, int(match.group(1)))
    return f"server_{highest + 1 if highest else len(get_servers(config)) + 1}"


def prompt_value(label: str, default: str = "", required: bool = False) -> str:
    suffix = f" [{default}]" if default else ""
    while True:
        value = input(f"{label}{suffix}: ").strip()
        if not value and default:
            value = default
        if value or not required:
            return value
        print(f"{label} is required.", file=sys.stderr)


def prompt_bool(label: str, default: bool = True) -> bool:
    suffix = "Y/n" if default else "y/N"
    while True:
        value = input(f"{label} [{suffix}]: ").strip().casefold()
        if not value:
            return default
        if value in {"y", "yes", "true", "1"}:
            return True
        if value in {"n", "no", "false", "0"}:
            return False
        print("Enter yes or no.", file=sys.stderr)


def parse_port_value(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise RemoteSshError("Port must be an integer.") from exc
    if port < 1 or port > 65535:
        raise RemoteSshError("Port must be in range 1..65535.")
    return port


def add_server_record(config: dict[str, Any], record: dict[str, Any]) -> None:
    candidate = Server(record)
    errors = validate_server_shape(config, candidate, require_key_file=False)
    if errors:
        raise RemoteSshError("Invalid server record: " + "; ".join(errors))
    selector_values = {candidate.id.casefold(), candidate.name.casefold()}
    for server in get_servers(config):
        existing = {server.id.casefold(), server.name.casefold(), server.legacy_server_id.casefold()}
        if selector_values & existing:
            raise RemoteSshError(f"Server id or name already exists: {candidate.label}")
    config.setdefault("servers", []).append(record)


def cmd_add_server(args: argparse.Namespace) -> int:
    if not args.interactive:
        raise RemoteSshError("add-server currently requires --interactive.")
    settings = load_settings(args.settings)
    config_path = resolve_server_list_path(settings, args.config)
    config = load_config(config_path) if config_path.exists() else empty_config()

    default_id = next_server_id(config)
    server_id = prompt_value("id", default_id, required=True)
    name = prompt_value("name", server_id, required=True)
    host = prompt_value("host", required=True)
    port = parse_port_value(prompt_value("port", "22", required=True))
    username = prompt_value("username", required=True)
    key_name = prompt_value("key_name", required=True)
    workdir = prompt_value("workdir", default_workdir(settings), required=True)
    enabled = prompt_bool("enabled", True)
    notes = prompt_value("notes", "")

    record = {
        "id": server_id,
        "name": name,
        "type": "ssh",
        "host": host,
        "port": port,
        "username": username,
        "key_name": key_name,
        "workdir": workdir,
        "enabled": enabled,
    }
    if notes:
        record["notes"] = notes

    add_server_record(config, record)
    backup = backup_file(config_path)
    write_json_atomic(config_path, config)
    load_config(config_path)

    refreshed = load_config(config_path)
    server = select_server(refreshed, server_id, allow_disabled=True)
    if not server.enabled:
        snapshot = {
            "status": "skipped",
            "scanned_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "catalog_version": software_catalog_version(settings),
            "tools": {},
            "fpga_devices": [],
            "raw_summary": "",
            "last_error": "Server was added disabled, so mandatory software scan was skipped.",
        }
        cache_software_snapshot(config_path, refreshed, server, snapshot)
        print(f"server_record_saved: {server_id}")
        print("software_scan_status: skipped")
        print("next: enable the server and run scan-software before using it for remote tasks.")
        return 0

    scan_args = argparse.Namespace(
        settings=args.settings,
        config=args.config,
        server=server_id,
        allow_disabled=False,
        accept_new_host_key=False,
        timeout=None,
    )
    scan_rc = cmd_scan_software(scan_args)
    if scan_rc != 0:
        print(f"server_record_saved: {server_id}")
        print(f"server_list: {config_path}")
        if backup:
            print(f"backup: {backup}")
        print("software_scan_required: failed")
        return scan_rc

    print(f"added: {server_id}")
    print(f"server_list: {config_path}")
    if backup:
        print(f"backup: {backup}")
    return 0


def cmd_setup_key(args: argparse.Namespace) -> int:
    config, settings, _ = load_config_for_args(args)
    server = select_server(config, args.server, getattr(args, "allow_disabled", False))
    errors = validate_server_shape(config, server, require_key_file=False)
    if errors:
        for error in errors:
            print(f"check failed: {error}", file=sys.stderr)
        raise RemoteSshError("Server local precheck failed.")

    key_path = expand_key_path(config, server)
    public_key_path = expand_public_key_path(config, server)
    private_exists = key_path.exists()
    public_exists = public_key_path.exists()
    batch_mode = any(item == "BatchMode=yes" for item in ssh_options(settings))

    print(f"server: {server.label}")
    print(f"private_key: {REDACTED}")
    print(f"public_key: {REDACTED}")
    print(f"private_key_exists: {str(private_exists).lower()}")
    print(f"public_key_exists: {str(public_exists).lower()}")
    print(f"default_workdir: {REDACTED}")
    print(f"batch_mode_check: {'BatchMode=yes' if batch_mode else 'BatchMode not configured'}")
    print("next: keep the private key local and install only the public key in the remote account's ~/.ssh/authorized_keys.")
    print("next: run check, then exec with a short command such as echo ok to verify passwordless SSH.")
    if not private_exists:
        print("next: create or place the private key outside this helper, then update key_name or default_key_dir.")
    if private_exists and not public_exists:
        print("next: create the matching public key file before sharing the key material with the remote account.")
    return 0 if private_exists and public_exists else 2


def prepare_server_for_remote(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], Server]:
    config, settings, _ = load_config_for_args(args)
    server = select_server(config, args.server, getattr(args, "allow_disabled", False))
    errors = validate_server(config, server)
    if errors:
        for error in errors:
            print(f"check failed: {error}", file=sys.stderr)
        raise RemoteSshError("Server local precheck failed.")
    return config, settings, server


def cmd_workspace_check(args: argparse.Namespace) -> int:
    config, settings, server = prepare_server_for_remote(args)
    result = run_ssh(build_ssh_args(config, settings, server, "pwd && test -d . && test -x ."), args.timeout or default_timeout(settings))
    print(f"server: {server.label}")
    print(f"workdir: {REDACTED}")
    if result.returncode != 0:
        summary = summarize_error(result.stderr)
        if summary:
            print(summary, file=sys.stderr)
        print("status: failed")
        return result.returncode
    print("status: ok")
    return 0


def cmd_file_list(args: argparse.Namespace) -> int:
    config, settings, server = prepare_server_for_remote(args)
    relative_path = remote_relative_path(args.path)
    data = remote_json_operation(config, settings, server, REMOTE_LIST_SCRIPT, relative_path)
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def cmd_file_stat(args: argparse.Namespace) -> int:
    config, settings, server = prepare_server_for_remote(args)
    relative_path = remote_relative_path(args.path)
    data = remote_json_operation(config, settings, server, REMOTE_STAT_SCRIPT, relative_path)
    print(json.dumps(data, indent=2, ensure_ascii=False))
    return 0


def cmd_file_download(args: argparse.Namespace) -> int:
    config, settings, server = prepare_server_for_remote(args)
    relative_path = remote_relative_path(args.remote, allow_dot=False)
    local_target = resolve_download_target(settings, args.local)
    remote_info = resolve_remote_path(config, settings, server, relative_path, allow_missing=False)
    max_bytes = positive_int_setting(settings, "files", "max_transfer_bytes", 100 * 1024 * 1024)
    if remote_info.get("is_file") and isinstance(remote_info.get("size"), int) and int(remote_info["size"]) > max_bytes:
        raise RemoteSshError("Remote file exceeds files.max_transfer_bytes.")
    if local_target.exists() and local_target.is_dir():
        local_target = local_target / posix_basename(relative_path)
    local_target.parent.mkdir(parents=True, exist_ok=True)
    result = run_scp(
        build_scp_download_args(
            config,
            settings,
            server,
            str(remote_info["target"]),
            local_target,
            recursive=bool(remote_info.get("is_dir")),
        ),
        args.timeout or default_timeout(settings),
    )
    if result.returncode != 0:
        summary = summarize_error(result.stderr)
        if summary:
            print(redact_text(config, summary), file=sys.stderr)
        return result.returncode
    print(f"downloaded: {local_target}")
    return 0


def cmd_request_upload(args: argparse.Namespace) -> int:
    config, settings, server = prepare_server_for_remote(args)
    local_source = resolve_local_project_path(args.local, must_exist=True)
    remote_path = remote_relative_path(args.remote, allow_dot=False)
    path = create_request(
        settings,
        "upload",
        server,
        {
            "local_project_path": relative_to_project(local_source),
            "remote_path": remote_path,
            "recursive": local_source.is_dir(),
        },
        reason=args.reason or "",
    )
    print_request_created(path, load_request(path))
    return 0


def cmd_request_mkdir(args: argparse.Namespace) -> int:
    _, settings, server = prepare_server_for_remote(args)
    remote_path = remote_relative_path(args.path, allow_dot=False)
    path = create_request(settings, "mkdir", server, {"remote_path": remote_path}, reason=args.reason or "")
    print_request_created(path, load_request(path))
    return 0


def cmd_request_delete(args: argparse.Namespace) -> int:
    _, settings, server = prepare_server_for_remote(args)
    remote_path = remote_relative_path(args.path, allow_dot=False)
    path = create_request(
        settings,
        "delete",
        server,
        {"remote_path": remote_path, "recursive": bool(args.recursive)},
        reason=args.reason or "",
    )
    print_request_created(path, load_request(path))
    return 0


def cmd_request_command(args: argparse.Namespace) -> int:
    _, settings, server = prepare_server_for_remote(args)
    command = remote_command_from_tokens(args.remote_command)
    path = create_request(settings, "command", server, {"command": command}, reason=args.reason)
    print_request_created(path, load_request(path))
    return 0


def execute_remote_simple(
    config: dict[str, Any],
    settings: dict[str, Any],
    server: Server,
    command: str,
    timeout: int,
) -> int:
    result = run_ssh(build_ssh_args(config, settings, server, command), timeout)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode != 0:
        summary = summarize_error(result.stderr)
        if summary:
            print(redact_text(config, summary), file=sys.stderr)
    return result.returncode


def cmd_run_request(args: argparse.Namespace) -> int:
    if not args.execute:
        raise RemoteSshError("run-request requires --execute.")
    request = load_request(args.request)
    config, settings, _ = load_config_for_args(args)
    server = select_server(config, str(request["server"]), False)
    errors = validate_server(config, server)
    if errors:
        raise RemoteSshError("Server local precheck failed: " + "; ".join(errors))

    operation = str(request["operation"])
    payload = request["payload"]
    timeout = args.timeout or default_timeout(settings)
    print(f"request_id: {request.get('request_id')}")
    print(f"operation: {operation}")
    print(f"server: {server.label}")
    for risk in request.get("risk_summary", []):
        print(f"risk: {risk}")

    workspace_rc = cmd_workspace_check(argparse.Namespace(settings=args.settings, config=args.config, server=server.id, allow_disabled=False, timeout=timeout))
    if workspace_rc != 0:
        return workspace_rc

    if operation == "upload":
        local_source = resolve_local_project_path(payload.get("local_project_path", ""), must_exist=True)
        remote_path = remote_relative_path(str(payload.get("remote_path", "")), allow_dot=False)
        parent = posix_dirname(remote_path) or "."
        parent_info = resolve_remote_path(config, settings, server, parent, allow_missing=False)
        remote_target = str(parent_info["target"])
        if posix_basename(remote_path):
            remote_target = f"{remote_target.rstrip('/')}/{posix_basename(remote_path)}"
        result = run_scp(
            build_scp_upload_args(config, settings, server, local_source, remote_target, recursive=local_source.is_dir()),
            timeout,
        )
        if result.returncode != 0:
            summary = summarize_error(result.stderr)
            if summary:
                print(redact_text(config, summary), file=sys.stderr)
            return result.returncode
        print("status: uploaded")
        return 0

    if operation == "mkdir":
        remote_path = remote_relative_path(str(payload.get("remote_path", "")), allow_dot=False)
        remote_info = resolve_remote_path(config, settings, server, remote_path, allow_missing=True)
        return execute_remote_simple(config, settings, server, f"mkdir -p -- {shlex.quote(str(remote_info['target']))}", timeout)

    if operation == "delete":
        remote_path = remote_relative_path(str(payload.get("remote_path", "")), allow_dot=False)
        remote_info = resolve_remote_path(config, settings, server, remote_path, allow_missing=False)
        recursive = bool(payload.get("recursive"))
        if remote_info.get("is_dir") and not recursive:
            command = f"rmdir -- {shlex.quote(str(remote_info['target']))}"
        elif recursive:
            command = f"rm -rf -- {shlex.quote(str(remote_info['target']))}"
        else:
            command = f"rm -- {shlex.quote(str(remote_info['target']))}"
        return execute_remote_simple(config, settings, server, command, timeout)

    if operation == "command":
        command = str(payload.get("command", "")).strip()
        if not command:
            raise RemoteSshError("Command request has an empty command.")
        return execute_remote_simple(config, settings, server, command, timeout)

    raise RemoteSshError(f"Unsupported request operation: {operation}")


def shell_literal(value: str) -> str:
    return shlex.quote(value)


def render_scan_command(template: str, path_expression: str) -> str:
    command = template.replace("{path}", path_expression).strip()
    return command or f"{path_expression} --version 2>&1"


def build_software_scan_script(settings: dict[str, Any]) -> str:
    catalog = software_catalog(settings)
    lines = [
        "set -u",
        "field() { printf '%s: %s\\n' \"$1\" \"$2\"; }",
        "clean_value() { printf '%s' \"${1:-}\" | tr '\\n\\r:' '   '; }",
        "emit_software() { printf 'software:%s:%s:%s:%s:%s\\n' \"$(clean_value \"$1\")\" \"$(clean_value \"$2\")\" \"$(clean_value \"$3\")\" \"$(clean_value \"$4\")\" \"$(clean_value \"$5\")\"; }",
        "command_or_empty() { command -v \"$1\" >/dev/null 2>&1 && \"$@\" 2>/dev/null || true; }",
        "field hostname \"$(hostname 2>/dev/null || true)\"",
        "field kernel \"$(uname -srmo 2>/dev/null || true)\"",
        "if command -v lscpu >/dev/null 2>&1; then",
        "  field cpu_model \"$(lscpu 2>/dev/null | awk -F: '/Model name/ {gsub(/^[ \\t]+/, \"\", $2); print $2; exit}')\"",
        "  field cpu_threads \"$(lscpu 2>/dev/null | awk -F: '/^CPU\\(s\\)/ {gsub(/^[ \\t]+/, \"\", $2); print $2; exit}')\"",
        "else",
        "  field cpu_model \"$(awk -F: '/model name/ {gsub(/^[ \\t]+/, \"\", $2); print $2; exit}' /proc/cpuinfo 2>/dev/null || true)\"",
        "  field cpu_threads \"$(grep -c '^processor' /proc/cpuinfo 2>/dev/null || true)\"",
        "fi",
        "field gpu_nvidia \"$(command_or_empty nvidia-smi --query-gpu=name --format=csv,noheader | paste -sd ';' -)\"",
        "fpga_devices=\"$(command -v lspci >/dev/null 2>&1 && lspci | grep -e Xilinx 2>/dev/null || true)\"",
        "field fpga_xilinx \"$(printf '%s' \"$fpga_devices\" | paste -sd ';' -)\"",
        "if [ -n \"$fpga_devices\" ]; then",
        "  device_id=0",
        "  set -- $(printf '%s\\n' \"$fpga_devices\" | awk '{print $1}')",
        "  while [ \"$#\" -gt 0 ]; do",
        "    mgmt=\"$1\"",
        "    shift || true",
        "    user=\"${1:-}\"",
        "    if [ \"$#\" -gt 0 ]; then shift || true; fi",
        "    printf 'fpga_device:%s:%s:%s\\n' \"$device_id\" \"$mgmt\" \"$user\"",
        "    device_id=$((device_id + 1))",
        "  done",
        "fi",
    ]

    for item in catalog:
        tool_id = str(item["id"]).strip()
        found_var = f"found_tool_{tool_id.replace('-', '_').replace('.', '_')}"
        lines.append(f"{found_var}=0")
        commands = [str(command).strip() for command in item.get("commands", [])]
        if commands:
            lines.append("tool_path=\"\"")
            for command in commands:
                quoted = shell_literal(command)
                lines.append(f"if [ -z \"$tool_path\" ] && command -v {quoted} >/dev/null 2>&1; then tool_path=\"$(command -v {quoted})\"; fi")
            lines.append("if [ -n \"$tool_path\" ]; then")
            version_template = str(item.get("version_command") or "{path} --version 2>&1 | head -n 1")
            install_template = str(item.get("install_path_command") or "")
            version_command = render_scan_command(version_template, '"$tool_path"')
            lines.append(f"  tool_version=\"$({version_command} 2>/dev/null | head -n 1 || true)\"")
            if install_template:
                install_command = render_scan_command(install_template, '"$tool_path"')
                lines.append(f"  install_path=\"$({install_command} 2>/dev/null | head -n 1 || true)\"")
            else:
                lines.append("  install_path=\"\"")
            lines.append(f"  emit_software {shell_literal(tool_id)} installed \"$tool_path\" \"$tool_version\" \"$install_path\"")
            lines.append(f"  {found_var}=1")
            lines.append("fi")
        for scan in item.get("directory_scans", []) or []:
            subdir = str(scan["subdir"]).strip().strip("/")
            executable = str(scan["executable"]).strip().strip("/")
            version_template = str(scan.get("version_command") or "{path} -version 2>&1 | head -n 1")
            base_dirs = " ".join(shell_literal(str(path)) for path in scan["base_dirs"])
            lines.append(f"for base_path in {base_dirs}; do")
            lines.append(f"  scan_root=\"$base_path/{subdir}\"")
            lines.append("  if [ -d \"$scan_root\" ]; then")
            lines.append("    for version_dir in \"$scan_root\"/*; do")
            lines.append(f"      exec_path=\"$version_dir/{executable}\"")
            lines.append("      if [ -d \"$version_dir\" ] && [ -x \"$exec_path\" ]; then")
            version_command = render_scan_command(version_template, '"$exec_path"')
            lines.append(f"        tool_version=\"$({version_command} 2>/dev/null | head -n 1 || true)\"")
            lines.append(f"        emit_software {shell_literal(tool_id)} installed \"$exec_path\" \"$tool_version\" \"$version_dir\"")
            lines.append(f"        {found_var}=1")
            lines.append("      fi")
            lines.append("    done")
            lines.append("  fi")
            lines.append("done")
        lines.append(f"if [ \"${found_var}\" -eq 0 ]; then emit_software {shell_literal(tool_id)} not_detected \"\" \"\" \"\"; fi")
    return "\n".join(lines) + "\n"


def cmd_list(args: argparse.Namespace) -> int:
    config, _, _ = load_config_for_args(args)
    servers = get_servers(config)
    if not args.all:
        servers = [server for server in servers if server.enabled]
    if not servers:
        print("No servers found.")
        return 0

    print("id\tname\tenabled\tvalidation\tworkspace\ttarget\tkey")
    for server in servers:
        target = user_host(server) if args.show_sensitive else f"{redact(server.username)}@{redact(server.host)}"
        key = str(expand_key_path(config, server)) if args.show_sensitive else redact(server.key_name)
        print(
            f"{server.id}\t{server.name}\t{server.enabled}\t{server.validation_status}"
            f"\t{server.workspace_status}\t{target}\t{key}"
        )
    return 0


def cmd_check(args: argparse.Namespace) -> int:
    config, _, _ = load_config_for_args(args)
    server = select_server(config, args.server, args.allow_disabled)
    errors = validate_server(config, server)

    print(f"server: {server.label}")
    print(f"name: {server.name}")
    print(f"enabled: {server.enabled}")
    print(f"validation_status: {server.validation_status}")
    print(f"workspace_status: {server.workspace_status}")
    if args.show_sensitive:
        print(f"target: {user_host(server)}")
        print(f"key_path: {expand_key_path(config, server)}")
    else:
        print(f"target: {REDACTED}")
        print(f"key_path: {REDACTED}")

    warnings = []
    if server.validation_status == "failed":
        warning = "validation status is failed"
        if server.validation_error:
            warning = f"{warning}: {redact_text(config, server.validation_error)}"
        warnings.append(warning)
    if server.workspace_status in {"failed", "skipped"}:
        warnings.append(f"workspace check status is {server.workspace_status}")
    for warning in warnings:
        print(f"warning: {warning}")

    if errors:
        print("status: failed")
        for error in errors:
            print(f"- {error}")
        return 2

    print("status: ok")
    return 0


def cmd_command(args: argparse.Namespace) -> int:
    config, settings, _ = load_config_for_args(args)
    server = select_server(config, args.server, args.allow_disabled)
    errors = validate_server(config, server)
    if errors:
        for error in errors:
            print(f"check failed: {error}", file=sys.stderr)
        return 2

    command = display_command(build_ssh_args(config, settings, server, accept_new_host_key=args.accept_new_host_key))
    print(command if args.show_sensitive else redact_command(config, command))
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    config, settings, _ = load_config_for_args(args)
    server = select_server(config, args.server, args.allow_disabled)
    errors = validate_server(config, server)
    if errors:
        for error in errors:
            print(f"check failed: {error}", file=sys.stderr)
        return 2

    remote_command = remote_command_from_tokens(args.remote_command)
    timeout = args.timeout if args.timeout is not None else default_timeout(settings)
    result = run_ssh(build_ssh_args(config, settings, server, remote_command, args.accept_new_host_key), timeout)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode != 0:
        summary = summarize_error(result.stderr)
        if summary:
            print(summary, file=sys.stderr)
        return result.returncode
    return 0


def add_software_record(tools: dict[str, Any], tool_id: str, status: str, path: str, version: str, install_path: str) -> None:
    record = {
        "status": status or "not_detected",
        "path": path,
        "version": version,
        "install_path": install_path,
    }
    current = tools.get(tool_id)
    if current is None:
        if status == "installed":
            record["versions"] = [dict(record)]
        tools[tool_id] = record
        return
    if status != "installed":
        return
    current["status"] = "installed"
    current.setdefault("versions", [])
    current["versions"].append(dict(record))
    if not current.get("path"):
        current["path"] = path
    if not current.get("version"):
        current["version"] = version
    if not current.get("install_path"):
        current["install_path"] = install_path


def parse_inventory(output: str) -> dict[str, Any]:
    inventory: dict[str, Any] = {}
    software_tools: dict[str, Any] = {}
    fpga_devices: list[dict[str, Any]] = []
    for line in output.splitlines():
        if line.startswith("software:"):
            parts = line.split(":", 5)
            if len(parts) == 6:
                _, tool_id, status, path, version, install_path = parts
                add_software_record(software_tools, tool_id.strip(), status.strip(), path.strip(), version.strip(), install_path.strip())
            continue
        if line.startswith("fpga_device:"):
            match = re.match(r"^fpga_device:(\d+):((?:[0-9A-Fa-f]{4}:)?[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-7]):((?:[0-9A-Fa-f]{4}:)?[0-9A-Fa-f]{2}:[0-9A-Fa-f]{2}\.[0-7])?$", line)
            if match:
                device_id, mgmt, user = match.groups()
                try:
                    parsed_id = int(device_id)
                except ValueError:
                    parsed_id = len(fpga_devices)
                fpga_devices.append({"device_id": parsed_id, "pcie_bdf_mgmt": mgmt.strip(), "pcie_bdf_user": (user or "").strip()})
            continue
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        inventory[key.strip()] = value.strip() or "not detected"
    inventory["_software_tools"] = software_tools
    inventory["_fpga_devices"] = fpga_devices
    return inventory


def run_software_scan(config: dict[str, Any], settings: dict[str, Any], server: Server, args: argparse.Namespace) -> tuple[subprocess.CompletedProcess[str], dict[str, Any]]:
    timeout = args.timeout if getattr(args, "timeout", None) is not None else default_timeout(settings)
    script = build_software_scan_script(settings)
    result = run_ssh(build_ssh_args(config, settings, server, script, getattr(args, "accept_new_host_key", False)), timeout)
    inventory = parse_inventory(result.stdout) if result.returncode == 0 else {}
    return result, inventory


def software_snapshot_from_inventory(settings: dict[str, Any], inventory: dict[str, Any], raw_output: str) -> dict[str, Any]:
    return {
        "status": "ok",
        "scanned_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "catalog_version": software_catalog_version(settings),
        "tools": inventory.get("_software_tools", {}),
        "fpga_devices": inventory.get("_fpga_devices", []),
        "raw_summary": raw_output.strip(),
    }


def failed_software_snapshot(settings: dict[str, Any], message: str) -> dict[str, Any]:
    return {
        "status": "failed",
        "scanned_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "catalog_version": software_catalog_version(settings),
        "tools": {},
        "fpga_devices": [],
        "raw_summary": "",
        "last_error": message,
    }


def cache_software_snapshot(config_path: Path, config: dict[str, Any], server: Server, snapshot: dict[str, Any]) -> None:
    selector = server.id.casefold()
    for record in config.get("servers", []):
        if isinstance(record, dict) and str(record.get("id", "")).casefold() == selector:
            record["software_scan"] = snapshot
            write_json_atomic(config_path, config)
            return
    raise RemoteSshError(f"Cannot update software scan cache; server not found: {server.label}")


def print_software_snapshot(server: Server, snapshot: dict[str, Any], name: str | None = None) -> int:
    status = str(snapshot.get("status", "unknown"))
    print(f"server: {server.label}")
    print(f"software_scan_status: {status}")
    if snapshot.get("scanned_at"):
        print(f"scanned_at: {snapshot.get('scanned_at')}")
    tools = snapshot.get("tools", {})
    if not isinstance(tools, dict):
        tools = {}
    if name:
        folded = name.casefold()
        match_key = next((key for key in tools if key.casefold() == folded), None)
        if match_key is None:
            print(f"name: {name}")
            print("status: not_scanned")
            return 3
        tool = tools.get(match_key) or {}
        print(f"name: {match_key}")
        print(f"status: {tool.get('status', 'not_detected')}")
        print(f"path: {tool.get('path', '')}")
        print(f"version: {tool.get('version', '')}")
        print(f"install_path: {tool.get('install_path', '')}")
        versions = tool.get("versions", [])
        if isinstance(versions, list) and len(versions) > 1:
            for version in versions:
                if isinstance(version, dict):
                    print(
                        "version_entry:"
                        f" {version.get('path', '')}"
                        f" | {version.get('version', '')}"
                        f" | {version.get('install_path', '')}"
                    )
        return 0 if tool.get("status") == "installed" else 3
    print("name\tstatus\tpath\tversion\tinstall_path")
    for key in sorted(tools):
        tool = tools.get(key) or {}
        print(f"{key}\t{tool.get('status', 'not_detected')}\t{tool.get('path', '')}\t{tool.get('version', '')}\t{tool.get('install_path', '')}")
    return 0 if status == "ok" else 3


def cmd_scan_software(args: argparse.Namespace) -> int:
    config, settings, config_path = load_config_for_args(args)
    server = select_server(config, args.server, args.allow_disabled)
    errors = validate_server(config, server)
    if errors:
        snapshot = failed_software_snapshot(settings, "; ".join(errors))
        cache_software_snapshot(config_path, config, server, snapshot)
        for error in errors:
            print(f"check failed: {error}", file=sys.stderr)
        print_software_snapshot(server, snapshot)
        return 2
    result, inventory = run_software_scan(config, settings, server, args)
    if result.returncode != 0:
        summary = summarize_error(result.stderr) or f"SSH command failed with exit code {result.returncode}."
        snapshot = failed_software_snapshot(settings, summary)
        cache_software_snapshot(config_path, config, server, snapshot)
        print(summary, file=sys.stderr)
        print_software_snapshot(server, snapshot)
        return result.returncode
    snapshot = software_snapshot_from_inventory(settings, inventory, result.stdout)
    cache_software_snapshot(config_path, config, server, snapshot)
    return print_software_snapshot(server, snapshot)


def cmd_software(args: argparse.Namespace) -> int:
    config, _, _ = load_config_for_args(args)
    server = select_server(config, args.server, args.allow_disabled)
    snapshot = server.raw.get("software_scan")
    if not isinstance(snapshot, dict) or not snapshot:
        print(f"server: {server.label}")
        print("software_scan_status: missing")
        print("next: run scan-software for this server.")
        return 3
    return print_software_snapshot(server, snapshot, args.name)


def cmd_inventory(args: argparse.Namespace) -> int:
    config, settings, _ = load_config_for_args(args)
    server = select_server(config, args.server, args.allow_disabled)
    errors = validate_server(config, server)
    if errors:
        for error in errors:
            print(f"check failed: {error}", file=sys.stderr)
        return 2

    result, inventory = run_software_scan(config, settings, server, args)
    if result.returncode != 0:
        summary = summarize_error(result.stderr)
        if summary:
            print(summary, file=sys.stderr)
        return result.returncode

    tools = inventory.get("_software_tools", {})
    print(f"server: {server.label}")
    for key in [
        "hostname",
        "kernel",
        "cpu_model",
        "cpu_threads",
        "gpu_nvidia",
        "fpga_xilinx",
        "python",
        "conda",
        "cuda",
        "gcc",
        "gpp",
        "cmake",
        "vivado",
        "vitis",
    ]:
        if key in tools:
            tool = tools.get(key, {})
            value = tool.get("version") or tool.get("path") or "not detected"
            if tool.get("status") != "installed":
                value = "not detected"
            print(f"{key}: {value}")
        else:
            print(f"{key}: {inventory.get(key, 'not detected')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Erie Remote SSH helper for structured server lists.")
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    def positive_int(value: str) -> int:
        try:
            parsed = int(value)
        except ValueError as exc:
            raise argparse.ArgumentTypeError("must be a positive integer") from exc
        if parsed < 1:
            raise argparse.ArgumentTypeError("must be a positive integer")
        return parsed

    def add_common(
        subparser: argparse.ArgumentParser,
        include_server: bool = True,
        include_host_key_policy: bool = False,
    ) -> None:
        subparser.add_argument("--settings", type=Path, help="Path to Erie Remote SSH settings JSON.")
        subparser.add_argument("--config", type=Path, help="Path to server_list JSON. Overrides settings.")
        if include_server:
            subparser.add_argument("--server", required=True, help="Server id, name, or legacy id.")
            subparser.add_argument("--allow-disabled", action="store_true", help="Allow disabled targets.")
        if include_host_key_policy:
            subparser.add_argument(
                "--accept-new-host-key",
                action="store_true",
                help="Allow OpenSSH to add a new host key to the user's known_hosts file.",
            )

    discover_parser = subparsers.add_parser("discover", help="Discover whether SSH server configuration exists.")
    add_common(discover_parser, include_server=False)
    discover_parser.add_argument("--json", action="store_true", help="Print a machine-readable discovery summary.")
    discover_parser.set_defaults(func=cmd_discover)

    choices_parser = subparsers.add_parser("choices", help="Show selectable servers grouped by category and function.")
    add_common(choices_parser, include_server=False)
    choices_parser.add_argument("--all", action="store_true", help="Include disabled servers.")
    choices_parser.add_argument("--json", action="store_true", help="Print a machine-readable grouped server choice summary.")
    choices_parser.add_argument("--show-sensitive", action="store_true", help="Show full target and key path.")
    choices_parser.set_defaults(func=cmd_choices)

    init_parser = subparsers.add_parser("init-config", help="Create an empty v1 server list if needed.")
    add_common(init_parser, include_server=False)
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing server list after creating a backup.")
    init_parser.set_defaults(func=cmd_init_config)

    add_server_parser = subparsers.add_parser("add-server", help="Add a server entry to the configured server list.")
    add_common(add_server_parser, include_server=False)
    add_server_parser.add_argument("--interactive", action="store_true", help="Prompt for server fields and write the server list.")
    add_server_parser.set_defaults(func=cmd_add_server)

    setup_key_parser = subparsers.add_parser("setup-key", help="Check local key files and print passwordless SSH setup guidance.")
    add_common(setup_key_parser)
    setup_key_parser.set_defaults(func=cmd_setup_key)

    workspace_parser = subparsers.add_parser("workspace-check", help="Check the configured remote workdir.")
    add_common(workspace_parser)
    workspace_parser.add_argument("--timeout", type=positive_int, help="SSH timeout in seconds. Defaults to settings.")
    workspace_parser.set_defaults(func=cmd_workspace_check)

    file_list_parser = subparsers.add_parser("file-list", help="List a path inside the remote workdir.")
    add_common(file_list_parser)
    file_list_parser.add_argument("--path", required=True, help="Relative path inside server workdir.")
    file_list_parser.set_defaults(func=cmd_file_list)

    file_stat_parser = subparsers.add_parser("file-stat", help="Stat a path inside the remote workdir.")
    add_common(file_stat_parser)
    file_stat_parser.add_argument("--path", required=True, help="Relative path inside server workdir.")
    file_stat_parser.set_defaults(func=cmd_file_stat)

    file_download_parser = subparsers.add_parser("file-download", help="Download a remote workdir path into downloads_dir.")
    add_common(file_download_parser)
    file_download_parser.add_argument("--remote", required=True, help="Relative remote path inside server workdir.")
    file_download_parser.add_argument("--local", required=True, help="Local target path inside paths.downloads_dir.")
    file_download_parser.add_argument("--timeout", type=positive_int, help="SCP timeout in seconds. Defaults to settings.")
    file_download_parser.set_defaults(func=cmd_file_download)

    request_upload_parser = subparsers.add_parser("request-upload", help="Create an upload request.")
    add_common(request_upload_parser)
    request_upload_parser.add_argument("--local", required=True, help="Local source path inside project root.")
    request_upload_parser.add_argument("--remote", required=True, help="Relative remote target inside server workdir.")
    request_upload_parser.add_argument("--reason", help="Reason for the upload request.")
    request_upload_parser.set_defaults(func=cmd_request_upload)

    request_mkdir_parser = subparsers.add_parser("request-mkdir", help="Create a remote mkdir request.")
    add_common(request_mkdir_parser)
    request_mkdir_parser.add_argument("--path", required=True, help="Relative remote path inside server workdir.")
    request_mkdir_parser.add_argument("--reason", help="Reason for the mkdir request.")
    request_mkdir_parser.set_defaults(func=cmd_request_mkdir)

    request_delete_parser = subparsers.add_parser("request-delete", help="Create a remote delete request.")
    add_common(request_delete_parser)
    request_delete_parser.add_argument("--path", required=True, help="Relative remote path inside server workdir.")
    request_delete_parser.add_argument("--recursive", action="store_true", help="Allow recursive directory deletion in the request.")
    request_delete_parser.add_argument("--reason", help="Reason for the delete request.")
    request_delete_parser.set_defaults(func=cmd_request_delete)

    request_command_parser = subparsers.add_parser("request-command", help="Create a remote command request.")
    add_common(request_command_parser)
    request_command_parser.add_argument("--reason", required=True, help="Reason for running the remote command.")
    request_command_parser.add_argument("remote_command", nargs=argparse.REMAINDER, help="Remote command after --.")
    request_command_parser.set_defaults(func=cmd_request_command)

    run_request_parser = subparsers.add_parser("run-request", help="Execute an approved request.")
    add_common(run_request_parser, include_server=False)
    run_request_parser.add_argument("--request", required=True, type=Path, help="Request JSON path.")
    run_request_parser.add_argument("--execute", action="store_true", help="Required explicit execution gate.")
    run_request_parser.add_argument("--timeout", type=positive_int, help="SSH/SCP timeout in seconds. Defaults to settings.")
    run_request_parser.set_defaults(func=cmd_run_request)

    list_parser = subparsers.add_parser("list", help="List configured servers.")
    add_common(list_parser, include_server=False)
    list_parser.add_argument("--all", action="store_true", help="Include disabled servers.")
    list_parser.add_argument("--show-sensitive", action="store_true", help="Show full target and key path.")
    list_parser.set_defaults(func=cmd_list)

    check_parser = subparsers.add_parser("check", help="Validate one server locally.")
    add_common(check_parser)
    check_parser.add_argument("--show-sensitive", action="store_true", help="Show full target and key path.")
    check_parser.set_defaults(func=cmd_check)

    command_parser = subparsers.add_parser("command", help="Print an SSH command without connecting.")
    add_common(command_parser, include_host_key_policy=True)
    command_parser.add_argument("--show-sensitive", action="store_true", help="Show a runnable command with full target and key path.")
    command_parser.set_defaults(func=cmd_command)

    exec_parser = subparsers.add_parser("exec", help="Run an explicit remote command over SSH.")
    add_common(exec_parser, include_host_key_policy=True)
    exec_parser.add_argument("--timeout", type=positive_int, help="SSH timeout in seconds. Defaults to settings.")
    exec_parser.add_argument("remote_command", nargs=argparse.REMAINDER, help="Remote command after --.")
    exec_parser.set_defaults(func=cmd_exec)

    scan_software_parser = subparsers.add_parser("scan-software", help="Scan configured software on a remote server and cache the result.")
    add_common(scan_software_parser, include_host_key_policy=True)
    scan_software_parser.add_argument("--timeout", type=positive_int, help="SSH timeout in seconds. Defaults to settings.")
    scan_software_parser.set_defaults(func=cmd_scan_software)

    software_parser = subparsers.add_parser("software", help="Read the cached software scan for a server.")
    add_common(software_parser)
    software_parser.add_argument("--name", help="Specific software id/name to query from the cached scan.")
    software_parser.set_defaults(func=cmd_software)

    inventory_parser = subparsers.add_parser("inventory", help="Collect a remote hardware/software inventory report.")
    add_common(inventory_parser, include_host_key_policy=True)
    inventory_parser.add_argument("--timeout", type=positive_int, help="SSH timeout in seconds. Defaults to settings.")
    inventory_parser.set_defaults(func=cmd_inventory)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args))
    except RemoteSshError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
