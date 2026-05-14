#!/usr/bin/env python3
"""Erie Remote SSH helper for Codex skills.

This tool reads a structured server list, validates local prerequisites,
generates SSH commands, executes explicit remote commands, and gathers a small
server inventory report.
"""

from __future__ import annotations

import argparse
import datetime as dt
import getpass
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
PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+$")


class RemoteSshError(Exception):
    """Expected user-facing error."""


def detect_project_root(skill_dir: Path) -> Path:
    """Resolve the default local project root for settings and upload guards.

    In a repository checkout, the skill lives at <repo>/skills/<skill-name>, so
    `${project_root}` should resolve to the repository root. In an installed or
    release copy, the skill root itself is the safest default local boundary.
    """

    resolved = skill_dir.resolve()
    if len(resolved.parents) < 2:
        return resolved
    repo_candidate = resolved.parents[1]
    source_layout = repo_candidate / "skills" / resolved.name
    repo_markers = ("AGENTS.md", ".git", "PROJECT_GOALS.md", "TASK_PLAN.md")
    if source_layout == resolved and any((repo_candidate / marker).exists() for marker in repo_markers):
        return repo_candidate.resolve()
    return resolved


PROJECT_ROOT = detect_project_root(SKILL_DIR)


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
        "cwd": str(Path.cwd()),
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


def ssh_keygen_client(settings: dict[str, Any]) -> str:
    return str(settings_value(settings, "tools", "ssh_keygen", default="ssh-keygen") or "ssh-keygen")


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


def default_ssh_config_path(settings: dict[str, Any]) -> Path:
    value = settings_value(settings, "ssh", "config_path", default="${home}/.ssh/config")
    if not isinstance(value, str) or not value.strip():
        raise RemoteSshError("Settings field ssh.config_path must be a non-empty string.")
    return resolve_config_path(value, settings_path(settings), settings["_context"])


def jobs_remote_dir(settings: dict[str, Any]) -> str:
    value = settings_value(settings, "jobs", "remote_dir", default=".erie-remote-ssh/jobs")
    if not isinstance(value, str) or not value.strip():
        raise RemoteSshError("Settings field jobs.remote_dir must be a non-empty string.")
    return remote_relative_path(value, allow_dot=False)


def jobs_local_dir(settings: dict[str, Any]) -> Path:
    value = settings_value(settings, "jobs", "local_dir", default="${skill_dir}/reports/jobs")
    if not isinstance(value, str) or not value.strip():
        raise RemoteSshError("Settings field jobs.local_dir must be a non-empty string.")
    return resolve_config_path(value, settings_path(settings), settings["_context"])


def default_tail_lines(settings: dict[str, Any]) -> int:
    value = settings_value(settings, "jobs", "default_tail_lines", default=80)
    if isinstance(value, bool):
        raise RemoteSshError("Settings field jobs.default_tail_lines must be a positive integer.")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise RemoteSshError("Settings field jobs.default_tail_lines must be a positive integer.") from exc
    if parsed < 1:
        raise RemoteSshError("Settings field jobs.default_tail_lines must be a positive integer.")
    return parsed


def project_auto_discover(settings: dict[str, Any]) -> bool:
    value = settings_value(settings, "projects", "auto_discover", default=True)
    return bool(value)


def project_config_dir(settings: dict[str, Any]) -> str:
    value = settings_value(settings, "projects", "config_dir", default=".erie-remote-ssh")
    if not isinstance(value, str) or not value.strip():
        raise RemoteSshError("Settings field projects.config_dir must be a non-empty string.")
    return value.strip()


def project_config_names(settings: dict[str, Any]) -> list[str]:
    value = settings_value(settings, "projects", "config_names", default=["project.local.json", "project.json"])
    if not isinstance(value, list) or not all(isinstance(item, str) and item.strip() for item in value):
        raise RemoteSshError("Settings field projects.config_names must be a list of non-empty strings.")
    return [str(item).strip() for item in value]


def project_workdir_template(settings: dict[str, Any]) -> str:
    value = settings_value(settings, "projects", "default_workdir_template", default="~/workspace/${project_id}")
    if not isinstance(value, str) or not value.strip():
        raise RemoteSshError("Settings field projects.default_workdir_template must be a non-empty string.")
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
        path_scan = str(item.get("path_scan", "first")).strip().casefold()
        if path_scan not in {"first", "all"}:
            raise RemoteSshError(f"Software catalog {tool_id} path_scan must be either 'first' or 'all'.")
        executable_globs = item.get("executable_globs", [])
        if executable_globs is None:
            executable_globs = []
        glob_pattern = re.compile(r"^[A-Za-z0-9_./*?\[\]+-]+$")
        if not isinstance(executable_globs, list) or not all(
            isinstance(path, str)
            and path.strip().startswith("/")
            and ".." not in PurePosixPath(path.strip()).parts
            and glob_pattern.fullmatch(path.strip())
            for path in executable_globs
        ):
            raise RemoteSshError(f"Software catalog {tool_id} executable_globs must contain absolute POSIX paths with safe glob characters.")
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
        if not commands and not scans and not executable_globs:
            raise RemoteSshError(f"Software catalog {tool_id} must define commands, executable_globs, or directory_scans.")
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
    def software_cache_status(self) -> str:
        snapshot = self.raw.get("software_scan") or {}
        if not isinstance(snapshot, dict):
            return "missing"
        return str(snapshot.get("status", "missing"))

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


@dataclass(frozen=True)
class ProjectContext:
    project_id: str
    config_path: Path | None
    data: dict[str, Any]
    effective_workdir: str
    workdir_source: str


@dataclass(frozen=True)
class SshConfigAlias:
    alias: str
    hostname: str = ""
    user: str = ""
    port: str = ""
    identity_file: str = ""


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
    suffix = 1
    while backup.exists():
        backup = path.with_name(f"{path.name}.bak.{timestamp}.{suffix}")
        suffix += 1
    shutil.copy2(path, backup)
    return backup


def load_config_for_args(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], Path]:
    settings = load_settings(args.settings)
    config_path = resolve_server_list_path(settings, args.config)
    return load_config(config_path), settings, config_path


def validate_project_id(project_id: str) -> str:
    value = project_id.strip()
    if not value or value in {".", ".."} or not PROJECT_ID_PATTERN.fullmatch(value):
        raise RemoteSshError("Project id must contain only letters, numbers, '_', '-', or '.'.")
    return value


def validate_remote_workdir(value: str) -> str:
    workdir = value.strip()
    if not workdir:
        raise RemoteSshError("Project remote_workdir must be a non-empty string.")
    if "\x00" in workdir or "\\" in workdir or re.match(r"^[A-Za-z]:", workdir):
        raise RemoteSshError("Project remote_workdir must be a POSIX path.")
    if workdir in {"~", "~/"}:
        raise RemoteSshError("Project remote_workdir must identify a child directory.")
    if "//" in workdir:
        raise RemoteSshError("Project remote_workdir must not contain repeated separators.")
    if not (workdir.startswith("~/") or workdir.startswith("/")):
        raise RemoteSshError("Project remote_workdir must start with ~/ or /.")
    if workdir == "/":
        raise RemoteSshError("Project remote_workdir must not be the filesystem root.")
    raw_parts = workdir[2:].split("/") if workdir.startswith("~/") else workdir[1:].split("/")
    if not raw_parts or any(part in {"", ".", ".."} for part in raw_parts):
        raise RemoteSshError("Project remote_workdir must not contain empty, '.', or '..' path segments.")
    posix = PurePosixPath(workdir.replace("~", "/home-placeholder", 1) if workdir.startswith("~/") else workdir)
    if posix.name in {"", ".", ".."} or any(part in {"", ".", ".."} for part in posix.parts[1:]):
        raise RemoteSshError("Project remote_workdir must not contain empty, '.', or '..' path segments.")
    return workdir


def render_project_workdir(settings: dict[str, Any], project_id: str) -> str:
    template = project_workdir_template(settings)
    rendered = expand_placeholders(template, settings["_context"], {"project_id": project_id})
    return validate_remote_workdir(rendered)


def load_project_config(path: Path, settings: dict[str, Any], server: Server | None = None) -> dict[str, Any]:
    data = load_json_file(path, "Project config")
    if data.get("version") != 1:
        raise RemoteSshError(f"Unsupported project config version: {data.get('version')!r}")
    project_id = validate_project_id(str(data.get("project_id", "")))
    data["project_id"] = project_id
    remote_workdir = data.get("remote_workdir")
    if remote_workdir is not None:
        data["remote_workdir"] = validate_remote_workdir(str(remote_workdir))
    else:
        data["remote_workdir"] = render_project_workdir(settings, project_id)
    servers = data.get("servers", {})
    if servers is None:
        servers = {}
    if not isinstance(servers, dict):
        raise RemoteSshError("Project config field servers must be an object.")
    for selector, item in servers.items():
        if not isinstance(selector, str) or not selector.strip():
            raise RemoteSshError("Project config server keys must be non-empty strings.")
        if not isinstance(item, dict):
            raise RemoteSshError(f"Project config servers.{selector} must be an object.")
        if "remote_workdir" in item:
            item["remote_workdir"] = validate_remote_workdir(str(item["remote_workdir"]))
    data["servers"] = servers
    return data


def discover_project_config(settings: dict[str, Any], cwd: Path | None = None) -> Path | None:
    current = (cwd or Path.cwd()).resolve()
    config_dir = project_config_dir(settings)
    names = project_config_names(settings)
    for directory in [current, *current.parents]:
        for name in names:
            candidate = directory / config_dir / name
            if candidate.exists():
                return candidate.resolve()
    return None


def project_config_for_args(args: argparse.Namespace, settings: dict[str, Any], server: Server | None = None) -> tuple[dict[str, Any] | None, Path | None]:
    if getattr(args, "no_project", False):
        return None, None
    if getattr(args, "project_config", None) is not None:
        path = Path(args.project_config).resolve()
        return load_project_config(path, settings, server), path
    project_value = getattr(args, "project", None)
    if project_value is not None:
        project_id = validate_project_id(str(project_value))
        return {
            "version": 1,
            "project_id": project_id,
            "remote_workdir": render_project_workdir(settings, project_id),
            "servers": {},
        }, None
    if project_auto_discover(settings):
        path = discover_project_config(settings)
        if path is not None:
            return load_project_config(path, settings, server), path
    return None, None


def project_server_entry(project_data: dict[str, Any], server: Server) -> dict[str, Any]:
    servers = project_data.setdefault("servers", {})
    if not isinstance(servers, dict):
        raise RemoteSshError("Project config field servers must be an object.")
    for selector in [server.id, server.name, server.raw.get("legacy_server_id", "")]:
        key = str(selector).strip()
        if key and isinstance(servers.get(key), dict):
            return servers[key]
    return {}


def effective_project_context(args: argparse.Namespace, settings: dict[str, Any], server: Server) -> ProjectContext | None:
    project_data, path = project_config_for_args(args, settings, server)
    if project_data is None:
        return None
    entry = project_server_entry(project_data, server)
    workdir = str(entry.get("remote_workdir") or project_data.get("remote_workdir") or render_project_workdir(settings, str(project_data["project_id"])))
    return ProjectContext(
        project_id=str(project_data["project_id"]),
        config_path=path,
        data=project_data,
        effective_workdir=validate_remote_workdir(workdir),
        workdir_source="project",
    )


def server_with_workdir(server: Server, workdir: str) -> Server:
    raw = dict(server.raw)
    raw["workdir"] = workdir
    return Server(raw)


def server_for_args_project(args: argparse.Namespace, settings: dict[str, Any], server: Server) -> tuple[Server, ProjectContext | None]:
    project = effective_project_context(args, settings, server)
    if project is None:
        return server, None
    return server_with_workdir(server, project.effective_workdir), project


def print_project_context(project: ProjectContext | None) -> None:
    if project is None:
        print("workdir_source: server_default")
        return
    print(f"project: {project.project_id}")
    print("workdir_source: project")


def print_contract_fields(
    server: Server | None,
    *,
    status: str,
    message: str,
    next_action: str,
    workdir_status: str | None = None,
    software_cache_status: str | None = None,
) -> None:
    print(f"status: {status}")
    if server is None:
        print("server_id: not_applicable")
        print("server_name: not_applicable")
        resolved_workdir_status = workdir_status or "not_applicable"
        resolved_software_status = software_cache_status or "not_applicable"
    else:
        print(f"server_id: {server.id}")
        print(f"server_name: {server.name}")
        resolved_workdir_status = workdir_status or server.workspace_status
        resolved_software_status = software_cache_status or server.software_cache_status
    print(f"workdir_status: {resolved_workdir_status}")
    print(f"software_cache_status: {resolved_software_status}")
    print(f"message: {message}")
    print(f"next_action: {next_action}")


def project_output_record(project: ProjectContext | None) -> dict[str, Any]:
    if project is None:
        return {"project_id": None, "workdir_source": "server_default"}
    return {
        "project_id": project.project_id,
        "effective_workdir": project.effective_workdir,
        "workdir_source": project.workdir_source,
    }


def get_servers(config: dict[str, Any]) -> list[Server]:
    return [Server(server) for server in config.get("servers", [])]


def normalize_host(value: str) -> str:
    return value.strip().casefold()


def parse_endpoint_selector(selector: str) -> tuple[str | None, str, int | None] | None:
    value = selector.strip()
    if not value:
        return None
    username: str | None = None
    target = value
    if "@" in target:
        username, target = target.rsplit("@", 1)
        username = username.strip() or None
    host = target.strip()
    port: int | None = None
    if host.startswith("[") and "]" in host:
        closing = host.find("]")
        bracketed_host = host[1:closing].strip()
        suffix = host[closing + 1 :].strip()
        if suffix.startswith(":") and suffix[1:].isdigit():
            port = parse_port_value(suffix[1:])
        elif suffix:
            return None
        host = bracketed_host
    elif host.count(":") == 1:
        host_part, port_part = host.rsplit(":", 1)
        if port_part.isdigit():
            host = host_part.strip()
            port = parse_port_value(port_part)
    if not host:
        return None
    return username, host, port


def server_matches_endpoint(server: Server, username: str | None, host: str, port: int | None) -> bool:
    if normalize_host(server.host) != normalize_host(host):
        return False
    if username is not None and server.username.casefold() != username.casefold():
        return False
    if port is not None and server.port != port:
        return False
    return True


def same_host_servers(config: dict[str, Any], host: str) -> list[Server]:
    normalized = normalize_host(host)
    return [server for server in get_servers(config) if normalize_host(server.host) == normalized]


def format_ambiguous_selector(selector: str, matches: list[Server]) -> str:
    labels = ", ".join(server.label for server in matches)
    return (
        f"Selector matched multiple servers: {labels}. "
        "Choose a server id/name or specify a port, for example user@host:port. "
        "Run choices --host <host> --show-sensitive to inspect login ports."
    )


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
        endpoint = parse_endpoint_selector(selector)
        if endpoint is not None:
            username, host, port = endpoint
            matches = [server for server in get_servers(config) if server_matches_endpoint(server, username, host, port)]
    if not matches:
        raise RemoteSshError(f"No server matched selector: {selector}")
    if len(matches) > 1:
        raise RemoteSshError(format_ambiguous_selector(selector, matches))

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


def ssh_config_has_pattern(alias: str) -> bool:
    return any(char in alias for char in "*?!")


def ssh_config_tokens(line: str) -> list[str]:
    try:
        tokens = shlex.split(line, comments=True, posix=False)
    except ValueError:
        tokens = line.split()
    return [token[1:-1] if len(token) >= 2 and token[0] == token[-1] and token[0] in {"'", '"'} else token for token in tokens]


def parse_ssh_config(path: Path, seen: set[Path] | None = None) -> list[SshConfigAlias]:
    if not path.exists():
        return []
    resolved_path = path.resolve()
    seen_paths = seen or set()
    if resolved_path in seen_paths:
        return []
    seen_paths.add(resolved_path)
    aliases: list[SshConfigAlias] = []
    current_hosts: list[str] = []
    current_options: dict[str, str] = {}
    in_match_block = False

    def flush() -> None:
        nonlocal current_hosts, current_options
        for alias in current_hosts:
            if ssh_config_has_pattern(alias):
                continue
            aliases.append(
                SshConfigAlias(
                    alias=alias,
                    hostname=current_options.get("hostname", ""),
                    user=current_options.get("user", ""),
                    port=current_options.get("port", ""),
                    identity_file=current_options.get("identityfile", ""),
                )
            )
        current_hosts = []
        current_options = {}

    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise RemoteSshError(f"Unable to read SSH config: {path}") from exc

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        tokens = ssh_config_tokens(line)
        if not tokens:
            continue
        key = tokens[0].lower()
        values = tokens[1:]
        if key == "match":
            flush()
            in_match_block = True
            continue
        if key == "host":
            in_match_block = False
            flush()
            current_hosts = [item for item in values if item and not item.startswith("!")]
            continue
        if key == "include" and not current_hosts and not in_match_block:
            for include_value in values:
                include_path = Path(os.path.expandvars(os.path.expanduser(include_value)))
                if not include_path.is_absolute():
                    include_path = resolved_path.parent / include_path
                for include_match in sorted(include_path.parent.glob(include_path.name)):
                    aliases.extend(parse_ssh_config(include_match, seen_paths))
            continue
        if current_hosts and not in_match_block and key in {"hostname", "user", "port", "identityfile"} and values:
            current_options.setdefault(key, values[0])
    flush()
    return aliases


def select_ssh_alias(settings: dict[str, Any], alias: str, config_path: Path | None = None) -> SshConfigAlias:
    requested = str(alias or "").strip()
    if not requested:
        raise RemoteSshError("SSH alias must not be empty.")
    for record in parse_ssh_config((config_path or default_ssh_config_path(settings)).resolve()):
        if record.alias == requested:
            return record
    raise RemoteSshError(f"SSH alias not found in config: {requested}")


def redact_ssh_alias_text(record: SshConfigAlias, value: str) -> str:
    redacted = value
    for item in sorted(
        [record.hostname, record.user, record.port, record.identity_file],
        key=len,
        reverse=True,
    ):
        if item:
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


def build_ssh_alias_args(
    settings: dict[str, Any],
    alias: str,
    remote_command: str | None = None,
    accept_new_host_key: bool = False,
) -> list[str]:
    args = [
        ssh_client(settings),
        *ssh_options(settings, accept_new_host_key),
        alias,
    ]
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
            encoding="utf-8",
            errors="replace",
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
    return resolve_settings_path(settings, "paths", "requests_dir", "${skill_dir}/reports/requests")


def downloads_dir(settings: dict[str, Any]) -> Path:
    return resolve_settings_path(settings, "paths", "downloads_dir", "${skill_dir}/reports/downloads")


def configured_upload_roots(settings: dict[str, Any]) -> list[Path]:
    configured = settings_value(settings, "paths", "upload_roots", default=["${project_root}"])
    if not isinstance(configured, list) or not configured:
        raise RemoteSshError("Settings field paths.upload_roots must be a non-empty list of paths.")
    roots: list[Path] = []
    for item in configured:
        if not isinstance(item, str) or not item.strip():
            raise RemoteSshError("Settings field paths.upload_roots must contain non-empty path strings.")
        root = resolve_config_path(item, settings_path(settings), settings["_context"])
        if root == Path(root.anchor):
            raise RemoteSshError("Settings field paths.upload_roots must not contain a filesystem root.")
        try:
            if root == Path.home().resolve():
                raise RemoteSshError("Settings field paths.upload_roots must not contain the whole user home directory.")
        except RuntimeError:
            pass
        if root not in roots:
            roots.append(root)
    return roots


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


def relative_to_upload_root(path: Path, upload_roots: Iterable[Path]) -> tuple[Path, str]:
    resolved = path.resolve()
    for root in upload_roots:
        try:
            return root, resolved.relative_to(root).as_posix()
        except ValueError:
            continue
    raise RemoteSshError("Local upload path must stay inside configured paths.upload_roots.")


def resolve_local_upload_path(settings: dict[str, Any], value: str | Path, must_exist: bool = True) -> tuple[Path, Path, str]:
    raw = Path(os.path.expandvars(os.path.expanduser(str(value))))
    candidates = [raw.resolve()] if raw.is_absolute() else [(Path.cwd() / raw).resolve(), (PROJECT_ROOT / raw).resolve()]
    upload_roots = configured_upload_roots(settings)
    last_missing: Path | None = None
    for candidate in candidates:
        try:
            root, relative = relative_to_upload_root(candidate, upload_roots)
        except RemoteSshError:
            continue
        if must_exist and not candidate.exists():
            last_missing = candidate
            continue
        return candidate, root, relative
    if last_missing is not None:
        raise RemoteSshError(f"Local path not found: {last_missing}")
    raise RemoteSshError("Local upload path must stay inside configured paths.upload_roots.")


def resolve_local_upload_request_path(settings: dict[str, Any], payload: dict[str, Any], must_exist: bool = True) -> tuple[Path, Path, str]:
    if "local_upload_root" in payload or "local_relative_path" in payload:
        root_value = str(payload.get("local_upload_root", "")).strip()
        relative_value = str(payload.get("local_relative_path", "")).strip()
        if not root_value or not relative_value:
            raise RemoteSshError("Upload request must include local_upload_root and local_relative_path.")
        relative = PurePosixPath(relative_value)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise RemoteSshError("Upload request local_relative_path must be a safe relative path.")
        root = Path(root_value).resolve()
        configured_roots = configured_upload_roots(settings)
        if root not in configured_roots:
            raise RemoteSshError("Local upload path must stay inside configured paths.upload_roots.")
        path = (root / Path(*relative.parts)).resolve()
        matched_root, matched_relative = relative_to_upload_root(path, [root])
        if must_exist and not path.exists():
            raise RemoteSshError(f"Local path not found: {path}")
        return path, matched_root, matched_relative
    return resolve_local_project_path(payload.get("local_project_path", ""), must_exist=must_exist), PROJECT_ROOT.resolve(), str(
        payload.get("local_project_path", "")
    )


def path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def sensitive_local_upload_reasons(path: Path) -> list[str]:
    resolved = path.resolve()
    lowered_parts = [part.casefold() for part in resolved.parts]
    name = resolved.name.casefold()
    reasons: list[str] = []
    if ".codex" in lowered_parts:
        reasons.append("inside .codex")
    if ".ssh" in lowered_parts:
        reasons.append("inside .ssh")
    if name in {".env", "known_hosts", "authorized_keys"} or name.startswith(".env."):
        reasons.append("sensitive configuration file")
    if name in {"id_rsa", "id_dsa", "id_ecdsa", "id_ed25519"} or name.endswith((".pem", ".key", ".ppk")):
        reasons.append("possible private key")
    for env_name in ["SystemRoot", "WINDIR", "ProgramFiles", "ProgramFiles(x86)", "ProgramData"]:
        value = os.environ.get(env_name)
        if value and path_is_relative_to(resolved, Path(value)):
            reasons.append("inside system directory")
            break
    for system_path in [Path("/etc"), Path("/bin"), Path("/sbin"), Path("/usr/bin"), Path("/usr/sbin")]:
        if system_path.exists() and path_is_relative_to(resolved, system_path):
            reasons.append("inside system directory")
            break
    deduped: list[str] = []
    for reason in reasons:
        if reason not in deduped:
            deduped.append(reason)
    return deduped


def require_sensitive_upload_confirmation(reasons: list[str], confirmed: bool, reason: str) -> None:
    if not reasons:
        return
    if not confirmed or not reason.strip():
        raise RemoteSshError("Sensitive local upload requires --confirm-sensitive-local-upload and --reason.")


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


def create_request(
    settings: dict[str, Any],
    operation: str,
    server: Server,
    payload: dict[str, Any],
    reason: str = "",
    risk_summary: list[str] | None = None,
    project: ProjectContext | None = None,
) -> Path:
    request = {
        "version": 1,
        "request_id": request_id(operation),
        "operation": operation,
        "server": server.id,
        "reason": reason,
        "created_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "payload": payload,
    }
    if project is not None:
        request.update(project_output_record(project))
    if risk_summary is not None:
        request["risk_summary"] = risk_summary
    elif operation == "command":
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


def print_request_created(path: Path, request: dict[str, Any], server: Server, project: ProjectContext | None = None) -> None:
    print_contract_fields(
        server,
        status="pending",
        message=f"Created a reviewed {request['operation']} request.",
        next_action="Review the request, then run run-request --request <request.json> --execute when you are ready.",
        workdir_status="project_pending" if project is not None else server.workspace_status,
    )
    print(f"request: {path}")
    print(f"operation: {request['operation']}")
    print(f"server: {request['server']}")
    if request.get("project_id"):
        print(f"project: {request['project_id']}")
        print("workdir_source: project")
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
        record["username"] = server.username
        record["port"] = server.port
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
        aliases = parse_ssh_config(default_ssh_config_path(settings))
        next_steps = [
            "Run scripts/bat/config/configure_remote_ssh.bat, scripts/shell/config/configure_remote_ssh.sh, or scripts/powershell/config/configure_remote_ssh.ps1",
            "Or run remote_ssh.py init-config then remote_ssh.py add-server --interactive",
        ]
        if aliases:
            next_steps.append("Or run remote_ssh.py ssh-config-discover for read-only temporary SSH alias targets.")
        return (
            {
                "status": "not_configured",
                "server_list_exists": False,
                "server_list_path": REDACTED,
                "server_count": 0,
                "enabled_ssh_count": 0,
                "ssh_config_fallback_available": bool(aliases),
                "ssh_config_alias_count": len(aliases),
                "ssh_config_aliases": [{"alias": record.alias} for record in aliases],
                "message": "No configured SSH server list is available yet.",
                "next_action": next_steps[0],
                "next_steps": next_steps,
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
    message = "Configured SSH targets are available." if enabled_servers else "A server list exists, but no enabled SSH targets are configured."

    return (
        {
            "status": status,
            "server_list_exists": True,
            "server_list_path": REDACTED,
            "server_count": len(servers),
            "enabled_ssh_count": len(enabled_servers),
            "enabled_ssh_servers": [{"id": server.id, "name": server.name} for server in enabled_servers],
            "message": message,
            "next_action": next_steps[0],
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

    print_contract_fields(
        None,
        status=str(summary["status"]),
        message=str(summary["message"]),
        next_action=str(summary["next_action"]),
    )
    print(f"server_list_exists: {summary['server_list_exists']}")
    print(f"server_count: {summary['server_count']}")
    print(f"enabled_ssh_count: {summary['enabled_ssh_count']}")
    if "ssh_config_fallback_available" in summary:
        print(f"ssh_config_fallback_available: {str(summary['ssh_config_fallback_available']).lower()}")
        print(f"ssh_config_alias_count: {summary['ssh_config_alias_count']}")
        for alias in summary.get("ssh_config_aliases", []):
            print(f"ssh_config_alias: {alias['alias']}")
    for server in summary.get("enabled_ssh_servers", []):
        print(f"- {server['id']}: {server['name']}")
    for step in summary["next_steps"]:
        print(f"next: {step}")
    return exit_code


def cmd_ssh_config_discover(args: argparse.Namespace) -> int:
    settings = load_settings(args.settings)
    config_path = (args.ssh_config or default_ssh_config_path(settings)).resolve()
    aliases = parse_ssh_config(config_path)
    status = "available" if aliases else "not_configured"
    if args.json:
        payload: dict[str, Any] = {
            "status": status,
            "ssh_config_exists": config_path.exists(),
            "ssh_config_path": str(config_path) if args.show_sensitive else REDACTED,
            "alias_count": len(aliases),
            "aliases": [
                {
                    "alias": record.alias,
                    **(
                        {
                            "hostname": record.hostname,
                            "user": record.user,
                            "port": record.port,
                            "identity_file": record.identity_file,
                        }
                        if args.show_sensitive
                        else {}
                    ),
                }
                for record in aliases
            ],
        }
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if aliases else 3

    print(f"status: {status}")
    print(f"ssh_config_exists: {str(config_path.exists()).lower()}")
    print(f"ssh_config_path: {config_path if args.show_sensitive else REDACTED}")
    print(f"alias_count: {len(aliases)}")
    for record in aliases:
        print(f"alias: {record.alias}")
        if args.show_sensitive:
            if record.hostname:
                print(f"  hostname: {record.hostname}")
            if record.user:
                print(f"  user: {record.user}")
            if record.port:
                print(f"  port: {record.port}")
            if record.identity_file:
                print(f"  identity_file: {record.identity_file}")
    if aliases:
        print("next: use command or exec with --ssh-alias <alias> for a temporary read-only alias target.")
        return 0
    print("next: create a server list or add simple Host aliases to the selected SSH config.")
    return 3


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
    if args.host:
        source_servers = servers if args.all else enabled_servers
        selected = [server for server in source_servers if normalize_host(server.host) == normalize_host(args.host)]
    elif args.all:
        selected = servers
    elif enabled_servers:
        selected = enabled_servers
    else:
        selected = servers

    if args.host and not selected:
        status = "no_matching_host"
        exit_code = 4
    else:
        status = "available" if enabled_servers else "no_enabled_ssh"
        exit_code = 0 if enabled_servers else 4
    records = [server_choice_record(config, server, args.show_sensitive) for server in selected]
    grouped = grouped_choice_records(records)
    selection_required = bool(args.host and len(records) > 1)
    summary = {
        "status": status,
        "server_list_exists": True,
        "server_list_path": str(config_path) if args.show_sensitive else REDACTED,
        "server_count": len(servers),
        "enabled_ssh_count": len(enabled_servers),
        "selection_required": selection_required,
        "servers": records,
        "groups": grouped,
        "next_steps": (
            ["Reply with the server id or name, or specify user@host:port, before any remote access."]
            if selection_required
            else ["Reply with the server id or name to select a target before any remote access."]
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
    print(f"selection_required: {str(selection_required).lower()}")
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
                print(f"  username: {record.get('username', '')}")
                print(f"  port: {record.get('port', '')}")
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


def parse_functions_value(value: str) -> list[str]:
    if not value.strip():
        return []
    return [item.strip() for item in re.split(r"[;,]", value) if item.strip()]


def format_functions_value(value: Any) -> str:
    if not isinstance(value, list):
        return ""
    return "; ".join(str(item).strip() for item in value if str(item).strip())


def prompt_functions(default: list[str] | None = None) -> list[str]:
    raw = prompt_value("functions", format_functions_value(default or []))
    return parse_functions_value(raw)


def prompt_choice(label: str, choices: list[str], default: str | None = None) -> str:
    if default is not None and default not in choices:
        raise RemoteSshError(f"Invalid default choice {default!r} for {label}.")
    suffix = "/".join(choices)
    while True:
        default_text = f"; default {default}" if default is not None else ""
        value = input(f"{label} [{suffix}{default_text}]: ").strip().casefold()
        if not value:
            if default is not None:
                return default
            print(f"Enter one of: {', '.join(choices)}.", file=sys.stderr)
            continue
        matches = [choice for choice in choices if choice.startswith(value)]
        if len(matches) == 1:
            return matches[0]
        print(f"Enter one of: {', '.join(choices)}.", file=sys.stderr)


def prompt_passphrase() -> str:
    mode = prompt_choice("key_passphrase", ["empty", "custom"], "empty")
    if mode == "empty":
        return ""
    first = getpass.getpass("passphrase: ")
    second = getpass.getpass("confirm_passphrase: ")
    if first != second:
        raise RemoteSshError("Passphrases did not match.")
    return first


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


def validate_updated_server_record(config: dict[str, Any], record: dict[str, Any], original_id: str) -> None:
    candidate = Server(record)
    errors = validate_server_shape(config, candidate, require_key_file=False)
    if errors:
        raise RemoteSshError("Invalid server record: " + "; ".join(errors))
    name = candidate.name.casefold()
    for server in get_servers(config):
        if server.id == original_id:
            continue
        existing = {server.id.casefold(), server.name.casefold(), server.legacy_server_id.casefold()}
        if name in existing:
            raise RemoteSshError(f"Server name already exists: {candidate.name}")


def print_existing_host_logins(existing: list[Server]) -> None:
    print(f"matching_host_count: {len(existing)}")
    for server in existing:
        print(f"existing: {server.label}")
        print(f"  name: {server.name}")
        print(f"  username: {server.username}")
        print(f"  port: {server.port}")
        print(f"  enabled: {server.enabled}")


def print_server_record_summary(config: dict[str, Any], record: dict[str, Any], show_sensitive: bool = False) -> None:
    server = Server(record)
    print("server_record_summary:")
    print(f"  id: {server.id}")
    print(f"  name: {server.name}")
    print(f"  enabled: {str(server.enabled).lower()}")
    if show_sensitive:
        print(f"  target: {user_host(server)}:{server.port}")
        print(f"  key_path: {expand_key_path(config, server)}")
        print(f"  workdir: {server.workdir}")
    else:
        print(f"  target: {REDACTED}")
        print(f"  key_path: {REDACTED}")
        print(f"  workdir: {REDACTED}")
    category = str(record.get("category", "")).strip()
    functions = format_functions_value(record.get("functions"))
    notes = str(record.get("notes", "")).strip()
    print(f"  category: {category or 'unspecified'}")
    print(f"  functions: {functions or 'unspecified'}")
    print(f"  notes: {notes or 'unspecified'}")


def print_public_key_guidance(config: dict[str, Any], server: Server, public_key_path: Path, show_sensitive: bool = False) -> None:
    if show_sensitive:
        print(f"public_key_path: {public_key_path}")
        if public_key_path.exists():
            print(f"public_key_content: {public_key_path.read_text(encoding='utf-8').strip()}")
    else:
        print(f"public_key_path: {REDACTED}")
    print("manual_login_required: true")
    print("next: log in to the remote account once using a password, console, existing jump host, or administrator path.")
    print("next: append the public key content to the remote account's ~/.ssh/authorized_keys.")
    print("next: run setup-key, check, then exec -- echo ok after the remote authorized_keys update.")


def generate_ssh_key_for_server(
    config: dict[str, Any],
    settings: dict[str, Any],
    server: Server,
    show_sensitive: bool = False,
) -> bool:
    key_path = expand_key_path(config, server)
    public_key_path = expand_public_key_path(config, server)
    if key_path.exists():
        raise RemoteSshError("Refusing to overwrite an existing private key.")
    print(f"key_generation_target: {key_path}")
    if not prompt_bool("generate SSH key at this path", False):
        print("key_generation: cancelled")
        return False
    passphrase = prompt_passphrase()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    command = [
        ssh_keygen_client(settings),
        "-t",
        "ed25519",
        "-f",
        str(key_path),
        "-C",
        f"{server.username}@{server.host}",
        "-N",
        passphrase,
    ]
    try:
        result = subprocess.run(command, text=True, capture_output=True, check=False)
    except FileNotFoundError as exc:
        raise RemoteSshError(f"SSH key generator '{command[0]}' was not found on PATH.") from exc
    if result.returncode != 0:
        summary = summarize_error(result.stderr)
        if summary:
            print(redact_text(config, summary), file=sys.stderr)
        raise RemoteSshError("SSH key generation failed.")
    print("key_generation: created")
    print_public_key_guidance(config, server, public_key_path, show_sensitive)
    return True


def resolve_missing_private_key_for_record(
    config: dict[str, Any],
    settings: dict[str, Any],
    record: dict[str, Any],
    show_sensitive: bool = False,
) -> int | None:
    server = Server(record)
    key_path = expand_key_path(config, server)
    if key_path.exists() or not server.enabled:
        return None
    print("key_status: missing_private_key")
    print(f"private_key: {key_path if show_sensitive else REDACTED}")
    action = prompt_choice("missing_key_action", ["generate", "disable", "cancel"], "disable")
    if action == "generate":
        if not generate_ssh_key_for_server(config, settings, server, show_sensitive):
            return 3
        return None
    if action == "disable":
        record["enabled"] = False
        print("key_generation: skipped")
        print("server_enabled: false")
        print("next: generate or place the private key, then update and enable this server.")
        return None
    print("configuration_status: cancelled")
    return 3


def is_passwordless_auth_failure(message: str) -> bool:
    folded = message.casefold()
    return any(
        marker in folded
        for marker in [
            "permission denied",
            "publickey",
            "key file not found",
            "authentication failed",
            "authentications that can continue",
            "no supported authentication methods",
        ]
    )


def print_key_only_repair_guidance(message: str = "") -> None:
    if not message or not is_passwordless_auth_failure(message):
        return
    print("next: ask the user whether to run configure-key --interactive for key-only passwordless repair.")
    print("next: configure-key only changes key_name and validation caches after successful verification.")


def update_server_key_repair_cache(
    config_path: Path,
    config: dict[str, Any],
    server: Server,
    key_name: str,
    validation: dict[str, Any],
    workspace_check: dict[str, Any],
) -> None:
    selector = server.id.casefold()
    for record in config.get("servers", []):
        if isinstance(record, dict) and str(record.get("id", "")).casefold() == selector:
            record["key_name"] = key_name
            record["validation"] = validation
            record["workspace_check"] = workspace_check
            write_json_atomic(config_path, config)
            return
    raise RemoteSshError(f"Cannot update key repair cache; server not found: {server.label}")


def cmd_configure_key(args: argparse.Namespace) -> int:
    if not args.interactive:
        raise RemoteSshError("configure-key currently requires --interactive.")
    config, settings, config_path = load_config_for_args(args)
    current = select_server(config, args.server, getattr(args, "allow_disabled", False))
    candidate_record = dict(current.raw)
    candidate_record["key_name"] = prompt_value("key_name", current.key_name, required=True)
    candidate_record.pop("validation", None)
    candidate_record.pop("workspace_check", None)
    candidate_record.pop("software_scan", None)
    candidate = Server(candidate_record)

    errors = validate_server_shape(config, candidate, require_key_file=False)
    if errors:
        for error in errors:
            print(f"check failed: {error}", file=sys.stderr)
        raise RemoteSshError("Server local precheck failed.")

    key_path = expand_key_path(config, candidate)
    public_key_path = expand_public_key_path(config, candidate)
    print(f"server: {current.label}")
    print(f"key_path: {key_path if getattr(args, 'show_sensitive', False) else REDACTED}")
    if not key_path.exists():
        print("key_status: missing_private_key")
        action = prompt_choice("missing_key_action", ["generate", "cancel"], "generate")
        if action == "cancel":
            print("configuration_status: cancelled")
            return 3
        if not generate_ssh_key_for_server(config, settings, candidate, getattr(args, "show_sensitive", False)):
            print("configuration_status: cancelled")
            return 3
    elif not public_key_path.exists():
        print("key_status: missing_public_key")
        print_public_key_guidance(config, candidate, public_key_path, getattr(args, "show_sensitive", False))
        print("configuration_status: cancelled")
        print("next: create the matching public key file before passwordless verification.")
        return 2
    else:
        print("key_status: local_key_pair_present")
        print_public_key_guidance(config, candidate, public_key_path, getattr(args, "show_sensitive", False))

    if not prompt_bool("remote_public_key_installed", False):
        print("configuration_status: cancelled")
        print("next: install the public key on the remote account, then rerun configure-key.")
        return 3

    timeout = args.timeout or default_timeout(settings)
    candidate, project = server_for_args_project(args, settings, candidate)
    result = run_workspace_probe(config, settings, candidate, timeout)
    print(f"server: {current.label}")
    print_project_context(project)
    print(f"workdir: {REDACTED}")
    if result.returncode != 0:
        summary = summarize_error(result.stderr) or f"SSH command failed with exit code {result.returncode}."
        if summary:
            print(redact_text(config, summary), file=sys.stderr)
        print("key_only_repair: verification_failed")
        print_key_only_repair_guidance(summary)
        return result.returncode

    backup = backup_file(config_path)
    timestamp = utc_timestamp()
    update_server_key_repair_cache(
        config_path,
        config,
        current,
        candidate.key_name,
        {
            "status": "verified",
            "method": "ssh_workspace",
            "verified_at": timestamp,
            "last_error": None,
        },
        {
            "status": "ok",
            "checked_at": timestamp,
            "message": f"The working directory can be accessed: {candidate.workdir}",
        },
    )
    print("key_only_repair: verified")
    if backup:
        print(f"backup: {backup}")

    refreshed = load_config(config_path)
    refreshed_server = select_server(refreshed, current.id, allow_disabled=False)
    scan_args = argparse.Namespace(timeout=timeout, accept_new_host_key=False)
    scan_result, inventory = run_software_scan(refreshed, settings, refreshed_server, scan_args)
    if scan_result.returncode != 0:
        summary = summarize_error(scan_result.stderr) or f"SSH command failed with exit code {scan_result.returncode}."
        snapshot = failed_software_snapshot(settings, redact_text(refreshed, summary))
        cache_software_snapshot(config_path, refreshed, refreshed_server, snapshot)
        print(redact_text(refreshed, summary), file=sys.stderr)
        print_software_snapshot(refreshed_server, snapshot)
        return scan_result.returncode
    snapshot = software_snapshot_from_inventory(settings, inventory, scan_result.stdout)
    cache_software_snapshot(config_path, refreshed, refreshed_server, snapshot)
    return print_software_snapshot(refreshed_server, snapshot)


def cmd_add_server(args: argparse.Namespace) -> int:
    if not args.interactive:
        raise RemoteSshError("add-server currently requires --interactive.")
    settings = load_settings(args.settings)
    config_path = resolve_server_list_path(settings, args.config)
    config = load_config(config_path) if config_path.exists() else empty_config()

    default_id = next_server_id(config)
    print("section: connection")
    server_id = prompt_value("id", default_id, required=True)
    name = prompt_value("name", server_id, required=True)
    host = prompt_value("host", required=True)
    port = parse_port_value(prompt_value("port", "22", required=True))
    username = prompt_value("username", required=True)
    existing_logins = same_host_servers(config, host)
    duplicate_logins = [
        server
        for server in existing_logins
        if server.username.casefold() == username.casefold() and server.port == port
    ]
    if existing_logins:
        print_existing_host_logins(existing_logins)
    if duplicate_logins:
        print("duplicate_login: true")
        if not prompt_bool("add another entry with the same host, username, and port", False):
            print("add_server_status: cancelled")
            print("next: choose the existing server id/name or enter a different username or port.")
            return 3
    elif existing_logins:
        if not prompt_bool("add another login for this host", True):
            print("add_server_status: cancelled")
            print("next: choose an existing server id/name or enter a different host.")
            return 3
    print("section: key-workdir")
    key_name = prompt_value("key_name", required=True)
    workdir = prompt_value("workdir", default_workdir(settings), required=True)
    enabled = prompt_bool("enabled", True)
    print("section: metadata")
    category = prompt_value("category", "")
    functions = prompt_functions()
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
    if category:
        record["category"] = category
    if functions:
        record["functions"] = functions
    if notes:
        record["notes"] = notes

    print_server_record_summary(config, record, getattr(args, "show_sensitive", False))
    if not prompt_bool("save_server_record", True):
        print("add_server_status: cancelled")
        return 3

    key_resolution = resolve_missing_private_key_for_record(config, settings, record, getattr(args, "show_sensitive", False))
    if key_resolution is not None:
        return key_resolution

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


UPDATE_FIELDS = ["name", "host", "port", "username", "key_name", "workdir", "enabled", "category", "functions", "notes"]
CONNECTION_FIELDS = {"host", "port", "username", "key_name", "workdir"}


def print_update_menu() -> None:
    print("field_menu:")
    print("  show: display current candidate values")
    print("  all: edit all fields")
    print("  done: validate and save")
    print("  cancel: leave server list unchanged")
    print(f"  fields: {', '.join(UPDATE_FIELDS)}")


def prompt_update_action() -> str:
    allowed = {"show", "all", "done", "cancel", *UPDATE_FIELDS}
    while True:
        value = input("update_field [show/all/done/cancel/name/host/port/username/key_name/workdir/enabled/category/functions/notes]: ").strip().casefold()
        if not value:
            print_update_menu()
            continue
        if value in allowed:
            return value
        print(f"Enter one of: {', '.join(['show', 'all', 'done', 'cancel', *UPDATE_FIELDS])}.", file=sys.stderr)


def edit_server_field(record: dict[str, Any], field: str) -> None:
    if field == "name":
        record["name"] = prompt_value("name", str(record.get("name", "")), required=True)
    elif field == "host":
        record["host"] = prompt_value("host", str(record.get("host", "")), required=True)
    elif field == "port":
        record["port"] = parse_port_value(prompt_value("port", str(record.get("port", "22")), required=True))
    elif field == "username":
        record["username"] = prompt_value("username", str(record.get("username", "")), required=True)
    elif field == "key_name":
        record["key_name"] = prompt_value("key_name", str(record.get("key_name", "")), required=True)
    elif field == "workdir":
        record["workdir"] = prompt_value("workdir", str(record.get("workdir", "")), required=True)
    elif field == "enabled":
        record["enabled"] = prompt_bool("enabled", bool(record.get("enabled", True)))
    elif field == "category":
        value = prompt_value("category", str(record.get("category", "")))
        if value:
            record["category"] = value
        else:
            record.pop("category", None)
    elif field == "functions":
        functions = prompt_functions(record.get("functions") if isinstance(record.get("functions"), list) else [])
        if functions:
            record["functions"] = functions
        else:
            record.pop("functions", None)
    elif field == "notes":
        value = prompt_value("notes", str(record.get("notes", "")))
        if value:
            record["notes"] = value
        else:
            record.pop("notes", None)
    else:
        raise RemoteSshError(f"Unsupported update field: {field}")


def edit_all_server_fields(record: dict[str, Any]) -> None:
    for field in UPDATE_FIELDS:
        edit_server_field(record, field)


def cmd_update_server(args: argparse.Namespace) -> int:
    if not args.interactive:
        raise RemoteSshError("update-server currently requires --interactive.")
    config, settings, config_path = load_config_for_args(args)
    current = select_server(config, args.server, allow_disabled=True)
    record = dict(current.raw)

    print_update_menu()
    while True:
        action = prompt_update_action()
        if action == "show":
            print_server_record_summary(config, record, getattr(args, "show_sensitive", False))
            continue
        if action == "cancel":
            print("configuration_status: cancelled")
            return 3
        if action == "done":
            break
        if action == "all":
            edit_all_server_fields(record)
            break
        edit_server_field(record, action)

    print_server_record_summary(config, record, getattr(args, "show_sensitive", False))
    if not prompt_bool("save_server_record", True):
        print("configuration_status: cancelled")
        return 3

    connection_changed = any(record.get(field) != current.raw.get(field) for field in CONNECTION_FIELDS)
    enabled_changed_to_true = bool(record.get("enabled", True)) and not current.enabled

    if connection_changed:
        record.pop("validation", None)
        record.pop("workspace_check", None)
        record.pop("software_scan", None)

    should_validate_key_material = bool(record.get("enabled", True)) and (connection_changed or enabled_changed_to_true)
    if should_validate_key_material:
        key_resolution = resolve_missing_private_key_for_record(config, settings, record, getattr(args, "show_sensitive", False))
        if key_resolution is not None:
            return key_resolution

    validate_updated_server_record(config, record, current.id)
    for index, server in enumerate(config.get("servers", [])):
        if isinstance(server, dict) and str(server.get("id", "")) == current.id:
            config["servers"][index] = record
            break
    else:
        raise RemoteSshError(f"Server disappeared before update: {current.label}")

    backup = backup_file(config_path)
    write_json_atomic(config_path, config)
    refreshed = load_config(config_path)
    server = select_server(refreshed, current.id, allow_disabled=True)
    if not server.enabled:
        snapshot = {
            "status": "skipped",
            "scanned_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
            "catalog_version": software_catalog_version(settings),
            "tools": {},
            "fpga_devices": [],
            "raw_summary": "",
            "last_error": "Server was updated disabled, so software scan was skipped.",
        }
        cache_software_snapshot(config_path, refreshed, server, snapshot)
        print(f"updated: {server.id}")
        print("software_scan_status: skipped")
        if backup:
            print(f"backup: {backup}")
        return 0

    if not (connection_changed or enabled_changed_to_true):
        print(f"updated: {server.id}")
        if backup:
            print(f"backup: {backup}")
        return 0

    scan_args = argparse.Namespace(
        settings=args.settings,
        config=args.config,
        server=server.id,
        allow_disabled=False,
        accept_new_host_key=False,
        timeout=None,
    )
    scan_rc = cmd_scan_software(scan_args)
    if scan_rc != 0:
        print(f"server_record_saved: {server.id}")
        if backup:
            print(f"backup: {backup}")
        print("software_scan_required: failed")
        return scan_rc
    print(f"updated: {server.id}")
    if backup:
        print(f"backup: {backup}")
    return 0


def print_manual_configuration(config_path: Path) -> None:
    print("configuration_mode: manual")
    print(f"manual: create or edit the server list at {config_path}")
    print("manual: run init-config if the file does not exist.")
    print("manual: add or update a server entry with host, port, username, key_name, workdir, and enabled.")
    print("manual: install the public key by logging in to the remote account once and updating ~/.ssh/authorized_keys.")
    print("manual: run setup-key, check, then scan-software after key material is ready.")


def cmd_configure(args: argparse.Namespace) -> int:
    if not args.interactive:
        raise RemoteSshError("configure currently requires --interactive.")
    settings = load_settings(args.settings)
    config_path = resolve_server_list_path(settings, args.config)
    summary, _ = discover_summary(settings, config_path)
    print(f"status: {summary['status']}")
    mode = prompt_choice("configuration_mode", ["script", "manual", "cancel"])
    if mode == "manual":
        print_manual_configuration(config_path)
        return 0
    print(f"configuration_mode: {mode}")
    if mode == "cancel":
        print("configuration_status: cancelled")
        return 3

    if not config_path.exists():
        write_json_atomic(config_path, empty_config())
        print(f"created: {config_path}")
    config = load_config(config_path)
    if args.server:
        return cmd_update_server(
            argparse.Namespace(
                settings=args.settings,
                config=args.config,
                server=args.server,
                interactive=True,
                show_sensitive=args.show_sensitive,
            )
        )

    servers = get_servers(config)
    if not servers:
        return cmd_add_server(
            argparse.Namespace(
                settings=args.settings,
                config=args.config,
                interactive=True,
                show_sensitive=args.show_sensitive,
            )
        )

    print("configured_servers:")
    for index, server in enumerate(servers, start=1):
        print(f"[{index}] {server.label}: {server.name} enabled={str(server.enabled).lower()}")
    action = prompt_choice("configuration_action", ["add", "update", "cancel"])
    if action == "cancel":
        print("configuration_status: cancelled")
        return 3
    if action == "add":
        return cmd_add_server(
            argparse.Namespace(
                settings=args.settings,
                config=args.config,
                interactive=True,
                show_sensitive=args.show_sensitive,
            )
        )

    selector = prompt_value("server", required=True)
    if selector.isdigit():
        index = int(selector)
        if index < 1 or index > len(servers):
            raise RemoteSshError(f"Server selection index out of range: {selector}")
        selector = servers[index - 1].id
    return cmd_update_server(
        argparse.Namespace(
            settings=args.settings,
            config=args.config,
            server=selector,
            interactive=True,
            show_sensitive=args.show_sensitive,
        )
    )


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
    print("manual_login_required: true")
    print("next: keep the private key local and install only the public key in the remote account's ~/.ssh/authorized_keys.")
    print("next: log in to the remote account once using a password, console, existing jump host, or administrator path to append the public key.")
    print("next: run check, then exec with a short command such as echo ok to verify passwordless SSH.")
    if not private_exists:
        print("next: create or place the private key outside this helper, then update key_name or default_key_dir.")
    if private_exists and not public_exists:
        print("next: create the matching public key file before sharing the key material with the remote account.")
    return 0 if private_exists and public_exists else 2


def prepare_server_for_remote(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], Server]:
    config, settings, server, _ = prepare_server_for_remote_with_project(args)
    return config, settings, server


def prepare_server_for_remote_with_project(args: argparse.Namespace) -> tuple[dict[str, Any], dict[str, Any], Server, ProjectContext | None]:
    config, settings, _ = load_config_for_args(args)
    server = select_server(config, args.server, getattr(args, "allow_disabled", False))
    errors = validate_server(config, server)
    if errors:
        for error in errors:
            print(f"check failed: {error}", file=sys.stderr)
        raise RemoteSshError("Server local precheck failed.")
    effective_server, project = server_for_args_project(args, settings, server)
    return config, settings, effective_server, project


def utc_timestamp() -> str:
    return dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def update_server_status_cache(
    config_path: Path,
    config: dict[str, Any],
    server: Server,
    validation: dict[str, Any],
    workspace_check: dict[str, Any],
) -> None:
    selector = server.id.casefold()
    for record in config.get("servers", []):
        if isinstance(record, dict) and str(record.get("id", "")).casefold() == selector:
            record["validation"] = validation
            record["workspace_check"] = workspace_check
            write_json_atomic(config_path, config)
            return
    raise RemoteSshError(f"Cannot update validation cache; server not found: {server.label}")


def update_project_workspace_cache(settings: dict[str, Any], project: ProjectContext, server: Server, workspace_check: dict[str, Any]) -> None:
    if project.config_path is None:
        return
    data = load_project_config(project.config_path, settings, server)
    servers = data.setdefault("servers", {})
    entry = servers.setdefault(server.id, {})
    if not isinstance(entry, dict):
        entry = {}
        servers[server.id] = entry
    entry.setdefault("remote_workdir", project.effective_workdir)
    entry["workspace_check"] = workspace_check
    write_json_atomic(project.config_path, data)


def run_workspace_probe(config: dict[str, Any], settings: dict[str, Any], server: Server, timeout: int) -> subprocess.CompletedProcess[str]:
    return run_ssh(build_ssh_args(config, settings, server, "pwd && test -d . && test -x ."), timeout)


def print_workspace_probe_result(config: dict[str, Any], server: Server, result: subprocess.CompletedProcess[str]) -> int:
    print(f"server: {server.label}")
    print(f"workdir: {REDACTED}")
    if result.returncode != 0:
        summary = summarize_error(result.stderr)
        if summary:
            print(redact_text(config, summary), file=sys.stderr)
        print("status: failed")
        return result.returncode
    print("status: ok")
    return 0


def project_config_write_path(args: argparse.Namespace) -> Path:
    override = getattr(args, "project_config", None)
    if override is not None:
        return Path(override).resolve()
    return (Path.cwd() / ".erie-remote-ssh" / "project.local.json").resolve()


def remote_dir_check_command(path: str) -> str:
    quoted = shlex.quote(path)
    return (
        f"target={quoted}; "
        "parent=$(dirname -- \"$target\"); "
        "exists=false; is_dir=false; is_empty=true; parent_exists=false; parent_writable=false; "
        "if [ -e \"$target\" ]; then exists=true; fi; "
        "if [ -d \"$target\" ]; then is_dir=true; fi; "
        "if [ -d \"$target\" ] && find \"$target\" -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null | grep -q .; then is_empty=false; fi; "
        "if [ -d \"$parent\" ]; then parent_exists=true; fi; "
        "if [ -w \"$parent\" ]; then parent_writable=true; fi; "
        "printf '{\"exists\":%s,\"is_dir\":%s,\"is_empty\":%s,\"parent_exists\":%s,\"parent_writable\":%s}\\n' "
        "\"$exists\" \"$is_dir\" \"$is_empty\" \"$parent_exists\" \"$parent_writable\""
    )


def run_remote_dir_check(config: dict[str, Any], settings: dict[str, Any], server: Server, remote_workdir: str, timeout: int) -> dict[str, Any]:
    no_cd_server = server_with_workdir(server, "")
    result = run_ssh(build_ssh_args(config, settings, no_cd_server, remote_dir_check_command(remote_workdir)), timeout)
    if result.returncode != 0:
        summary = summarize_error(result.stderr) or f"SSH command failed with exit code {result.returncode}."
        raise RemoteSshError(f"Remote workdir check failed: {redact_text(config, summary)}")
    try:
        data = json.loads(result.stdout.strip().splitlines()[-1])
    except (IndexError, json.JSONDecodeError) as exc:
        raise RemoteSshError("Remote workdir check did not return valid JSON.") from exc
    if not isinstance(data, dict):
        raise RemoteSshError("Remote workdir check result must be an object.")
    return data


def project_remote_workdir_check_status(check: dict[str, Any]) -> str:
    if check.get("exists"):
        return "collision"
    if check.get("parent_exists") and check.get("parent_writable"):
        return "available"
    return "unavailable"


def project_config_data(project_id: str, server: Server, remote_workdir: str, check: dict[str, Any], resolution: str | None = None) -> dict[str, Any]:
    check_record: dict[str, Any] = {
        "status": project_remote_workdir_check_status(check),
        "checked_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "exists": bool(check.get("exists")),
        "is_dir": bool(check.get("is_dir")),
        "is_empty": bool(check.get("is_empty")),
        "parent_exists": bool(check.get("parent_exists")),
        "parent_writable": bool(check.get("parent_writable")),
    }
    if resolution:
        check_record["collision_resolution"] = resolution
    return {
        "version": 1,
        "project_id": project_id,
        "default_server": server.id,
        "remote_workdir": remote_workdir,
        "servers": {
            server.id: {
                "remote_workdir": remote_workdir,
                "remote_workdir_check": check_record,
            }
        },
    }


def candidate_workdir_from_project(settings: dict[str, Any], project_id: str, override: str | None) -> str:
    if override:
        value = override.strip()
        if not (value.startswith("~/") or value.startswith("/")):
            value = f"~/workspace/{validate_project_id(value)}"
        return validate_remote_workdir(value)
    return render_project_workdir(settings, project_id)


def local_project_workdir_duplicate(settings: dict[str, Any], server: Server, remote_workdir: str, exclude: Path) -> Path | None:
    search_root = Path.cwd().resolve().parent
    config_dir_name = project_config_dir(settings)
    config_names = project_config_names(settings)
    for project_dir in search_root.rglob(config_dir_name):
        try:
            if not project_dir.is_dir() or project_dir.name != config_dir_name:
                continue
        except OSError:
            continue
        for name in config_names:
            candidate = project_dir / name
            try:
                if not candidate.is_file():
                    continue
            except OSError:
                continue
            try:
                if candidate.resolve() == exclude.resolve():
                    continue
                data = load_project_config(candidate, settings, server)
                entry = project_server_entry(data, server)
                declared = str(entry.get("remote_workdir") or data.get("remote_workdir") or "").strip()
                if declared == remote_workdir:
                    return candidate.resolve()
            except (OSError, RemoteSshError):
                continue
    return None


def cmd_project_init(args: argparse.Namespace) -> int:
    if not args.interactive:
        raise RemoteSshError("project-init currently requires --interactive.")
    config, settings, _ = load_config_for_args(args)
    server = select_server(config, args.server, getattr(args, "allow_disabled", False))
    errors = validate_server(config, server)
    if errors:
        for error in errors:
            print(f"check failed: {error}", file=sys.stderr)
        raise RemoteSshError("Server local precheck failed.")
    if not args.project:
        raise RemoteSshError("project-init requires --project <id>.")
    project_id = validate_project_id(str(args.project))
    timeout = args.timeout or default_timeout(settings)
    remote_workdir = candidate_workdir_from_project(settings, project_id, args.remote_workdir)
    resolution: str | None = None
    output_path = project_config_write_path(args)
    while True:
        check = run_remote_dir_check(config, settings, server, remote_workdir, timeout)
        duplicate = local_project_workdir_duplicate(settings, server, remote_workdir, output_path)
        if duplicate is not None:
            check = dict(check)
            check["exists"] = True
        status = project_remote_workdir_check_status(check)
        print(f"project: {project_id}")
        print(f"remote_workdir: {REDACTED}")
        print(f"remote_workdir_status: {status}")
        if status == "available":
            break
        if status == "unavailable":
            raise RemoteSshError("Remote workdir parent is missing or not writable.")
        print("remote_workdir_collision: true")
        if duplicate is not None:
            print("local_project_duplicate: true")
        action = prompt_choice("collision_action", ["overwrite", "rename", "timestamp", "cancel"], "cancel")
        if action == "cancel":
            print("project_init_status: cancelled")
            return 3
        if action == "overwrite":
            resolution = "overwrite_existing_directory"
            break
        if action == "rename":
            renamed = prompt_value("remote_workdir", required=True)
            remote_workdir = candidate_workdir_from_project(settings, project_id, renamed)
            resolution = "renamed"
            continue
        if action == "timestamp":
            suffix = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
            remote_workdir = render_project_workdir(settings, f"{project_id}-{suffix}")
            resolution = "timestamp_suffix"
            print("collision_resolution: timestamp_suffix")
            continue

    write_json_atomic(output_path, project_config_data(project_id, server, remote_workdir, check, resolution))
    print(f"project_config: {output_path}")
    print("project_init_status: saved")
    return 0


def cmd_project_show(args: argparse.Namespace) -> int:
    config, settings, _ = load_config_for_args(args)
    server = select_server(config, args.server, getattr(args, "allow_disabled", False))
    project = effective_project_context(args, settings, server)
    print(f"server: {server.label}")
    if project is None:
        print("project: none")
        print("workdir_source: server_default")
        print(f"effective_workdir: {server.workdir if args.show_sensitive else REDACTED}")
        return 0
    print(f"project: {project.project_id}")
    print("workdir_source: project")
    print(f"project_config: {project.config_path if args.show_sensitive and project.config_path else REDACTED}")
    print(f"effective_workdir: {project.effective_workdir if args.show_sensitive else REDACTED}")
    entry = project_server_entry(project.data, server)
    check = entry.get("remote_workdir_check") if isinstance(entry, dict) else None
    print(f"remote_workdir_check: {check.get('status', 'missing') if isinstance(check, dict) else 'missing'}")
    return 0


def cmd_workspace_check(args: argparse.Namespace) -> int:
    config, settings, config_path = load_config_for_args(args)
    server = select_server(config, args.server, getattr(args, "allow_disabled", False))
    errors = validate_server(config, server)
    if errors:
        for error in errors:
            print(f"check failed: {error}", file=sys.stderr)
        raise RemoteSshError("Server local precheck failed.")

    backup = backup_file(config_path)
    timeout = args.timeout or default_timeout(settings)
    effective_server, project = server_for_args_project(args, settings, server)
    result = run_workspace_probe(config, settings, effective_server, timeout)
    print(f"server: {server.label}")
    print_project_context(project)
    print(f"workdir: {REDACTED}")
    if result.returncode != 0:
        summary = summarize_error(result.stderr)
        if summary:
            print(redact_text(config, summary), file=sys.stderr)
        timestamp = utc_timestamp()
        message = redact_text(config, summary or f"SSH command failed with exit code {result.returncode}.")
        validation = {
            "status": "failed",
            "method": "ssh_workspace",
            "verified_at": None,
            "last_error": message,
        }
        workspace_check = {
            "status": "failed",
            "checked_at": timestamp,
            "message": message,
        }
        if project is None:
            update_server_status_cache(config_path, config, server, validation, workspace_check)
        else:
            update_project_workspace_cache(settings, project, server, workspace_check)
        next_action = (
            "Create the project remote workdir manually or with a reviewed request before retrying workspace-check."
            if project is not None
            else "Repair SSH authentication or the remote workdir, then retry workspace-check."
        )
        print_contract_fields(
            server,
            status="failed",
            message=message,
            next_action=next_action,
            workdir_status="failed",
            software_cache_status=server.software_cache_status,
        )
        print_key_only_repair_guidance(summary or "")
        if backup:
            print(f"backup: {backup}")
        return result.returncode
    timestamp = utc_timestamp()
    validation = {
        "status": "verified",
        "method": "ssh_workspace",
        "verified_at": timestamp,
        "last_error": None,
    }
    workspace_check = {
        "status": "ok",
        "checked_at": timestamp,
        "message": f"The working directory can be accessed: {effective_server.workdir}",
    }
    if project is None:
        update_server_status_cache(config_path, config, server, validation, workspace_check)
    else:
        selector = server.id.casefold()
        for record in config.get("servers", []):
            if isinstance(record, dict) and str(record.get("id", "")).casefold() == selector:
                record["validation"] = validation
                write_json_atomic(config_path, config)
                break
        update_project_workspace_cache(settings, project, server, workspace_check)
    print_contract_fields(
        server,
        status="ok",
        message=workspace_check["message"],
        next_action="Use file-list, request-command, exec, inventory, or project-init dependent workflows as needed.",
        workdir_status="ok",
        software_cache_status="refresh_pending",
    )
    if backup:
        print(f"backup: {backup}")

    refreshed = load_config(config_path)
    refreshed_server = select_server(refreshed, server.id, allow_disabled=False)
    if project is not None:
        refreshed_server = server_with_workdir(refreshed_server, project.effective_workdir)
    scan_args = argparse.Namespace(
        timeout=timeout,
        accept_new_host_key=False,
    )
    scan_result, inventory = run_software_scan(refreshed, settings, refreshed_server, scan_args)
    if scan_result.returncode != 0:
        summary = summarize_error(scan_result.stderr) or f"SSH command failed with exit code {scan_result.returncode}."
        snapshot = failed_software_snapshot(settings, redact_text(refreshed, summary))
        cache_software_snapshot(config_path, refreshed, refreshed_server, snapshot)
        print(redact_text(refreshed, summary), file=sys.stderr)
        print_software_snapshot(refreshed_server, snapshot)
        return scan_result.returncode
    snapshot = software_snapshot_from_inventory(settings, inventory, scan_result.stdout)
    cache_software_snapshot(config_path, refreshed, refreshed_server, snapshot)
    return print_software_snapshot(refreshed_server, snapshot)


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
    config, settings, server, project = prepare_server_for_remote_with_project(args)
    local_source, upload_root, local_relative_path = resolve_local_upload_path(settings, args.local, must_exist=True)
    sensitive_reasons = sensitive_local_upload_reasons(local_source)
    reason = args.reason or ""
    require_sensitive_upload_confirmation(sensitive_reasons, bool(args.confirm_sensitive_local_upload), reason)
    remote_path = remote_relative_path(args.remote, allow_dot=False)
    risks = ["modifies remote workdir content"]
    risks.extend(f"sensitive local upload: {item}" for item in sensitive_reasons)
    path = create_request(
        settings,
        "upload",
        server,
        {
            "local_upload_root": upload_root.as_posix(),
            "local_relative_path": local_relative_path,
            "remote_path": remote_path,
            "recursive": local_source.is_dir(),
        },
        reason=reason,
        risk_summary=risks,
        project=project,
    )
    print_request_created(path, load_request(path), server, project)
    return 0


def cmd_request_mkdir(args: argparse.Namespace) -> int:
    _, settings, server, project = prepare_server_for_remote_with_project(args)
    remote_path = remote_relative_path(args.path, allow_dot=False)
    path = create_request(settings, "mkdir", server, {"remote_path": remote_path}, reason=args.reason or "", project=project)
    print_request_created(path, load_request(path), server, project)
    return 0


def cmd_request_delete(args: argparse.Namespace) -> int:
    _, settings, server, project = prepare_server_for_remote_with_project(args)
    remote_path = remote_relative_path(args.path, allow_dot=False)
    path = create_request(
        settings,
        "delete",
        server,
        {"remote_path": remote_path, "recursive": bool(args.recursive)},
        reason=args.reason or "",
        project=project,
    )
    print_request_created(path, load_request(path), server, project)
    return 0


def cmd_request_command(args: argparse.Namespace) -> int:
    _, settings, server, project = prepare_server_for_remote_with_project(args)
    command = remote_command_from_tokens(args.remote_command)
    payload: dict[str, Any] = {"command": command}
    if args.detached:
        payload["detached"] = True
    path = create_request(settings, "command", server, payload, reason=args.reason, project=project)
    print_request_created(path, load_request(path), server, project)
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


def job_id(operation: str = "job") -> str:
    now = dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ")
    return f"{now}-{operation}-{uuid.uuid4().hex[:8]}"


def parse_key_value_output(output: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for line in output.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        parsed[key.strip()] = value.strip()
    return parsed


def write_job_manifest(settings: dict[str, Any], manifest: dict[str, Any]) -> Path:
    directory = jobs_local_dir(settings)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{manifest['job_id']}.json"
    write_json_atomic(path, manifest)
    return path


def load_job_manifest(settings: dict[str, Any], job: str) -> dict[str, Any] | None:
    path = jobs_local_dir(settings) / f"{job}.json"
    if not path.exists():
        return None
    data = load_json_file(path, "Job manifest")
    if data.get("version") != 1:
        raise RemoteSshError(f"Unsupported job manifest version: {data.get('version')!r}")
    return data


def select_job_server(
    config: dict[str, Any],
    settings: dict[str, Any],
    requested_server: str | None,
    allow_disabled: bool,
    job: str,
) -> Server:
    manifest = load_job_manifest(settings, job)
    manifest_server = str(manifest.get("server", "")) if manifest else ""
    selector = str(requested_server or manifest_server).strip()
    if not selector:
        raise RemoteSshError("status/tail-log requires --server when no local job manifest exists.")
    if requested_server and manifest_server and str(requested_server) != manifest_server:
        raise RemoteSshError("Requested --server does not match the local job manifest.")
    server = select_server(config, selector, allow_disabled)
    if manifest and manifest.get("effective_workdir"):
        server = server_with_workdir(server, validate_remote_workdir(str(manifest["effective_workdir"])))
    return server


def print_job_start(manifest: dict[str, Any], server: Server, manifest_path: Path | None = None) -> None:
    print_contract_fields(
        server,
        status="started",
        message="Detached remote job started and can be resumed through status/tail-log.",
        next_action="Use status --job <job-id> or tail-log --job <job-id> to resume monitoring.",
        workdir_status="ok",
    )
    print(f"job_id: {manifest['job_id']}")
    print(f"server: {manifest['server']}")
    if manifest.get("project_id"):
        print(f"project: {manifest['project_id']}")
    print(f"remote_job_dir: {manifest['remote_job_dir']}")
    if manifest.get("pid"):
        print(f"pid: {manifest['pid']}")
    if manifest_path is not None:
        print(f"manifest: {manifest_path}")


def build_detached_start_command(settings: dict[str, Any], requested_job_id: str, command: str, reason: str) -> str:
    remote_root = jobs_remote_dir(settings)
    remote_job_dir = f"{remote_root}/{requested_job_id}"
    runner = "\n".join(
        [
            "#!/bin/sh",
            "cd \"$(dirname \"$0\")\" || exit 97",
            "date -u +%Y-%m-%dT%H:%M:%SZ > started_at",
            "sh -c \"$ERIE_REMOTE_SSH_COMMAND\" > stdout.log 2>&1",
            "code=$?",
            "printf '%s\\n' \"$code\" > exit_code",
            "date -u +%Y-%m-%dT%H:%M:%SZ > finished_at",
            "exit \"$code\"",
        ]
    )
    return "\n".join(
        [
            f"job_id={shlex.quote(requested_job_id)}",
            f"job_dir={shlex.quote(remote_job_dir)}",
            "mkdir -p -- \"$job_dir\" || exit $?",
            f"printf '%s\\n' {shlex.quote(command)} > \"$job_dir/command.txt\"",
            f"printf '%s\\n' {shlex.quote(reason)} > \"$job_dir/reason.txt\"",
            f"cat > \"$job_dir/runner.sh\" <<'ERIE_REMOTE_SSH_RUNNER'\n{runner}\nERIE_REMOTE_SSH_RUNNER",
            "chmod +x \"$job_dir/runner.sh\"",
            f"ERIE_REMOTE_SSH_COMMAND={shlex.quote(command)} nohup sh \"$job_dir/runner.sh\" >/dev/null 2>&1 &",
            "pid=$!",
            "printf '%s\\n' \"$pid\" > \"$job_dir/pid\"",
            "printf 'job_id: %s\\n' \"$job_id\"",
            "printf 'remote_job_dir: %s\\n' \"$job_dir\"",
            "printf 'pid: %s\\n' \"$pid\"",
            "printf 'status: started\\n'",
        ]
    )


def start_detached_job(
    config: dict[str, Any],
    settings: dict[str, Any],
    server: Server,
    command: str,
    reason: str,
    timeout: int,
    project: ProjectContext | None = None,
) -> tuple[dict[str, Any], Path]:
    requested_job_id = job_id("job")
    remote_command = build_detached_start_command(settings, requested_job_id, command, reason)
    result = run_ssh(build_ssh_args(config, settings, server, remote_command), timeout)
    if result.returncode != 0:
        summary = summarize_error(result.stderr)
        raise RemoteSshError(redact_text(config, summary) or "Detached job start failed.")
    parsed = parse_key_value_output(result.stdout)
    actual_job_id = parsed.get("job_id") or requested_job_id
    remote_job_dir = parsed.get("remote_job_dir") or f"{jobs_remote_dir(settings)}/{actual_job_id}"
    manifest: dict[str, Any] = {
        "version": 1,
        "job_id": actual_job_id,
        "server": server.id,
        "reason": reason,
        "command": command,
        "created_at": dt.datetime.now(dt.UTC).isoformat().replace("+00:00", "Z"),
        "remote_job_dir": remote_job_dir,
        "stdout_log": f"{remote_job_dir.rstrip('/')}/stdout.log",
        "pid": parsed.get("pid", ""),
        "status": "started",
    }
    if project is not None:
        manifest.update(project_output_record(project))
    manifest_path = write_job_manifest(settings, manifest)
    return manifest, manifest_path


def remote_job_dir_for(settings: dict[str, Any], job: str) -> str:
    clean = remote_relative_path(job, allow_dot=False)
    if "/" in clean:
        raise RemoteSshError("Job id must not contain path separators.")
    return f"{jobs_remote_dir(settings)}/{clean}"


def build_job_status_command(settings: dict[str, Any], job: str) -> str:
    remote_job_dir = remote_job_dir_for(settings, job)
    return "\n".join(
        [
            f"job_dir={shlex.quote(remote_job_dir)}",
            "pid=''",
            "[ -f \"$job_dir/pid\" ] && pid=$(cat \"$job_dir/pid\" 2>/dev/null || true)",
            "exit_code=''",
            "[ -f \"$job_dir/exit_code\" ] && exit_code=$(cat \"$job_dir/exit_code\" 2>/dev/null || true)",
            "if [ -n \"$exit_code\" ]; then",
            "  if [ \"$exit_code\" = 0 ]; then status=succeeded; else status=failed; fi",
            "elif [ -n \"$pid\" ] && kill -0 \"$pid\" 2>/dev/null; then",
            "  status=running",
            "elif [ -d \"$job_dir\" ]; then",
            "  status=unknown",
            "else",
            "  status=not_found",
            "fi",
            "printf 'status: %s\\n' \"$status\"",
            "printf 'pid: %s\\n' \"$pid\"",
            "printf 'exit_code: %s\\n' \"$exit_code\"",
        ]
    )


def build_tail_log_command(settings: dict[str, Any], job: str, lines: int) -> str:
    remote_job_dir = remote_job_dir_for(settings, job)
    return f"tail -n {int(lines)} -- {shlex.quote(remote_job_dir + '/stdout.log')}"


def cmd_run_request(args: argparse.Namespace) -> int:
    if not args.execute:
        raise RemoteSshError("run-request requires --execute.")
    request = load_request(args.request)
    config, settings, _ = load_config_for_args(args)
    server = select_server(config, str(request["server"]), False)
    errors = validate_server(config, server)
    if errors:
        raise RemoteSshError("Server local precheck failed: " + "; ".join(errors))
    request_project_id = request.get("project_id")
    request_project_context: ProjectContext | None = None
    if request_project_id:
        current_project, _ = project_config_for_args(args, settings, server)
        if current_project is not None and str(current_project.get("project_id")) != str(request_project_id):
            raise RemoteSshError("Current project context does not match the request project_id.")
        effective_workdir = validate_remote_workdir(str(request.get("effective_workdir", "")))
        server = server_with_workdir(server, effective_workdir)
        request_project_context = ProjectContext(
            project_id=str(request_project_id),
            config_path=None,
            data={},
            effective_workdir=effective_workdir,
            workdir_source=str(request.get("workdir_source", "project")),
        )

    operation = str(request["operation"])
    payload = request["payload"]
    timeout = args.timeout or default_timeout(settings)
    upload_local_source: Path | None = None
    print_contract_fields(
        server,
        status="pending",
        message=f"Executing reviewed {operation} request.",
        next_action="Wait for the workspace probe and remote execution result below.",
        workdir_status="project_pending" if request_project_id else server.workspace_status,
    )
    print(f"request_id: {request.get('request_id')}")
    print(f"operation: {operation}")
    print(f"server: {server.label}")
    if request_project_id:
        print(f"project: {request_project_id}")
        print("workdir_source: project")
    for risk in request.get("risk_summary", []):
        print(f"risk: {risk}")

    if operation == "upload":
        upload_local_source, _, _ = resolve_local_upload_request_path(settings, payload, must_exist=True)
        sensitive_reasons = sensitive_local_upload_reasons(upload_local_source)
        require_sensitive_upload_confirmation(sensitive_reasons, bool(args.confirm_sensitive_local_upload), str(request.get("reason", "")))

    workspace_result = run_workspace_probe(config, settings, server, timeout)
    workspace_rc = print_workspace_probe_result(config, server, workspace_result)
    if workspace_rc != 0:
        return workspace_rc

    if operation == "upload":
        local_source = upload_local_source
        if local_source is None:
            local_source, _, _ = resolve_local_upload_request_path(settings, payload, must_exist=True)
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
        if payload.get("detached") is True:
            manifest, manifest_path = start_detached_job(
                config,
                settings,
                server,
                command,
                str(request.get("reason", "")),
                timeout,
                project=request_project_context,
            )
            print_job_start(manifest, server, manifest_path)
            return 0
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
        "scan_command_paths() {",
        "  scan_name=\"$1\"",
        "  if [ \"${scan_name#*/}\" != \"$scan_name\" ]; then",
        "    if [ -x \"$scan_name\" ] && [ ! -d \"$scan_name\" ]; then printf '%s\\n' \"$scan_name\"; fi",
        "    return 0",
        "  fi",
        "  old_ifs=$IFS",
        "  IFS=:",
        "  for scan_dir in ${PATH:-}; do",
        "    if [ -z \"$scan_dir\" ]; then scan_dir=.; fi",
        "    scan_candidate=\"$scan_dir/$scan_name\"",
        "    if [ -x \"$scan_candidate\" ] && [ ! -d \"$scan_candidate\" ]; then printf '%s\\n' \"$scan_candidate\"; fi",
        "  done",
        "  IFS=$old_ifs",
        "}",
        "remember_tool_path() {",
        "  tool_key=\"$1\"",
        "  if command -v readlink >/dev/null 2>&1; then",
        "    resolved_tool_key=\"$(readlink -f \"$1\" 2>/dev/null || true)\"",
        "    if [ -n \"$resolved_tool_key\" ]; then tool_key=\"$resolved_tool_key\"; fi",
        "  fi",
        "  case \"${seen_tool_paths}\" in",
        "    *\"|$tool_key|\"*) return 1 ;;",
        "  esac",
        "  seen_tool_paths=\"${seen_tool_paths}|$tool_key|\"",
        "  return 0",
        "}",
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
        lines.append("seen_tool_paths=\"\"")
        commands = [str(command).strip() for command in item.get("commands", [])]
        if commands:
            version_template = str(item.get("version_command") or "{path} --version 2>&1 | head -n 1")
            install_template = str(item.get("install_path_command") or "")
            version_command = render_scan_command(version_template, '"$tool_path"')
            path_scan = str(item.get("path_scan", "first")).strip().casefold()
            if path_scan == "first":
                lines.append("tool_path=\"\"")
                for command in commands:
                    quoted = shell_literal(command)
                    lines.append(f"if [ -z \"$tool_path\" ] && command -v {quoted} >/dev/null 2>&1; then tool_path=\"$(command -v {quoted})\"; fi")
                lines.append("if [ -n \"$tool_path\" ] && remember_tool_path \"$tool_path\"; then")
            else:
                for command in commands:
                    quoted = shell_literal(command)
                    lines.append(f"for tool_path in $(scan_command_paths {quoted}); do")
                    lines.append("  if ! remember_tool_path \"$tool_path\"; then continue; fi")
                    lines.append(f"  tool_version=\"$({version_command} 2>/dev/null | head -n 1 || true)\"")
                    if install_template:
                        install_command = render_scan_command(install_template, '"$tool_path"')
                        lines.append(f"  install_path=\"$({install_command} 2>/dev/null | head -n 1 || true)\"")
                    else:
                        lines.append("  install_path=\"\"")
                    lines.append(f"  emit_software {shell_literal(tool_id)} installed \"$tool_path\" \"$tool_version\" \"$install_path\"")
                    lines.append(f"  {found_var}=1")
                    lines.append("done")
            if path_scan == "first":
                lines.append(f"  tool_version=\"$({version_command} 2>/dev/null | head -n 1 || true)\"")
                if install_template:
                    install_command = render_scan_command(install_template, '"$tool_path"')
                    lines.append(f"  install_path=\"$({install_command} 2>/dev/null | head -n 1 || true)\"")
                else:
                    lines.append("  install_path=\"\"")
                lines.append(f"  emit_software {shell_literal(tool_id)} installed \"$tool_path\" \"$tool_version\" \"$install_path\"")
                lines.append(f"  {found_var}=1")
                lines.append("fi")
        executable_globs = [str(path).strip() for path in item.get("executable_globs", []) or []]
        if executable_globs:
            version_template = str(item.get("version_command") or "{path} --version 2>&1 | head -n 1")
            install_template = str(item.get("install_path_command") or "")
            version_command = render_scan_command(version_template, '"$tool_path"')
            for glob_path in executable_globs:
                lines.append(f"for tool_path in {glob_path}; do")
                lines.append("  if [ ! -e \"$tool_path\" ] || [ ! -x \"$tool_path\" ] || [ -d \"$tool_path\" ]; then continue; fi")
                lines.append("  if ! remember_tool_path \"$tool_path\"; then continue; fi")
                lines.append(f"  tool_version=\"$({version_command} 2>/dev/null | head -n 1 || true)\"")
                if install_template:
                    install_command = render_scan_command(install_template, '"$tool_path"')
                    lines.append(f"  install_path=\"$({install_command} 2>/dev/null | head -n 1 || true)\"")
                else:
                    lines.append("  install_path=\"\"")
                lines.append(f"  emit_software {shell_literal(tool_id)} installed \"$tool_path\" \"$tool_version\" \"$install_path\"")
                lines.append(f"  {found_var}=1")
                lines.append("done")
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
    warnings = []
    if server.validation_status == "failed":
        warning = "validation status is failed"
        if server.validation_error:
            warning = f"{warning}: {redact_text(config, server.validation_error)}"
        warnings.append(warning)
    if server.workspace_status in {"failed", "skipped"}:
        warnings.append(f"workspace check status is {server.workspace_status}")

    if errors:
        message = "Server local precheck failed."
        next_action = "Fix the local precheck failures before running workspace-check, command, exec, or inventory."
    elif warnings:
        message = "Server local precheck passed with warnings."
        next_action = "Review the warnings, then run workspace-check or a short explicit command when ready."
    else:
        message = "Server local precheck passed."
        next_action = "Run workspace-check before remote file operations or reviewed command execution."

    print_contract_fields(
        server,
        status="failed" if errors else "ok",
        message=message,
        next_action=next_action,
    )

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

    for warning in warnings:
        print(f"warning: {warning}")

    if errors:
        for error in errors:
            print(f"- {error}")
        if any(is_passwordless_auth_failure(error) for error in errors):
            print_key_only_repair_guidance("; ".join(errors))
        return 2

    return 0


def cmd_command(args: argparse.Namespace) -> int:
    if getattr(args, "ssh_alias", None):
        settings = load_settings(args.settings)
        record = select_ssh_alias(settings, args.ssh_alias, args.ssh_config)
        command = display_command(build_ssh_alias_args(settings, record.alias, accept_new_host_key=args.accept_new_host_key))
        print(command if args.show_sensitive else redact_ssh_alias_text(record, command))
        return 0
    if not args.server:
        raise RemoteSshError("command requires --server or --ssh-alias.")

    config, settings, _ = load_config_for_args(args)
    server = select_server(config, args.server, args.allow_disabled)
    errors = validate_server(config, server)
    if errors:
        for error in errors:
            print(f"check failed: {error}", file=sys.stderr)
        return 2

    server, project = server_for_args_project(args, settings, server)
    command = display_command(build_ssh_args(config, settings, server, accept_new_host_key=args.accept_new_host_key))
    print_project_context(project)
    print(command if args.show_sensitive else redact_command(config, command))
    return 0


def cmd_exec(args: argparse.Namespace) -> int:
    if getattr(args, "ssh_alias", None):
        settings = load_settings(args.settings)
        record = select_ssh_alias(settings, args.ssh_alias, args.ssh_config)
        remote_command = remote_command_from_tokens(args.remote_command)
        timeout = args.timeout if args.timeout is not None else default_timeout(settings)
        result = run_ssh(build_ssh_alias_args(settings, record.alias, remote_command, args.accept_new_host_key), timeout)
        if result.stdout:
            print(result.stdout, end="")
        if result.returncode != 0:
            summary = summarize_error(result.stderr)
            if summary:
                print(redact_ssh_alias_text(record, summary), file=sys.stderr)
            return result.returncode
        return 0
    if not args.server:
        raise RemoteSshError("exec requires --server or --ssh-alias.")

    config, settings, _ = load_config_for_args(args)
    server = select_server(config, args.server, args.allow_disabled)
    errors = validate_server(config, server)
    if errors:
        for error in errors:
            print(f"check failed: {error}", file=sys.stderr)
        return 2

    remote_command = remote_command_from_tokens(args.remote_command)
    timeout = args.timeout if args.timeout is not None else default_timeout(settings)
    server, project = server_for_args_project(args, settings, server)
    if project is not None:
        print(f"project: {project.project_id}")
        print("workdir_source: project")
    result = run_ssh(build_ssh_args(config, settings, server, remote_command, args.accept_new_host_key), timeout)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode != 0:
        summary = summarize_error(result.stderr)
        if summary:
            print(redact_text(config, summary), file=sys.stderr)
        print_key_only_repair_guidance(summary or "")
        return result.returncode
    return 0


def cmd_exec_detached(args: argparse.Namespace) -> int:
    config, settings, _ = load_config_for_args(args)
    server = select_server(config, args.server, args.allow_disabled)
    errors = validate_server(config, server)
    if errors:
        for error in errors:
            print(f"check failed: {error}", file=sys.stderr)
        return 2

    remote_command = remote_command_from_tokens(args.remote_command)
    timeout = args.timeout if args.timeout is not None else default_timeout(settings)
    server, project = server_for_args_project(args, settings, server)
    manifest, manifest_path = start_detached_job(config, settings, server, remote_command, args.reason, timeout, project=project)
    print_job_start(manifest, server, manifest_path)
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config, settings, _ = load_config_for_args(args)
    server = select_job_server(config, settings, args.server, args.allow_disabled, args.job)
    errors = validate_server(config, server)
    if errors:
        for error in errors:
            print(f"check failed: {error}", file=sys.stderr)
        return 2
    timeout = args.timeout if args.timeout is not None else default_timeout(settings)
    result = run_ssh(build_ssh_args(config, settings, server, build_job_status_command(settings, args.job)), timeout)
    if result.returncode != 0:
        summary = summarize_error(result.stderr)
        if summary:
            print(redact_text(config, summary), file=sys.stderr)
        return result.returncode
    parsed = parse_key_value_output(result.stdout)
    status_value = parsed.get("status", "unknown")
    exit_code_text = parsed.get("exit_code", "")
    message_map = {
        "running": "Detached remote job is still running.",
        "succeeded": "Detached remote job finished successfully.",
        "failed": "Detached remote job finished with a non-zero exit code.",
        "not_found": "Detached remote job metadata was not found on the remote host.",
        "unknown": "Detached remote job state could not be determined.",
    }
    next_action_map = {
        "running": "Use tail-log --job <job-id> or rerun status to keep monitoring the detached job.",
        "succeeded": "Review the produced artifacts or logs before starting the next remote step.",
        "failed": "Use tail-log --job <job-id> to inspect logs before retrying the detached job.",
        "not_found": "Confirm the job id or rerun exec-detached/request-command --detached if the job must be started again.",
        "unknown": "Inspect tail-log or the remote job directory to resolve the detached job state.",
    }
    if args.json:
        print(
            json.dumps(
                {
                    "job_id": args.job,
                    "server_id": server.id,
                    "server_name": server.name,
                    "workdir_status": server.workspace_status,
                    "software_cache_status": server.software_cache_status,
                    "message": message_map.get(status_value, message_map["unknown"]),
                    "next_action": next_action_map.get(status_value, next_action_map["unknown"]),
                    **parsed,
                },
                indent=2,
                ensure_ascii=False,
            )
        )
    else:
        print_contract_fields(
            server,
            status=status_value,
            message=message_map.get(status_value, message_map["unknown"]),
            next_action=next_action_map.get(status_value, next_action_map["unknown"]),
        )
        print(f"job_id: {args.job}")
        print(f"pid: {parsed.get('pid', '')}")
        print(f"exit_code: {exit_code_text}")
    if status_value == "failed":
        try:
            return int(exit_code_text)
        except ValueError:
            return 1
    if status_value == "not_found":
        return 3
    return 0


def cmd_tail_log(args: argparse.Namespace) -> int:
    config, settings, _ = load_config_for_args(args)
    server = select_job_server(config, settings, args.server, args.allow_disabled, args.job)
    errors = validate_server(config, server)
    if errors:
        for error in errors:
            print(f"check failed: {error}", file=sys.stderr)
        return 2
    timeout = args.timeout if args.timeout is not None else default_timeout(settings)
    lines = args.lines if args.lines is not None else default_tail_lines(settings)
    result = run_ssh(build_ssh_args(config, settings, server, build_tail_log_command(settings, args.job, lines)), timeout)
    if result.stdout:
        print(result.stdout, end="")
    if result.returncode != 0:
        summary = summarize_error(result.stderr)
        if summary:
            print(redact_text(config, summary), file=sys.stderr)
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
        versions = tool.get("versions", [])
        if isinstance(versions, list) and len(versions) > 1:
            for version in versions:
                if isinstance(version, dict):
                    print(
                        f"{key}\t{version.get('status', 'not_detected')}"
                        f"\t{version.get('path', '')}"
                        f"\t{version.get('version', '')}"
                        f"\t{version.get('install_path', '')}"
                    )
            continue
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
        require_server: bool = True,
        include_ssh_alias: bool = False,
    ) -> None:
        subparser.add_argument("--settings", type=Path, help="Path to Erie Remote SSH settings JSON.")
        subparser.add_argument("--config", type=Path, help="Path to server_list JSON. Overrides settings.")
        subparser.add_argument("--project-config", type=Path, help="Path to project JSON. Overrides automatic project discovery.")
        subparser.add_argument("--project", help="Project id for an ephemeral project workdir context.")
        subparser.add_argument("--no-project", action="store_true", help="Disable automatic project config discovery for this command.")
        if include_server:
            subparser.add_argument("--server", required=require_server, help="Server id, name, or legacy id.")
            subparser.add_argument("--allow-disabled", action="store_true", help="Allow disabled targets.")
        if include_ssh_alias:
            subparser.add_argument("--ssh-alias", help="Temporary Host alias from OpenSSH config; does not read or write the server list.")
            subparser.add_argument("--ssh-config", type=Path, help="Path to OpenSSH config for --ssh-alias.")
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

    ssh_config_parser = subparsers.add_parser("ssh-config-discover", help="List read-only temporary Host aliases from OpenSSH config.")
    add_common(ssh_config_parser, include_server=False)
    ssh_config_parser.add_argument("--ssh-config", type=Path, help="Path to OpenSSH config. Defaults to settings ssh.config_path.")
    ssh_config_parser.add_argument("--json", action="store_true", help="Print a machine-readable SSH config summary.")
    ssh_config_parser.add_argument("--show-sensitive", action="store_true", help="Show HostName, User, Port, and IdentityFile values.")
    ssh_config_parser.set_defaults(func=cmd_ssh_config_discover)

    choices_parser = subparsers.add_parser("choices", help="Show selectable servers grouped by category and function.")
    add_common(choices_parser, include_server=False)
    choices_parser.add_argument("--all", action="store_true", help="Include disabled servers.")
    choices_parser.add_argument("--host", help="Filter choices to one SSH host or IP without connecting.")
    choices_parser.add_argument("--json", action="store_true", help="Print a machine-readable grouped server choice summary.")
    choices_parser.add_argument("--show-sensitive", action="store_true", help="Show full target, username, port, and key path.")
    choices_parser.set_defaults(func=cmd_choices)

    init_parser = subparsers.add_parser("init-config", help="Create an empty v1 server list if needed.")
    add_common(init_parser, include_server=False)
    init_parser.add_argument("--force", action="store_true", help="Overwrite an existing server list after creating a backup.")
    init_parser.set_defaults(func=cmd_init_config)

    configure_parser = subparsers.add_parser("configure", help="Choose manual or guided SSH server configuration.")
    add_common(configure_parser, include_server=False)
    configure_parser.add_argument("--interactive", action="store_true", help="Prompt for configuration mode and fields.")
    configure_parser.add_argument("--server", help="Existing server id/name to update in guided mode.")
    configure_parser.add_argument("--show-sensitive", action="store_true", help="Show key paths and generated public key content.")
    configure_parser.set_defaults(func=cmd_configure)

    add_server_parser = subparsers.add_parser("add-server", help="Add a server entry to the configured server list.")
    add_common(add_server_parser, include_server=False)
    add_server_parser.add_argument("--interactive", action="store_true", help="Prompt grouped server fields, show a summary, and write the server list.")
    add_server_parser.add_argument("--show-sensitive", action="store_true", help="Show key paths and generated public key content.")
    add_server_parser.set_defaults(func=cmd_add_server)

    update_server_parser = subparsers.add_parser("update-server", help="Update one configured SSH server entry.")
    add_common(update_server_parser)
    update_server_parser.add_argument("--interactive", action="store_true", help="Prompt with a field menu using the existing server as defaults.")
    update_server_parser.add_argument("--show-sensitive", action="store_true", help="Show key paths and generated public key content.")
    update_server_parser.set_defaults(func=cmd_update_server)

    configure_key_parser = subparsers.add_parser("configure-key", help="Repair one server's key_name and passwordless validation only.")
    add_common(configure_key_parser)
    configure_key_parser.add_argument("--interactive", action="store_true", help="Prompt for key-only repair and verify before writing.")
    configure_key_parser.add_argument("--show-sensitive", action="store_true", help="Show key paths and generated public key content.")
    configure_key_parser.add_argument("--timeout", type=positive_int, help="SSH timeout in seconds. Defaults to settings.")
    configure_key_parser.set_defaults(func=cmd_configure_key)

    project_init_parser = subparsers.add_parser("project-init", help="Create local project workdir config after checking the remote directory.")
    add_common(project_init_parser)
    project_init_parser.add_argument("--interactive", action="store_true", help="Prompt for collision handling before writing project config.")
    project_init_parser.add_argument("--remote-workdir", help="Explicit remote project workdir, or a safe name under ~/workspace.")
    project_init_parser.add_argument("--timeout", type=positive_int, help="SSH timeout in seconds. Defaults to settings.")
    project_init_parser.set_defaults(func=cmd_project_init)

    project_show_parser = subparsers.add_parser("project-show", help="Show current project workdir selection.")
    add_common(project_show_parser)
    project_show_parser.add_argument("--show-sensitive", action="store_true", help="Show project config path and effective workdir.")
    project_show_parser.set_defaults(func=cmd_project_show)

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
    request_upload_parser.add_argument("--local", required=True, help="Local source path inside configured paths.upload_roots.")
    request_upload_parser.add_argument("--remote", required=True, help="Relative remote target inside server workdir.")
    request_upload_parser.add_argument("--reason", help="Reason for the upload request.")
    request_upload_parser.add_argument(
        "--confirm-sensitive-local-upload",
        action="store_true",
        help="Confirm that a sensitive local source path may be uploaded.",
    )
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
    request_command_parser.add_argument("--detached", action="store_true", help="Create a detached long-running command request.")
    request_command_parser.add_argument("remote_command", nargs=argparse.REMAINDER, help="Remote command after --.")
    request_command_parser.set_defaults(func=cmd_request_command)

    run_request_parser = subparsers.add_parser("run-request", help="Execute an approved request.")
    add_common(run_request_parser, include_server=False)
    run_request_parser.add_argument("--request", required=True, type=Path, help="Request JSON path.")
    run_request_parser.add_argument("--execute", action="store_true", help="Required explicit execution gate.")
    run_request_parser.add_argument("--timeout", type=positive_int, help="SSH/SCP timeout in seconds. Defaults to settings.")
    run_request_parser.add_argument(
        "--confirm-sensitive-local-upload",
        action="store_true",
        help="Confirm execution of an upload request with a sensitive local source path.",
    )
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
    add_common(command_parser, include_host_key_policy=True, require_server=False, include_ssh_alias=True)
    command_parser.add_argument("--show-sensitive", action="store_true", help="Show a runnable command with full target and key path.")
    command_parser.set_defaults(func=cmd_command)

    exec_parser = subparsers.add_parser("exec", help="Run an explicit remote command over SSH.")
    add_common(exec_parser, include_host_key_policy=True, require_server=False, include_ssh_alias=True)
    exec_parser.add_argument("--timeout", type=positive_int, help="SSH timeout in seconds. Defaults to settings.")
    exec_parser.add_argument("remote_command", nargs=argparse.REMAINDER, help="Remote command after --.")
    exec_parser.set_defaults(func=cmd_exec)

    exec_detached_parser = subparsers.add_parser("exec-detached", help="Start a long-running remote command and record a resumable job.")
    add_common(exec_detached_parser)
    exec_detached_parser.add_argument("--reason", required=True, help="Reason for starting the detached remote command.")
    exec_detached_parser.add_argument("--timeout", type=positive_int, help="SSH timeout in seconds for the startup handshake. Defaults to settings.")
    exec_detached_parser.add_argument("remote_command", nargs=argparse.REMAINDER, help="Remote command after --.")
    exec_detached_parser.set_defaults(func=cmd_exec_detached)

    status_parser = subparsers.add_parser("status", help="Check a detached remote job status.")
    add_common(status_parser, require_server=False)
    status_parser.add_argument("--job", required=True, help="Detached job id.")
    status_parser.add_argument("--timeout", type=positive_int, help="SSH timeout in seconds. Defaults to settings.")
    status_parser.add_argument("--json", action="store_true", help="Print a machine-readable job status.")
    status_parser.set_defaults(func=cmd_status)

    tail_log_parser = subparsers.add_parser("tail-log", help="Tail a detached job stdout log.")
    add_common(tail_log_parser, require_server=False)
    tail_log_parser.add_argument("--job", required=True, help="Detached job id.")
    tail_log_parser.add_argument("--lines", type=positive_int, help="Number of stdout log lines. Defaults to settings.")
    tail_log_parser.add_argument("--timeout", type=positive_int, help="SSH timeout in seconds. Defaults to settings.")
    tail_log_parser.set_defaults(func=cmd_tail_log)

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


def unsupported_cmd_message(subcommand: str) -> str:
    return (
        f"received unsupported --cmd for {subcommand}. "
        f"Use: remote_ssh.py {subcommand} ... -- <remote command>"
    )


def precheck_unsupported_cmd(argv: list[str]) -> int | None:
    for index, token in enumerate(argv):
        if token not in {"exec", "request-command"}:
            continue
        remainder = argv[index + 1 :]
        before_separator = []
        for item in remainder:
            if item == "--":
                break
            before_separator.append(item)
        if "--cmd" in before_separator:
            print(f"error: {unsupported_cmd_message(token)}", file=sys.stderr)
            return 2
        return None
    return None


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    precheck = precheck_unsupported_cmd(raw_argv)
    if precheck is not None:
        return precheck
    parser = build_parser()
    args = parser.parse_args(raw_argv)
    try:
        return int(args.func(args))
    except RemoteSshError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
