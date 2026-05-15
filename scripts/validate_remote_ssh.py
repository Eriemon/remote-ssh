#!/usr/bin/env python3
"""Validation suite for the Erie Remote SSH skill helper."""

from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from typing import Any

import remote_ssh


ROOT = remote_ssh.PROJECT_ROOT
SKILL_DIR = remote_ssh.SKILL_DIR
TOOL = SKILL_DIR / "scripts" / "remote_ssh.py"
SEMVER_PATTERN = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:-[0-9A-Za-z.-]+)?(?:\+[0-9A-Za-z.-]+)?$")
ENCODING_CANARY = "编码校验：中文内容应保持 UTF-8，无乱码。"


class ValidationError(Exception):
    pass


def run_tool(
    args: list[str],
    expected: int | set[int] = 0,
    input_text: str | None = None,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    expected_set = {expected} if isinstance(expected, int) else expected
    result = subprocess.run(
        [sys.executable, str(TOOL), *args],
        cwd=cwd or ROOT,
        text=True,
        input=input_text,
        capture_output=True,
        check=False,
    )
    if result.returncode not in expected_set:
        raise ValidationError(
            f"command failed: remote_ssh.py {' '.join(args)}\n"
            f"returncode: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def resolve_optional_path(settings: dict[str, Any], value: str | Path | None) -> Path | None:
    if value is None or str(value) == "":
        return None
    expanded = remote_ssh.expand_placeholders(str(value), settings["_context"])
    if not expanded.strip():
        return None
    return remote_ssh.resolve_config_path(expanded, remote_ssh.settings_path(settings), settings["_context"])


def resolve_skill_validator(settings: dict[str, Any], override: Path | None = None) -> Path:
    if override is not None:
        path = override.resolve()
        if path.exists():
            return path
        raise ValidationError(f"skill validator not found: {path}")

    candidates = remote_ssh.settings_value(settings, "tools", "skill_validator_candidates", default=[])
    if not isinstance(candidates, list):
        raise ValidationError("settings.tools.skill_validator_candidates must be a list.")

    checked: list[str] = []
    for candidate in candidates:
        path = resolve_optional_path(settings, str(candidate))
        if path is None:
            continue
        checked.append(str(path))
        if path.exists():
            return path

    env_hint = "REMOTE_SSH_SKILL_VALIDATOR"
    raise ValidationError(
        "skill validator not found. Set tools.skill_validator_candidates in settings, "
        f"pass --skill-validator, or set {env_hint}. Checked: {checked}"
    )


def run_validator(skill_validator: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(skill_validator), str(SKILL_DIR)],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValidationError(f"skill validator failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")


def assert_contains(text: str, needle: str, label: str) -> None:
    if needle not in text:
        raise ValidationError(f"expected {label} to contain {needle!r}\n{text}")


def assert_not_contains(text: str, needle: str, label: str) -> None:
    if needle and needle in text:
        raise ValidationError(f"expected {label} not to contain {needle!r}\n{text}")


def load_ref(server_list: Path) -> dict:
    with server_list.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, data: object) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    return path


def init_git_repo(path: Path) -> None:
    subprocess.run(["git", "init", "-b", "master"], cwd=path, text=True, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.name", "Validation Bot"], cwd=path, text=True, capture_output=True, check=True)
    subprocess.run(["git", "config", "user.email", "validation@example.invalid"], cwd=path, text=True, capture_output=True, check=True)


def commit_all(path: Path, message: str) -> None:
    subprocess.run(["git", "add", "."], cwd=path, text=True, capture_output=True, check=True)
    subprocess.run(["git", "commit", "-m", message], cwd=path, text=True, capture_output=True, check=True)


def init_release_fixture_project(project_root: Path, version: str = "0.1.9") -> Path:
    skill_root = project_root / "skills" / "erie-remote-ssh"
    (skill_root / "scripts").mkdir(parents=True, exist_ok=True)
    (skill_root / "config").mkdir(parents=True, exist_ok=True)
    (skill_root / "references").mkdir(parents=True, exist_ok=True)
    (skill_root / "agents").mkdir(parents=True, exist_ok=True)
    (project_root / "dist").mkdir(parents=True, exist_ok=True)
    (skill_root / "SKILL.md").write_text("---\nname: erie-remote-ssh\ndescription: validation fixture\n---\nfixture\n", encoding="utf-8")
    (skill_root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (skill_root / "agents" / "openai.yaml").write_text("display_name: fixture\n", encoding="utf-8")
    (skill_root / "config" / "defaults.json").write_text('{"version": 1}\n', encoding="utf-8")
    (skill_root / "references" / "configuration.md").write_text("configuration\n", encoding="utf-8")
    (skill_root / "references" / "workflows.md").write_text("workflows\n", encoding="utf-8")
    (skill_root / "references" / "review-checklist.md").write_text("review\n", encoding="utf-8")
    (skill_root / "scripts" / "build_release.py").write_bytes((SKILL_DIR / "scripts" / "build_release.py").read_bytes())
    return skill_root


def run_fixture_build_release(project_root: Path, skill_root: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(skill_root / "scripts" / "build_release.py")],
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValidationError(
            f"fixture build_release.py failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def run_fixture_release_gate(
    project_root: Path,
    version: str,
    *,
    phase: str,
    install_intent: str = "requested",
) -> dict[str, Any]:
    result = subprocess.run(
        [
            sys.executable,
            str(resolve_agents_generator_script("manage_docs.py")),
            "release-gate",
            str(project_root),
            "--version",
            version,
            "--skill-dir",
            "skills/erie-remote-ssh",
            "--phase",
            phase,
            "--install-intent",
            install_intent,
        ],
        cwd=project_root,
        env={**os.environ, "CODEX_HOME": str((Path.home() / ".codex").resolve())},
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValidationError(f"fixture release-gate failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return json.loads(result.stdout)


def fake_scan_output() -> str:
    return "\n".join(
        [
            "hostname: validation-host",
            "kernel: Linux validation 6.0 x86_64 GNU/Linux",
            "cpu_model: Validation CPU",
            "cpu_threads: 8",
            "gpu_nvidia: Validation GPU",
            "fpga_xilinx: 0000:01:00.0 Processing accelerators: Xilinx Device",
            "software:python:installed:/usr/bin/python3:Python 3.11.0:",
            "software:conda:not_detected:::",
            "software:cuda:installed:/usr/local/cuda-11.8/bin/nvcc:Cuda compilation tools, release 11.8:/usr/local/cuda-11.8",
            "software:cuda:installed:/usr/local/cuda-12.2/bin/nvcc:Cuda compilation tools, release 12.2:/usr/local/cuda-12.2",
            "software:gcc:installed:/usr/bin/gcc:gcc (Ubuntu) 12.2.0:",
            "software:gcc:installed:/usr/bin/gcc-11:gcc (Ubuntu) 11.4.0:",
            "software:gcc:installed:/usr/bin/gcc-12:gcc (Ubuntu) 12.2.0:",
            "software:gpp:installed:/usr/bin/g++:g++ (Ubuntu) 12.2.0:",
            "software:cmake:installed:/usr/bin/cmake:cmake version 3.25.0:",
            "software:vivado:installed:/tools/Xilinx/Vivado/2024.1/bin/vivado:Vivado v2024.1:/tools/Xilinx/Vivado/2024.1",
            "software:vitis:installed:/tools/Xilinx/Vitis/2024.1/bin/vitis:Vitis v2024.1:/tools/Xilinx/Vitis/2024.1",
            "fpga_device:0:0000:01:00.0:0000:01:00.1",
            "",
        ]
    )


def compact_scan_inventory() -> dict[str, Any]:
    return {
        "catalog_version": 2,
        "software_catalog": [
            {
                "id": "gcc",
                "commands": ["gcc"],
                "path_scan": "all",
                "executable_globs": ["/usr/bin/gcc-[0-9]*"],
                "version_command": "{path} --version 2>&1 | head -n 1",
            },
            {
                "id": "cuda",
                "commands": ["nvcc"],
                "executable_globs": ["/usr/local/cuda-*/bin/nvcc"],
                "version_command": "{path} --version 2>&1 | awk '/release/ {print $0; exit}'",
                "install_path_command": "dirname \"$(dirname {path})\"",
            },
            {
                "id": "vivado",
                "directory_scans": [
                    {
                        "base_dirs": ["/tools/Xilinx"],
                        "subdir": "Vivado",
                        "executable": "bin/vivado",
                        "version_command": "{path} -version 2>&1 | head -n 1",
                    }
                ],
            },
        ],
    }


def create_fake_ssh(tmp_dir: Path, output: str | None = None, exit_code: int = 0) -> Path:
    helper_dir = tmp_dir / "fake-ssh"
    helper_dir.mkdir(parents=True, exist_ok=True)
    output_file = helper_dir / "scan-output.txt"
    output_file.write_text(output if output is not None else fake_scan_output(), encoding="utf-8")
    if os.name == "nt":
        script = helper_dir / "fake-ssh.cmd"
        script.write_text(
            f"@echo off\r\n"
            f"type \"{output_file}\"\r\n"
            f"exit /b {exit_code}\r\n",
            encoding="utf-8",
        )
    else:
        script = helper_dir / "fake-ssh.sh"
        script.write_text(f"#!/usr/bin/env sh\ncat {str(output_file)!r}\nexit {exit_code}\n", encoding="utf-8")
        script.chmod(0o755)
    return script


def create_sequence_fake_ssh(tmp_dir: Path, steps: list[dict[str, Any]]) -> Path:
    helper_dir = tmp_dir / "sequence-fake-ssh"
    helper_dir.mkdir(parents=True, exist_ok=True)
    steps_file = helper_dir / "steps.json"
    state_file = helper_dir / "state.txt"
    helper = helper_dir / "sequence_fake_ssh.py"
    write_json(steps_file, steps)
    state_file.write_text("0", encoding="utf-8")
    helper.write_text(
        "\n".join(
            [
                "import json",
                "import pathlib",
                "import sys",
                f"steps = json.loads(pathlib.Path({str(steps_file)!r}).read_text(encoding='utf-8'))",
                f"state = pathlib.Path({str(state_file)!r})",
                "try:",
                "    index = int(state.read_text(encoding='utf-8').strip() or '0')",
                "except ValueError:",
                "    index = 0",
                "step = steps[index] if index < len(steps) else steps[-1]",
                "state.write_text(str(index + 1), encoding='utf-8')",
                "stdout = str(step.get('stdout', ''))",
                "stderr = str(step.get('stderr', ''))",
                "if stdout:",
                "    sys.stdout.write(stdout)",
                "if stderr:",
                "    sys.stderr.write(stderr)",
                "raise SystemExit(int(step.get('returncode', 0)))",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        script = helper_dir / "sequence-fake-ssh.cmd"
        script.write_text(f"@echo off\r\npython \"{helper}\" %*\r\nexit /b %ERRORLEVEL%\r\n", encoding="utf-8")
    else:
        script = helper_dir / "sequence-fake-ssh.sh"
        script.write_text(f"#!/usr/bin/env sh\npython3 {str(helper)!r} \"$@\"\n", encoding="utf-8")
        script.chmod(0o755)
    return script


def create_fake_keygen(tmp_dir: Path) -> Path:
    helper_dir = tmp_dir / "fake-keygen"
    helper_dir.mkdir(parents=True, exist_ok=True)
    helper = helper_dir / "fake-keygen.py"
    helper.write_text(
        "\n".join(
            [
                "import pathlib",
                "import sys",
                "args = sys.argv[1:]",
                "try:",
                "    target = pathlib.Path(args[args.index('-f') + 1])",
                "except (ValueError, IndexError):",
                "    print('missing -f target', file=sys.stderr)",
                "    raise SystemExit(2)",
                "if target.exists():",
                "    print('target exists', file=sys.stderr)",
                "    raise SystemExit(3)",
                "target.parent.mkdir(parents=True, exist_ok=True)",
                "target.write_text('fake private key\\n', encoding='utf-8')",
                "(target.parent / (target.name + '.pub')).write_text('ssh-ed25519 fake-public-key validation@example\\n', encoding='utf-8')",
                "print('fake keygen ok')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        script = helper_dir / "fake-keygen.cmd"
        script.write_text(f"@echo off\r\npython \"{helper}\" %*\r\nexit /b %ERRORLEVEL%\r\n", encoding="utf-8")
    else:
        script = helper_dir / "fake-keygen.sh"
        script.write_text(f"#!/usr/bin/env sh\npython3 {str(helper)!r} \"$@\"\n", encoding="utf-8")
        script.chmod(0o755)
    return script


def create_invalid_utf8_helper(tmp_dir: Path) -> Path:
    helper_dir = tmp_dir / "invalid-utf8-helper"
    helper_dir.mkdir(parents=True, exist_ok=True)
    helper = helper_dir / "invalid_utf8.py"
    helper.write_text(
        "import sys\n"
        "sys.stdout.buffer.write(b'ok\\n')\n"
        "sys.stderr.buffer.write(b'bad byte: \\xff\\n')\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        script = helper_dir / "invalid-utf8.cmd"
        script.write_text(f"@echo off\r\npython \"{helper}\" %*\r\nexit /b %ERRORLEVEL%\r\n", encoding="utf-8")
    else:
        script = helper_dir / "invalid-utf8.sh"
        script.write_text(f"#!/usr/bin/env sh\npython3 {str(helper)!r} \"$@\"\n", encoding="utf-8")
        script.chmod(0o755)
    return script


def create_stdin_lf_helper(tmp_dir: Path) -> Path:
    helper_dir = tmp_dir / "stdin-lf-helper"
    helper_dir.mkdir(parents=True, exist_ok=True)
    helper = helper_dir / "stdin_lf_check.py"
    helper.write_text(
        "\n".join(
            [
                "import sys",
                "data = sys.stdin.buffer.read()",
                "if b'\\r\\n' in data:",
                "    sys.stderr.write('crlf-detected\\n')",
                "    raise SystemExit(3)",
                "sys.stdout.write('lf-only\\n')",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    if os.name == "nt":
        script = helper_dir / "stdin-lf-check.cmd"
        script.write_text(f"@echo off\r\npython \"{helper}\" %*\r\nexit /b %ERRORLEVEL%\r\n", encoding="utf-8")
    else:
        script = helper_dir / "stdin-lf-check.sh"
        script.write_text(f"#!/usr/bin/env sh\npython3 {str(helper)!r} \"$@\"\n", encoding="utf-8")
        script.chmod(0o755)
    return script


def validation_fixture_config(key_dir: Path) -> dict[str, Any]:
    return {
        "version": 1,
        "default_key_dir": str(key_dir),
        "servers": [
            {
                "id": "server_1",
                "legacy_server_id": "1",
                "name": "Validation Primary",
                "category": "FPGA",
                "functions": ["Vivado synthesis", "remote app testing"],
                "type": "ssh",
                "host": "validation-primary.example.invalid",
                "port": 10022,
                "username": "validation-user",
                "key_name": "id_validation_primary",
                "workdir": "~/workspace",
                "enabled": True,
                "validation": {
                    "status": "verified",
                    "method": "ssh_key",
                    "verified_at": "2026-01-01T00:00:00",
                    "last_error": None,
                },
                "workspace_check": {
                    "status": "ok",
                    "checked_at": "2026-01-01T00:00:00",
                    "message": "The working directory can be accessed: ~/workspace",
                },
                "software_scan": {
                    "status": "ok",
                    "scanned_at": "2026-01-01T00:00:00Z",
                    "catalog_version": 1,
                    "tools": {
                        "python": {
                            "status": "installed",
                            "path": "/usr/bin/python3",
                            "version": "Python 3.11.0",
                            "install_path": "",
                        },
                        "vivado": {
                            "status": "installed",
                            "path": "/tools/Xilinx/Vivado/2024.1/bin/vivado",
                            "version": "Vivado v2024.1",
                            "install_path": "/tools/Xilinx/Vivado/2024.1",
                        },
                        "conda": {
                            "status": "not_detected",
                            "path": "",
                            "version": "",
                            "install_path": "",
                        },
                    },
                    "fpga_devices": [
                        {
                            "device_id": 0,
                            "pcie_bdf_mgmt": "0000:01:00.0",
                            "pcie_bdf_user": "0000:01:00.1",
                        }
                    ],
                    "raw_summary": "validation software snapshot",
                },
            },
            {
                "id": "server_4",
                "legacy_server_id": "4",
                "name": "Validation Warning",
                "category": "GPU",
                "functions": ["CUDA validation"],
                "type": "ssh",
                "host": "validation-warning.example.invalid",
                "port": 20022,
                "username": "warning-user",
                "key_name": "id_validation_warning",
                "workdir": "~/workspace",
                "enabled": True,
                "validation": {
                    "status": "failed",
                    "method": "ssh_key",
                    "verified_at": None,
                    "last_error": "validation failure for redaction check",
                },
                "workspace_check": {
                    "status": "skipped",
                    "checked_at": "2026-01-01T00:00:00",
                    "message": "Workspace check skipped because passwordless validation failed.",
                },
            },
            {
                "id": "server_5",
                "legacy_server_id": "5",
                "name": "Validation Disabled",
                "category": "Testing",
                "functions": ["disabled target validation"],
                "type": "ssh",
                "host": "validation-disabled.example.invalid",
                "port": 30022,
                "username": "disabled-user",
                "key_name": "id_validation_disabled",
                "workdir": "~/workspace",
                "enabled": False,
                "validation": {
                    "status": "skipped",
                    "method": "ssh_key",
                    "verified_at": None,
                    "last_error": None,
                },
                "workspace_check": {
                    "status": "skipped",
                    "checked_at": "2026-01-01T00:00:00",
                    "message": "Workspace check skipped for disabled fixture.",
                },
            },
        ],
    }


def create_validation_server_list(tmp_dir: Path) -> Path:
    key_dir = tmp_dir / "fixture-keys"
    key_dir.mkdir(parents=True, exist_ok=True)
    for key_name in ["id_validation_primary", "id_validation_warning", "id_validation_disabled"]:
        (key_dir / key_name).write_text("validation private key placeholder\n", encoding="utf-8")
        (key_dir / f"{key_name}.pub").write_text(
            f"ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI{key_name} validation@example\n",
            encoding="utf-8",
        )
    return write_json(tmp_dir / "fixtures" / "server_list.validation.json", validation_fixture_config(key_dir))


def shell_compatible_path(path: Path, runner: str | None = None) -> str:
    if os.name != "nt":
        return str(path)
    resolved = path.resolve()
    drive = resolved.drive.rstrip(":").lower()
    rest = str(resolved)[2:].replace("\\", "/")
    if runner and "system32" in runner.casefold() and "bash" in runner.casefold():
        return f"/mnt/{drive}{rest}"
    cygpath = shutil.which("cygpath")
    if cygpath:
        result = subprocess.run([cygpath, "-u", str(path)], text=True, capture_output=True, check=False)
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
    return f"/{drive}{rest}"


def copy_settings_with_server_list(
    settings: dict[str, Any],
    target: Path,
    server_list: Path,
    requests_dir: Path | None = None,
    downloads_dir: Path | None = None,
    upload_roots: list[str] | None = None,
    default_workdir: str | None = None,
) -> Path:
    source_settings = remote_ssh.settings_path(settings)
    settings_copy = json.loads(source_settings.read_text(encoding="utf-8"))
    settings_copy["paths"]["default_server_list"] = str(server_list)
    if requests_dir is not None:
        settings_copy["paths"]["requests_dir"] = str(requests_dir)
    if downloads_dir is not None:
        settings_copy["paths"]["downloads_dir"] = str(downloads_dir)
    if upload_roots is not None:
        settings_copy["paths"]["upload_roots"] = upload_roots
    if default_workdir is not None:
        settings_copy.setdefault("ssh", {})["default_workdir"] = default_workdir
    target.write_text(json.dumps(settings_copy, indent=2), encoding="utf-8")
    return target


def sensitive_values(config: dict) -> list[str]:
    values: set[str] = set()
    for server in config["servers"]:
        for field in ["host", "username", "key_name", "port"]:
            value = server.get(field)
            if value is not None:
                values.add(str(value))
    return sorted(values, key=len, reverse=True)


def assert_redacted(output: str, config: dict, label: str) -> None:
    for value in sensitive_values(config):
        if len(value) < 6:
            pattern = rf"(?<![A-Za-z0-9_.-]){re.escape(value)}(?![A-Za-z0-9_.-])"
            if re.search(pattern, output):
                raise ValidationError(f"expected {label} not to contain sensitive token {value!r}\n{output}")
            continue
        assert_not_contains(output, value, label)


def latest_request(requests_dir: Path) -> dict:
    files = sorted(requests_dir.glob("*.json"))
    if not files:
        raise ValidationError(f"expected a request file in {requests_dir}")
    return json.loads(files[-1].read_text(encoding="utf-8"))


def request_from_output(output: str) -> dict:
    for line in output.splitlines():
        if line.startswith("request: "):
            return load_ref(Path(line.removeprefix("request: ").strip()))
    raise ValidationError(f"expected command output to include a request path\n{output}")


def clone_first_server(config: dict) -> dict:
    return json.loads(json.dumps(config["servers"][0]))


def tool_base_args(settings_path: Path, server_list: Path) -> list[str]:
    return ["--settings", str(settings_path), "--config", str(server_list)]


def validation_name(settings: dict[str, Any], key: str, fallback: str | None = None) -> str:
    value = remote_ssh.settings_value(settings, "validation", key, default=fallback)
    if not value:
        raise ValidationError(f"settings.validation.{key} is required for validation.")
    return str(value)


def codex_skills_root() -> Path:
    codex_home = Path(os.environ.get("CODEX_HOME", Path.home() / ".codex")).expanduser()
    return codex_home / "skills"


def resolve_agents_generator_script(name: str) -> Path:
    home_codex = Path.home() / ".codex" / "skills"
    candidates = [
        codex_skills_root() / "agents-md-generator" / "scripts" / name,
        codex_skills_root() / ".system" / "agents-md-generator" / "scripts" / name,
        home_codex / "agents-md-generator" / "scripts" / name,
        home_codex / ".system" / "agents-md-generator" / "scripts" / name,
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    raise ValidationError(f"agents-md-generator script not found: {name} (checked: {candidates})")


def run_agents_generator_tool(
    script_name: str,
    args: list[str],
    *,
    expected: int | set[int] = 0,
    cwd: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    expected_set = {expected} if isinstance(expected, int) else expected
    script_path = resolve_agents_generator_script(script_name)
    env = os.environ.copy()
    env["CODEX_HOME"] = str((Path.home() / ".codex").resolve())
    result = subprocess.run(
        [sys.executable, str(script_path), *args],
        cwd=cwd or ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode not in expected_set:
        raise ValidationError(
            f"agents-md-generator command failed: {script_name} {' '.join(args)}\n"
            f"returncode: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )
    return result


def require_output_field(output: str, field: str, label: str) -> None:
    assert_contains(output, f"{field}:", label)


def expected_governance_version() -> str:
    project_agents = ROOT / "AGENTS.md"
    if not project_agents.exists():
        return "unknown"
    match = re.search(r"generator_version=([^;> ]+)", project_agents.read_text(encoding="utf-8", errors="ignore"))
    if match:
        return match.group(1)
    return "unknown"


def validate_workspace_session_review_count(
    inspected_count: int,
    reviewed_count: int,
    reviewed_handoff: int,
    handoff_count: int,
) -> None:
    if reviewed_count == inspected_count:
        return
    # A single new exact-cwd session may exist before the next handoff refreshes
    # docs-governance-state.json. Treat that one-session gap as an active
    # in-progress review window, but reject larger drift.
    if reviewed_handoff == handoff_count and inspected_count == reviewed_count + 1:
        return
    raise ValidationError(
        "docs governance state last_workspace_session_count must match the latest "
        "review boundary. Only one in-progress exact-cwd session beyond the last "
        f"reviewed handoff is allowed (reviewed={reviewed_count}, inspected={inspected_count}, "
        f"reviewed_handoff={reviewed_handoff}, handoff_count={handoff_count})."
    )


def governance_window_tests() -> None:
    validate_workspace_session_review_count(14, 14, 10, 10)
    validate_workspace_session_review_count(15, 14, 10, 10)
    for inspected, reviewed, reviewed_handoff, handoff_count in [
        (13, 14, 10, 10),
        (16, 14, 10, 10),
        (15, 14, 9, 10),
    ]:
        try:
            validate_workspace_session_review_count(inspected, reviewed, reviewed_handoff, handoff_count)
        except ValidationError as exc:
            assert_contains(str(exc), "last_workspace_session_count", "workspace review drift guard")
        else:
            raise ValidationError(
                "workspace review drift guard accepted an invalid session freshness combination"
            )


def assurance_scope_tests() -> None:
    builder = getattr(remote_ssh, "build_assurance_report", None)
    if builder is None:
        raise ValidationError("remote_ssh.py must expose build_assurance_report for validator output contracts.")

    offline = builder(with_ssh=False, real_ssh_verified=False)
    if offline.get("verified_scopes") != ["offline-source", "governance", "release", "installed-skill"]:
        raise ValidationError(f"unexpected offline assurance scopes: {offline}")
    if offline.get("release_version") != skill_version():
        raise ValidationError(f"assurance report release_version must match VERSION: {offline}")
    if offline.get("governance_version") != expected_governance_version():
        raise ValidationError(f"assurance report governance_version must report AGENTS generator metadata or unknown in isolated fixtures: {offline}")
    real_ssh = offline.get("real_ssh") or {}
    if real_ssh.get("status") != "not-verified":
        raise ValidationError(f"offline assurance must mark real SSH as not-verified: {offline}")
    if "不能诚实地声称远程 100% 正确" not in str(offline.get("message", "")):
        raise ValidationError(f"offline assurance message must state the remote-confidence boundary: {offline}")

    online = builder(with_ssh=True, real_ssh_verified=True)
    if online.get("verified_scopes") != ["offline-source", "governance", "release", "installed-skill", "real-ssh"]:
        raise ValidationError(f"unexpected real-ssh assurance scopes: {online}")
    online_real_ssh = online.get("real_ssh") or {}
    if online_real_ssh.get("status") != "verified":
        raise ValidationError(f"real SSH assurance must report verified when live checks pass: {online}")


def governance_alignment_tests() -> None:
    if not (ROOT / "AGENTS.md").exists():
        return
    inspect = run_agents_generator_tool("inspect_project.py", ["."], cwd=ROOT)
    inspect_data = json.loads(inspect.stdout)
    if inspect_data.get("root_agents_md_trigger_required"):
        raise ValidationError(f"root AGENTS.md should not remain trigger-required:\n{inspect.stdout}")
    if inspect_data.get("root_agents_md_rebuild_required"):
        raise ValidationError(f"root AGENTS.md should not remain rebuild-required:\n{inspect.stdout}")
    state_path = ROOT / ".agents" / "docs-governance-state.json"
    state = load_ref(state_path)
    handoff_count = int(state.get("handoff_count", 0))
    reviewed_handoff = int(state.get("last_workspace_session_reviewed_handoff", 0))
    reviewed_count = int(state.get("last_workspace_session_count", 0))
    reviewed_at = str(state.get("last_workspace_session_sync_at", "")).strip()
    cross_project_count = int(state.get("last_cross_project_remote_ssh_session_count", 0))
    cross_project_sync_at = str(state.get("last_cross_project_remote_ssh_session_sync_at", "")).strip()
    if int(state.get("last_experience_at", 0)) != handoff_count:
        raise ValidationError("docs governance state must keep last_experience_at aligned with the latest handoff_count.")
    if reviewed_handoff != handoff_count:
        raise ValidationError(
            "docs governance state must record the exact handoff count used for the latest workspace-session freshness review"
        )
    validate_workspace_session_review_count(
        int(inspect_data.get("matched_session_count", -1)),
        reviewed_count,
        reviewed_handoff,
        handoff_count,
    )
    if not reviewed_at:
        raise ValidationError("docs governance state must record last_workspace_session_sync_at")
    if cross_project_count <= 0:
        raise ValidationError("docs governance state must record a positive last_cross_project_remote_ssh_session_count.")
    if not cross_project_sync_at:
        raise ValidationError("docs governance state must record last_cross_project_remote_ssh_session_sync_at.")

    control = load_ref(ROOT / ".agents" / "agents-control.json")
    branch_policy = control.get("git_branch_policy") or {}
    if branch_policy.get("release_requires_merge_to_master") is not True:
        raise ValidationError("agents-control git_branch_policy.release_requires_merge_to_master must be true for strict release flow.")
    if branch_policy.get("delete_other_local_branches_before_release") is not True:
        raise ValidationError("agents-control git_branch_policy.delete_other_local_branches_before_release must be true for strict release flow.")
    patterns = ((control.get("skill_design_contract") or {}).get("patterns")) or []
    if patterns != ["Tool Wrapper", "Pipeline", "Reviewer", "Inversion"]:
        raise ValidationError(f"agents-control skill_design_contract.patterns must be the reduced standard set: {patterns}")

    for agents_path in [ROOT / "AGENTS.md", ROOT / "docs" / "AGENTS.md"]:
        if not agents_path.exists():
            continue
        agents_text = agents_path.read_text(encoding="utf-8")
        assert_not_contains(agents_text, "Last verified: never", f"{agents_path.name} metadata")
    assert_contains((ROOT / "AGENTS.md").read_text(encoding="utf-8"), "Release version source", "AGENTS versioning clarification")
    assert_not_contains((ROOT / "AGENTS.md").read_text(encoding="utf-8"), "Design patterns: Tool Wrapper, Generator, Reviewer, Inversion, Pipeline.", "AGENTS design pattern list")

    handoff_text = (ROOT / "docs" / "handoff" / "HANDOFF.md").read_text(encoding="utf-8")
    assert_contains(handoff_text, "不能诚实地声称远程 100% 正确", "handoff real-ssh boundary")

    workflow_text = (ROOT / "docs" / "experience" / "1-workflow.md").read_text(encoding="utf-8")
    assert_contains(workflow_text, f"`handoff_count={handoff_count}`", "workflow experience freshness")
    latest_snapshot = f"handoff-{handoff_count}.json"
    assert_contains(workflow_text, latest_snapshot, "workflow experience latest snapshot reference")

    required_docs = [
        SKILL_DIR / "references" / "integration-contract.md",
        SKILL_DIR / "references" / "regression-scenarios.md",
    ]
    for path in required_docs:
        if not path.exists():
            raise ValidationError(f"missing required reference document: {path.relative_to(SKILL_DIR).as_posix()}")


def positive_tests(settings: dict[str, Any], settings_path: Path, server_list: Path, ref_config: dict) -> None:
    base = tool_base_args(settings_path, server_list)
    positive_server = validation_name(settings, "positive_server", clone_first_server(ref_config)["id"])
    warning_server = validation_name(settings, "warning_server", positive_server)

    discover_result = run_tool(["discover", *base])
    assert_contains(discover_result.stdout, "status: available", "discover output")
    require_output_field(discover_result.stdout, "message", "discover output")
    require_output_field(discover_result.stdout, "next_action", "discover output")
    assert_redacted(discover_result.stdout, ref_config, "discover output")

    discover_json_result = run_tool(["discover", *base, "--json"])
    discover_data = json.loads(discover_json_result.stdout)
    if discover_data.get("status") != "available" or discover_data.get("enabled_ssh_count", 0) < 1:
        raise ValidationError(f"unexpected discover json output: {discover_json_result.stdout}")
    for field in ["message", "next_action"]:
        if field not in discover_data:
            raise ValidationError(f"discover json missing {field}: {discover_json_result.stdout}")
    if discover_data.get("server_list_path") != "<redacted>":
        raise ValidationError(f"discover json should redact server_list_path by default: {discover_json_result.stdout}")
    assert_not_contains(discover_json_result.stdout, str(server_list), "discover json output")
    assert_redacted(discover_json_result.stdout, ref_config, "discover json output")

    list_result = run_tool(["list", *base])
    assert_contains(list_result.stdout, "<redacted>", "list output")
    assert_redacted(list_result.stdout, ref_config, "list output")

    list_all_result = run_tool(["list", *base, "--all"])
    assert_contains(list_all_result.stdout, positive_server, "list --all output")
    assert_redacted(list_all_result.stdout, ref_config, "list --all output")

    check_result = run_tool(["check", *base, "--server", positive_server])
    assert_contains(check_result.stdout, "status: ok", "check output")
    assert_contains(check_result.stdout, "key_path: <redacted>", "check output")
    for field in ["server_id", "server_name", "workdir_status", "software_cache_status", "message", "next_action"]:
        require_output_field(check_result.stdout, field, "check output")
    assert_redacted(check_result.stdout, ref_config, "check output")

    command_result = run_tool(["command", *base, "--server", positive_server])
    assert_contains(command_result.stdout, "StrictHostKeyChecking=yes", "command output")
    assert_contains(command_result.stdout, "UpdateHostKeys=no", "command output")
    assert_not_contains(command_result.stdout, "StrictHostKeyChecking=accept-new", "command output")
    assert_redacted(command_result.stdout, ref_config, "command output")

    accept_new_result = run_tool(["command", *base, "--server", positive_server, "--accept-new-host-key"])
    assert_contains(accept_new_result.stdout, "StrictHostKeyChecking=accept-new", "accept-new command output")

    warning_result = run_tool(["check", *base, "--server", warning_server])
    assert_contains(warning_result.stdout, "warning:", "warning output")
    for field in ["server_id", "server_name", "workdir_status", "software_cache_status", "message", "next_action"]:
        require_output_field(warning_result.stdout, field, "warning output")
    assert_redacted(warning_result.stdout, ref_config, "warning output")

    software_result = run_tool(["software", *base, "--server", positive_server])
    assert_contains(software_result.stdout, "software_scan_status: ok", "software output")
    assert_contains(software_result.stdout, "python\tinstalled", "software output")
    assert_contains(software_result.stdout, "vivado\tinstalled", "software output")
    assert_redacted(software_result.stdout, ref_config, "software output")

    vivado_result = run_tool(["software", *base, "--server", positive_server, "--name", "vivado"])
    assert_contains(vivado_result.stdout, "name: vivado", "software --name output")
    assert_contains(vivado_result.stdout, "status: installed", "software --name output")
    assert_contains(vivado_result.stdout, "install_path:", "software --name output")
    assert_redacted(vivado_result.stdout, ref_config, "software --name output")

    missing_software_result = run_tool(["software", *base, "--server", positive_server, "--name", "matlab"], expected=3)
    assert_contains(missing_software_result.stdout, "status: not_scanned", "missing software output")


def choices_tests(settings: dict[str, Any], settings_path: Path, server_list: Path, ref_config: dict, tmp_dir: Path) -> None:
    base = tool_base_args(settings_path, server_list)
    before = server_list.read_text(encoding="utf-8")
    is_validation_fixture = any(server.get("id") == "server_5" for server in ref_config.get("servers", []))

    choices_result = run_tool(["choices", *base])
    assert_contains(choices_result.stdout, "next: reply with the server id or name to select a target before any remote access.", "choices output")
    assert_redacted(choices_result.stdout, ref_config, "choices output")
    if server_list.read_text(encoding="utf-8") != before:
        raise ValidationError("choices changed the server list")
    if is_validation_fixture:
        assert_contains(choices_result.stdout, "Category: FPGA", "choices output")
        assert_contains(choices_result.stdout, "Category: GPU", "choices output")
        assert_contains(choices_result.stdout, "id: server_1", "choices output")
        assert_contains(choices_result.stdout, "functions: Vivado synthesis; remote app testing", "choices output")
        assert_contains(choices_result.stdout, "software: python installed; vivado installed", "choices output")
        assert_not_contains(choices_result.stdout, "server_5", "default choices output")

    choices_json_result = run_tool(["choices", *base, "--json"])
    choices_data = json.loads(choices_json_result.stdout)
    if choices_data.get("status") != "available":
        raise ValidationError(f"unexpected choices json status: {choices_json_result.stdout}")
    if choices_data.get("server_list_path") != "<redacted>":
        raise ValidationError(f"choices json should redact server_list_path by default: {choices_json_result.stdout}")
    assert_not_contains(choices_json_result.stdout, str(server_list), "choices json output")
    records = choices_data.get("servers", [])
    expected_enabled_count = sum(1 for server in ref_config.get("servers", []) if isinstance(server, dict) and server.get("enabled") is True)
    if len(records) != expected_enabled_count:
        raise ValidationError(f"default choices json should contain enabled servers only: {choices_json_result.stdout}")
    first = next((record for record in records if record.get("id") == "server_1"), None)
    if first is None:
        raise ValidationError(f"choices json missing server_1: {choices_json_result.stdout}")
    if first.get("enabled") is not True:
        raise ValidationError(f"choices json server_1 should be enabled: {first}")
    for required_key in ["category", "functions", "validation_status", "workspace_status"]:
        if required_key not in first:
            raise ValidationError(f"choices json missing {required_key}: {first}")
    if is_validation_fixture:
        expected_subset = {
            "category": "FPGA",
            "functions": ["Vivado synthesis", "remote app testing"],
            "enabled": True,
            "validation_status": "verified",
            "workspace_status": "ok",
        }
        for key, expected in expected_subset.items():
            if first.get(key) != expected:
                raise ValidationError(f"choices json field {key} mismatch: {first}")
    assert_redacted(choices_json_result.stdout, ref_config, "choices json output")

    if is_validation_fixture:
        choices_all_result = run_tool(["choices", *base, "--all"])
        assert_contains(choices_all_result.stdout, "Category: Testing", "choices --all output")
        assert_contains(choices_all_result.stdout, "id: server_5", "choices --all output")
        assert_contains(choices_all_result.stdout, "availability: disabled - requires explicit enablement before remote access", "choices --all output")
        assert_redacted(choices_all_result.stdout, ref_config, "choices --all output")

        multi_host_config = json.loads(json.dumps(ref_config))
        first_login = clone_first_server(ref_config)
        first_login["id"] = "same_host_primary"
        first_login["legacy_server_id"] = ""
        first_login["name"] = "Same Host Primary"
        first_login["host"] = "same-host.example.invalid"
        first_login["port"] = 10022
        first_login["username"] = "validation-user"
        second_login = clone_first_server(ref_config)
        second_login["id"] = "same_host_primary_alt_port"
        second_login["legacy_server_id"] = ""
        second_login["name"] = "Same Host Primary Alt Port"
        second_login["host"] = "same-host.example.invalid"
        second_login["port"] = 20022
        second_login["username"] = "validation-user"
        third_login = clone_first_server(ref_config)
        third_login["id"] = "same_host_third"
        third_login["legacy_server_id"] = ""
        third_login["name"] = "Same Host Third"
        third_login["host"] = "same-host.example.invalid"
        third_login["port"] = 52026
        third_login["username"] = "yanghy"
        multi_host_config["servers"] = [first_login, second_login, third_login]
        multi_host_path = write_json(tmp_dir / "multi-host-choices.json", multi_host_config)

        host_choices = run_tool(["choices", "--settings", str(settings_path), "--config", str(multi_host_path), "--host", "same-host.example.invalid"])
        assert_contains(host_choices.stdout, "selection_required: true", "choices --host output")
        assert_contains(host_choices.stdout, "id: same_host_primary", "choices --host output")
        assert_contains(host_choices.stdout, "id: same_host_third", "choices --host output")
        assert_not_contains(host_choices.stdout, "52026", "choices --host output")
        assert_not_contains(host_choices.stdout, "yanghy", "choices --host output")
        assert_redacted(host_choices.stdout, multi_host_config, "choices --host output")

        host_choices_sensitive = run_tool(
            ["choices", "--settings", str(settings_path), "--config", str(multi_host_path), "--host", "same-host.example.invalid", "--show-sensitive"]
        )
        assert_contains(host_choices_sensitive.stdout, "port: 52026", "choices --host sensitive output")
        assert_contains(host_choices_sensitive.stdout, "username: yanghy", "choices --host sensitive output")

        ambiguous_host = run_tool(["command", "--settings", str(settings_path), "--config", str(multi_host_path), "--server", "same-host.example.invalid"], expected=1)
        assert_contains(ambiguous_host.stderr, "Selector matched multiple servers", "ambiguous host selector")
        assert_contains(ambiguous_host.stderr, "same_host_primary", "ambiguous host selector")
        assert_contains(ambiguous_host.stderr, "same_host_third", "ambiguous host selector")

        ambiguous_user_host = run_tool(["command", "--settings", str(settings_path), "--config", str(multi_host_path), "--server", "validation-user@same-host.example.invalid"], expected=1)
        assert_contains(ambiguous_user_host.stderr, "specify a port", "ambiguous user@host selector")

        unique_user_host_port = run_tool(
            [
                "command",
                "--settings",
                str(settings_path),
                "--config",
                str(multi_host_path),
                "--server",
                "yanghy@same-host.example.invalid:52026",
                "--show-sensitive",
            ]
        )
        assert_contains(unique_user_host_port.stdout, "-p 52026", "user@host:port selector")
        assert_contains(unique_user_host_port.stdout, "yanghy@same-host.example.invalid", "user@host:port selector")

    legacy_config = json.loads(json.dumps(ref_config))
    legacy_server = legacy_config["servers"][0]
    legacy_server.pop("category", None)
    legacy_server.pop("functions", None)
    legacy_server["notes"] = (
        "legacy validation node "
        f"{legacy_server['host']} {legacy_server['username']} {legacy_server['key_name']}"
    )
    legacy_path = write_json(tmp_dir / "legacy-choices.json", {"version": 1, "default_key_dir": ref_config["default_key_dir"], "servers": [legacy_server]})
    legacy_result = run_tool(["choices", "--settings", str(settings_path), "--config", str(legacy_path)])
    assert_contains(legacy_result.stdout, "Category: FPGA", "legacy choices output")
    assert_contains(legacy_result.stdout, "functions: legacy validation node <redacted> <redacted> <redacted>", "legacy choices output")
    assert_contains(legacy_result.stdout, "vivado installed", "legacy choices output")
    assert_redacted(legacy_result.stdout, legacy_config, "legacy choices output")

    inferred_config = {
        "version": 1,
        "default_key_dir": ref_config["default_key_dir"],
        "servers": [dict(clone_first_server(ref_config)), dict(clone_first_server(ref_config))],
    }
    inferred_fpga = inferred_config["servers"][0]
    inferred_fpga["id"] = "inferred_fpga"
    inferred_fpga["name"] = "Inferred FPGA"
    inferred_fpga.pop("category", None)
    inferred_fpga.pop("software_scan", None)
    inferred_fpga.pop("notes", None)
    inferred_fpga.pop("inventory_snapshot", None)
    inferred_fpga["functions"] = ["Alveo U55C Vivado synthesis"]
    inferred_gpu = inferred_config["servers"][1]
    inferred_gpu["id"] = "inferred_gpu"
    inferred_gpu["name"] = "Inferred GPU"
    inferred_gpu.pop("category", None)
    inferred_gpu.pop("software_scan", None)
    inferred_gpu.pop("notes", None)
    inferred_gpu.pop("inventory_snapshot", None)
    inferred_gpu["functions"] = ["RTX4090 CUDA validation"]
    inferred_path = write_json(tmp_dir / "inferred-category-choices.json", inferred_config)
    inferred_result = run_tool(["choices", "--settings", str(settings_path), "--config", str(inferred_path)])
    assert_contains(inferred_result.stdout, "Category: FPGA", "inferred choices output")
    assert_contains(inferred_result.stdout, "Category: GPU", "inferred choices output")
    assert_redacted(inferred_result.stdout, inferred_config, "inferred choices output")

    legacy_json_result = run_tool(["choices", "--settings", str(settings_path), "--config", str(legacy_path), "--json"])
    assert_contains(legacy_json_result.stdout, "legacy validation node <redacted> <redacted> <redacted>", "legacy choices json output")
    assert_redacted(legacy_json_result.stdout, legacy_config, "legacy choices json output")

    disabled_only = json.loads(json.dumps(ref_config))
    for server in disabled_only["servers"]:
        server["enabled"] = False
    disabled_path = write_json(tmp_dir / "disabled-only-choices.json", disabled_only)
    disabled_result = run_tool(["choices", "--settings", str(settings_path), "--config", str(disabled_path)], expected=4)
    assert_contains(disabled_result.stdout, "status: no_enabled_ssh", "disabled-only choices output")
    assert_contains(disabled_result.stdout, "availability: disabled - requires explicit enablement before remote access", "disabled-only choices output")
    assert_redacted(disabled_result.stdout, disabled_only, "disabled-only choices output")


def software_scan_tests(settings: dict[str, Any], tmp_dir: Path) -> None:
    server_list = create_validation_server_list(tmp_dir / "scan-fixture")
    config = load_ref(server_list)
    config["servers"][0].pop("software_scan", None)
    write_json(server_list, config)
    fake_ssh = create_fake_ssh(tmp_dir)
    temp_settings = copy_settings_with_server_list(settings, tmp_dir / "scan-settings.json", server_list)
    settings_copy = load_ref(temp_settings)
    settings_copy["tools"]["ssh_client"] = str(fake_ssh)
    settings_copy["inventory"] = compact_scan_inventory()
    write_json(temp_settings, settings_copy)

    positive_server = config["servers"][0]["id"]
    scan_result = run_tool(["scan-software", "--settings", str(temp_settings), "--server", positive_server])
    assert_contains(scan_result.stdout, "software_scan_status: ok", "scan-software output")
    assert_contains(scan_result.stdout, "vivado\tinstalled", "scan-software output")

    updated = load_ref(server_list)
    snapshot = updated["servers"][0].get("software_scan")
    if not snapshot or snapshot.get("status") != "ok":
        raise ValidationError(f"scan-software did not cache an ok snapshot: {updated}")
    tools = snapshot.get("tools", {})
    if tools.get("vivado", {}).get("install_path") != "/tools/Xilinx/Vivado/2024.1":
        raise ValidationError(f"scan-software did not cache Vivado install path: {snapshot}")
    if snapshot.get("fpga_devices", [{}])[0].get("pcie_bdf_mgmt") != "0000:01:00.0":
        raise ValidationError(f"scan-software did not cache FPGA device data: {snapshot}")

    tools = snapshot.get("tools", {})
    if len(tools.get("gcc", {}).get("versions", [])) != 3:
        raise ValidationError(f"scan-software did not cache all GCC versions: {snapshot}")
    if len(tools.get("cuda", {}).get("versions", [])) != 2:
        raise ValidationError(f"scan-software did not cache all CUDA versions: {snapshot}")

    software_table_result = run_tool(["software", "--settings", str(temp_settings), "--server", positive_server])
    assert_contains(software_table_result.stdout, "gcc\tinstalled\t/usr/bin/gcc-11", "cached software table output")
    assert_contains(software_table_result.stdout, "cuda\tinstalled\t/usr/local/cuda-12.2/bin/nvcc", "cached software table output")

    software_result = run_tool(["software", "--settings", str(temp_settings), "--server", positive_server, "--name", "cuda"])
    assert_contains(software_result.stdout, "status: installed", "cached cuda output")
    assert_contains(software_result.stdout, "path: /usr/local/cuda-11.8/bin/nvcc", "cached cuda output")
    assert_contains(software_result.stdout, "version_entry: /usr/local/cuda-12.2/bin/nvcc", "cached cuda output")

    bad_catalog = tmp_dir / "bad-catalog-settings.json"
    bad_settings = load_ref(temp_settings)
    bad_settings["inventory"] = {"software_catalog": [{"id": "python", "commands": ["python3"]}, {"id": "python", "commands": ["python"]}]}
    write_json(bad_catalog, bad_settings)
    duplicate_result = run_tool(["scan-software", "--settings", str(bad_catalog), "--server", positive_server], expected=1)
    assert_contains(duplicate_result.stderr, "Duplicate software catalog id", "duplicate catalog error")

    missing_catalog = tmp_dir / "missing-catalog-settings.json"
    missing_catalog_settings = load_ref(temp_settings)
    missing_catalog_settings["inventory"] = {}
    write_json(missing_catalog, missing_catalog_settings)
    missing_catalog_result = run_tool(["scan-software", "--settings", str(missing_catalog), "--server", positive_server], expected=1)
    assert_contains(missing_catalog_result.stderr, "inventory.software_catalog must be a non-empty list", "missing catalog error")

    bad_command_catalog = tmp_dir / "bad-command-catalog-settings.json"
    bad_command_settings = load_ref(temp_settings)
    bad_command_settings["inventory"] = {"software_catalog": [{"id": "python", "commands": "python3"}]}
    write_json(bad_command_catalog, bad_command_settings)
    bad_command_result = run_tool(["scan-software", "--settings", str(bad_command_catalog), "--server", positive_server], expected=1)
    assert_contains(bad_command_result.stderr, "commands must be a list of non-empty strings", "bad commands catalog error")

    bad_path_scan = tmp_dir / "bad-path-scan-settings.json"
    bad_path_scan_settings = load_ref(temp_settings)
    bad_path_scan_settings["inventory"] = {"software_catalog": [{"id": "python", "commands": ["python3"], "path_scan": "everything"}]}
    write_json(bad_path_scan, bad_path_scan_settings)
    bad_path_scan_result = run_tool(["scan-software", "--settings", str(bad_path_scan), "--server", positive_server], expected=1)
    assert_contains(bad_path_scan_result.stderr, "path_scan must be either 'first' or 'all'", "bad path_scan catalog error")

    bad_glob = tmp_dir / "bad-glob-settings.json"
    bad_glob_settings = load_ref(temp_settings)
    bad_glob_settings["inventory"] = {"software_catalog": [{"id": "gcc", "executable_globs": ["usr/bin/gcc-*"]}]}
    write_json(bad_glob, bad_glob_settings)
    bad_glob_result = run_tool(["scan-software", "--settings", str(bad_glob), "--server", positive_server], expected=1)
    assert_contains(bad_glob_result.stderr, "executable_globs must contain absolute POSIX paths", "bad executable_globs catalog error")

    bad_scan = tmp_dir / "bad-scan-settings.json"
    invalid_scan_settings = load_ref(temp_settings)
    invalid_scan_settings["inventory"] = {
        "software_catalog": [
            {
                "id": "vivado",
                "directory_scans": [
                    {
                        "base_dirs": ["relative/Xilinx"],
                        "subdir": "Vivado",
                        "executable": "bin/vivado",
                    }
                ],
            }
        ]
    }
    write_json(bad_scan, invalid_scan_settings)
    invalid_scan_result = run_tool(["scan-software", "--settings", str(bad_scan), "--server", positive_server], expected=1)
    assert_contains(invalid_scan_result.stderr, "base_dirs must contain absolute POSIX paths", "invalid directory scan error")


def workspace_check_writeback_tests(settings: dict[str, Any], tmp_dir: Path) -> None:
    server_list = create_validation_server_list(tmp_dir / "workspace-writeback")
    config = load_ref(server_list)
    server_id = config["servers"][0]["id"]
    config["servers"][0].pop("validation", None)
    config["servers"][0].pop("workspace_check", None)
    config["servers"][0].pop("software_scan", None)
    write_json(server_list, config)

    temp_settings = copy_settings_with_server_list(settings, tmp_dir / "workspace-writeback-settings.json", server_list)
    settings_copy = load_ref(temp_settings)
    settings_copy["tools"]["ssh_client"] = str(
        create_sequence_fake_ssh(
            tmp_dir,
            [
                {"stdout": "/home/codex/workspace\n", "returncode": 0},
                {"stdout": fake_scan_output(), "returncode": 0},
            ],
        )
    )
    settings_copy["inventory"] = compact_scan_inventory()
    write_json(temp_settings, settings_copy)

    result = run_tool(["workspace-check", "--settings", str(temp_settings), "--server", server_id])
    assert_contains(result.stdout, "status: ok", "workspace-check writeback output")
    assert_contains(result.stdout, "backup:", "workspace-check writeback output")
    for field in ["server_id", "server_name", "workdir_status", "software_cache_status", "message", "next_action"]:
        require_output_field(result.stdout, field, "workspace-check writeback output")
    if not list(server_list.parent.glob(f"{server_list.name}.bak.*")):
        raise ValidationError("workspace-check did not create a backup before write-back")

    updated = load_ref(server_list)["servers"][0]
    if updated.get("validation", {}).get("status") != "verified":
        raise ValidationError(f"workspace-check did not persist verified validation: {updated}")
    if updated.get("validation", {}).get("method") != "ssh_workspace":
        raise ValidationError(f"workspace-check did not persist ssh_workspace method: {updated}")
    if updated.get("workspace_check", {}).get("status") != "ok":
        raise ValidationError(f"workspace-check did not persist ok workspace status: {updated}")
    if updated.get("software_scan", {}).get("status") != "ok":
        raise ValidationError(f"workspace-check did not persist software scan: {updated}")

    failure_list = create_validation_server_list(tmp_dir / "workspace-failure")
    failure_config = load_ref(failure_list)
    failure_config["servers"][0]["software_scan"] = {"status": "ok", "tools": {"python": {"status": "installed"}}}
    write_json(failure_list, failure_config)
    failure_settings = copy_settings_with_server_list(settings, tmp_dir / "workspace-failure-settings.json", failure_list)
    failure_settings_copy = load_ref(failure_settings)
    failure_settings_copy["tools"]["ssh_client"] = str(
        create_sequence_fake_ssh(tmp_dir, [{"stderr": "permission denied\n", "returncode": 255}])
    )
    write_json(failure_settings, failure_settings_copy)

    failure_result = run_tool(["workspace-check", "--settings", str(failure_settings), "--server", server_id], expected=255)
    assert_contains(failure_result.stdout, "status: failed", "workspace-check failure output")
    assert_contains(failure_result.stdout, "backup:", "workspace-check failure output")
    failed = load_ref(failure_list)["servers"][0]
    if failed.get("validation", {}).get("status") != "failed" or failed.get("workspace_check", {}).get("status") != "failed":
        raise ValidationError(f"workspace-check did not persist failed validation/workspace status: {failed}")
    if failed.get("software_scan", {}).get("status") != "ok":
        raise ValidationError(f"workspace-check failure should preserve existing software_scan: {failed}")

    scan_failure_list = create_validation_server_list(tmp_dir / "workspace-scan-failure")
    scan_failure_config = load_ref(scan_failure_list)
    scan_failure_config["servers"][0].pop("validation", None)
    scan_failure_config["servers"][0].pop("workspace_check", None)
    scan_failure_config["servers"][0].pop("software_scan", None)
    write_json(scan_failure_list, scan_failure_config)
    scan_failure_settings = copy_settings_with_server_list(settings, tmp_dir / "workspace-scan-failure-settings.json", scan_failure_list)
    scan_failure_settings_copy = load_ref(scan_failure_settings)
    scan_failure_settings_copy["tools"]["ssh_client"] = str(
        create_sequence_fake_ssh(
            tmp_dir,
            [
                {"stdout": "/home/codex/workspace\n", "returncode": 0},
                {"stderr": "scan failed\n", "returncode": 7},
            ],
        )
    )
    scan_failure_settings_copy["inventory"] = compact_scan_inventory()
    write_json(scan_failure_settings, scan_failure_settings_copy)

    scan_failure_result = run_tool(["workspace-check", "--settings", str(scan_failure_settings), "--server", server_id], expected=7)
    assert_contains(scan_failure_result.stdout, "status: ok", "workspace-check scan failure output")
    assert_contains(scan_failure_result.stdout, "software_scan_status: failed", "workspace-check scan failure output")
    scan_failed = load_ref(scan_failure_list)["servers"][0]
    if scan_failed.get("validation", {}).get("status") != "verified" or scan_failed.get("workspace_check", {}).get("status") != "ok":
        raise ValidationError(f"workspace-check scan failure should preserve verified validation/workspace status: {scan_failed}")
    if scan_failed.get("software_scan", {}).get("status") != "failed":
        raise ValidationError(f"workspace-check did not persist failed software scan: {scan_failed}")


def project_workdir_tests(settings: dict[str, Any], tmp_dir: Path) -> None:
    server_list = create_validation_server_list(tmp_dir / "project-workdir")
    config = load_ref(server_list)
    server_id = config["servers"][0]["id"]
    config["servers"][0]["workdir"] = "~/workspace/server-default"
    config["servers"][0].pop("validation", None)
    config["servers"][0].pop("workspace_check", None)
    config["servers"][0].pop("software_scan", None)
    write_json(server_list, config)

    project_root = tmp_dir / "local-project"
    nested = project_root / "src" / "nested"
    nested.mkdir(parents=True)
    project_dir = project_root / ".erie-remote-ssh"
    project_dir.mkdir()
    project_local = write_json(
        project_dir / "project.local.json",
        {
            "version": 1,
            "project_id": "local_project",
            "default_server": server_id,
            "remote_workdir": "~/workspace/local-project",
            "servers": {
                server_id: {
                    "remote_workdir": "~/workspace/local-project-server",
                }
            },
        },
    )
    write_json(
        project_dir / "project.json",
        {
            "version": 1,
            "project_id": "shared_project",
            "remote_workdir": "~/workspace/shared-project",
        },
    )

    temp_settings = copy_settings_with_server_list(settings, tmp_dir / "project-settings.json", server_list)
    settings_copy = load_ref(temp_settings)
    settings_copy["tools"]["ssh_client"] = str(
        create_sequence_fake_ssh(
            tmp_dir / "project-effective-ssh",
            [
                {"stdout": "~/workspace/local-project-server\n", "returncode": 0},
                {"stdout": "~/workspace/server-default\n", "returncode": 0},
                {"stdout": "~/workspace/adhoc_project\n", "returncode": 0},
            ],
        )
    )
    write_json(temp_settings, settings_copy)

    project_show = run_tool(["project-show", "--settings", str(temp_settings), "--server", server_id], cwd=nested)
    assert_contains(project_show.stdout, "project: local_project", "project-show output")
    assert_contains(project_show.stdout, "workdir_source: project", "project-show output")
    assert_contains(project_show.stdout, "project_config:", "project-show output")
    assert_not_contains(project_show.stdout, str(project_local), "project-show redacted output")

    explicit_project = write_json(
        tmp_dir / "explicit-project.json",
        {
            "version": 1,
            "project_id": "explicit_project",
            "remote_workdir": "~/workspace/explicit-project",
        },
    )
    explicit_show = run_tool(["project-show", "--settings", str(temp_settings), "--server", server_id, "--project-config", str(explicit_project)], cwd=nested)
    assert_contains(explicit_show.stdout, "project: explicit_project", "explicit project-show output")

    for invalid_project_id in [".", "..", "", "bad/name"]:
        invalid_result = run_tool(
            ["project-show", "--settings", str(temp_settings), "--server", server_id, "--project", invalid_project_id],
            expected=1,
        )
        assert_contains(invalid_result.stderr, "Project id must", f"invalid project id {invalid_project_id!r}")

    valid_project_id = run_tool(["project-show", "--settings", str(temp_settings), "--server", server_id, "--project", "my_project-1.0"], cwd=nested)
    assert_contains(valid_project_id.stdout, "project: my_project-1.0", "valid project id output")

    for index, invalid_workdir in enumerate(["/", "~/", "~", "~/workspace/.", "~/workspace//x", "~/workspace/.."]):
        invalid_project = write_json(
            tmp_dir / f"invalid-project-{index}.json",
            {
                "version": 1,
                "project_id": f"invalid_project_{index}",
                "remote_workdir": invalid_workdir,
            },
        )
        invalid_result = run_tool(
            ["project-show", "--settings", str(temp_settings), "--server", server_id, "--project-config", str(invalid_project)],
            expected=1,
        )
        assert_contains(invalid_result.stderr, "Project remote_workdir", f"invalid project workdir {invalid_workdir!r}")

    exec_project = run_tool(["exec", "--settings", str(temp_settings), "--server", server_id, "--", "pwd"], cwd=nested)
    assert_contains(exec_project.stdout, "~/workspace/local-project-server", "project exec output")

    exec_no_project = run_tool(["exec", "--settings", str(temp_settings), "--server", server_id, "--no-project", "--", "pwd"], cwd=nested)
    assert_contains(exec_no_project.stdout, "~/workspace/server-default", "no-project exec output")

    exec_adhoc_project = run_tool(["exec", "--settings", str(temp_settings), "--server", server_id, "--project", "adhoc_project", "--", "pwd"], cwd=tmp_dir)
    assert_contains(exec_adhoc_project.stdout, "~/workspace/adhoc_project", "adhoc project exec output")

    workspace_settings = copy_settings_with_server_list(settings, tmp_dir / "project-workspace-settings.json", server_list)
    workspace_settings_copy = load_ref(workspace_settings)
    workspace_settings_copy["tools"]["ssh_client"] = str(
        create_sequence_fake_ssh(
            tmp_dir / "project-workspace-ssh",
            [
                {"stdout": "~/workspace/local-project-server\n", "returncode": 0},
                {"stdout": fake_scan_output(), "returncode": 0},
            ],
        )
    )
    workspace_settings_copy["inventory"] = compact_scan_inventory()
    write_json(workspace_settings, workspace_settings_copy)
    workspace_result = run_tool(["workspace-check", "--settings", str(workspace_settings), "--server", server_id], cwd=nested)
    assert_contains(workspace_result.stdout, "project: local_project", "project workspace-check output")
    updated_server = load_ref(server_list)["servers"][0]
    if "workspace_check" in updated_server:
        raise ValidationError(f"project workspace-check must not overwrite global workspace_check: {updated_server}")
    project_cache = load_ref(project_local)
    server_cache = project_cache.get("servers", {}).get(server_id, {})
    if server_cache.get("workspace_check", {}).get("status") != "ok":
        raise ValidationError(f"project workspace-check did not write project cache: {project_cache}")
    if updated_server.get("validation", {}).get("status") != "verified":
        raise ValidationError(f"project workspace-check should preserve global validation success: {updated_server}")
    if updated_server.get("software_scan", {}).get("status") != "ok":
        raise ValidationError(f"project workspace-check should refresh global software scan: {updated_server}")

    request_settings = copy_settings_with_server_list(settings, tmp_dir / "project-request-settings.json", server_list)
    request_settings_copy = load_ref(request_settings)
    request_settings_copy["paths"]["requests_dir"] = str(tmp_dir / "project-requests")
    write_json(request_settings, request_settings_copy)
    request_result = run_tool(
        ["request-command", "--settings", str(request_settings), "--server", server_id, "--reason", "project context", "--", "pwd"],
        cwd=nested,
    )
    assert_contains(request_result.stdout, "request:", "project request output")
    request_path = Path(next(line.split(":", 1)[1].strip() for line in request_result.stdout.splitlines() if line.startswith("request:")))
    request_payload = load_ref(request_path)
    if request_payload.get("project_id") != "local_project" or request_payload.get("effective_workdir") != "~/workspace/local-project-server":
        raise ValidationError(f"request did not bind project context: {request_payload}")
    request_text = request_path.read_text(encoding="utf-8")
    for forbidden in ["project_config", str(project_local), str(Path.home()), ".erie-remote-ssh/project.local.json"]:
        assert_not_contains(request_text, forbidden, "project request JSON")

    run_settings = copy_settings_with_server_list(settings, tmp_dir / "project-run-settings.json", server_list)
    run_settings_copy = load_ref(run_settings)
    run_settings_copy["tools"]["ssh_client"] = str(create_sequence_fake_ssh(tmp_dir / "project-run-ssh", [{"stdout": "ok\n", "returncode": 0}, {"stdout": "ran\n", "returncode": 0}]))
    write_json(run_settings, run_settings_copy)
    run_result = run_tool(["run-request", "--settings", str(run_settings), "--request", str(request_path), "--execute"], cwd=tmp_dir)
    assert_contains(run_result.stdout, "project: local_project", "project run-request output")
    assert_contains(run_result.stdout, "ran", "project run-request output")

    collision_list = create_validation_server_list(tmp_dir / "project-init")
    collision_config = load_ref(collision_list)
    collision_config["servers"][0]["workdir"] = "~/workspace"
    write_json(collision_list, collision_config)
    collision_settings = copy_settings_with_server_list(settings, tmp_dir / "project-init-settings.json", collision_list)
    collision_settings_copy = load_ref(collision_settings)
    collision_settings_copy["tools"]["ssh_client"] = str(
        create_sequence_fake_ssh(
            tmp_dir / "project-init-ssh",
            [
                {"stdout": '{"exists":true,"is_dir":true,"is_empty":false,"parent_exists":true,"parent_writable":true}\n', "returncode": 0},
                {"stdout": '{"exists":true,"is_dir":true,"is_empty":true,"parent_exists":true,"parent_writable":true}\n', "returncode": 0},
                {"stdout": '{"exists":true,"is_dir":true,"is_empty":true,"parent_exists":true,"parent_writable":true}\n', "returncode": 0},
                {"stdout": '{"exists":false,"is_dir":false,"is_empty":true,"parent_exists":true,"parent_writable":true}\n', "returncode": 0},
                {"stdout": '{"exists":true,"is_dir":true,"is_empty":true,"parent_exists":true,"parent_writable":true}\n', "returncode": 0},
                {"stdout": '{"exists":false,"is_dir":false,"is_empty":true,"parent_exists":true,"parent_writable":true}\n', "returncode": 0},
                {"stdout": '{"exists":true,"is_dir":true,"is_empty":true,"parent_exists":true,"parent_writable":true}\n', "returncode": 0},
            ],
        )
    )
    write_json(collision_settings, collision_settings_copy)

    collision_project = tmp_dir / "project-init-cwd"
    collision_project.mkdir()
    init_overwrite = run_tool(
        ["project-init", "--settings", str(collision_settings), "--config", str(collision_list), "--server", server_id, "--project", "collision_project", "--interactive"],
        input_text="overwrite\n",
        cwd=collision_project,
    )
    assert_contains(init_overwrite.stdout, "remote_workdir_collision: true", "project-init overwrite output")
    written_project = load_ref(collision_project / ".erie-remote-ssh" / "project.local.json")
    if written_project.get("servers", {}).get(server_id, {}).get("remote_workdir_check", {}).get("collision_resolution") != "overwrite_existing_directory":
        raise ValidationError(f"overwrite collision resolution not recorded: {written_project}")

    duplicate_root = tmp_dir / "duplicate-root"
    existing_project_dir = duplicate_root / "existing" / ".erie-remote-ssh"
    existing_project_dir.mkdir(parents=True)
    write_json(
        existing_project_dir / "project.json",
        {
            "version": 1,
            "project_id": "existing_duplicate",
            "remote_workdir": "~/workspace/duplicate_project",
            "servers": {server_id: {"remote_workdir": "~/workspace/duplicate_project"}},
        },
    )
    duplicate_cwd = duplicate_root / "new"
    duplicate_cwd.mkdir()
    duplicate_result = run_tool(
        [
            "project-init",
            "--settings",
            str(collision_settings),
            "--config",
            str(collision_list),
            "--server",
            server_id,
            "--project",
            "duplicate_project",
            "--interactive",
        ],
        input_text="cancel\n",
        cwd=duplicate_cwd,
        expected=3,
    )
    assert_contains(duplicate_result.stdout, "local_project_duplicate: true", "project duplicate collision output")

    rename_project = tmp_dir / "project-rename-cwd"
    rename_project.mkdir()
    init_rename = run_tool(
        ["project-init", "--settings", str(collision_settings), "--config", str(collision_list), "--server", server_id, "--project", "rename_project", "--interactive"],
        input_text="rename\nrenamed_project\n",
        cwd=rename_project,
    )
    assert_contains(init_rename.stdout, "remote_workdir_collision: true", "project-init rename output")
    renamed_config = load_ref(rename_project / ".erie-remote-ssh" / "project.local.json")
    if renamed_config.get("remote_workdir") != "~/workspace/renamed_project":
        raise ValidationError(f"rename collision did not write renamed workdir: {renamed_config}")

    timestamp_project = tmp_dir / "project-timestamp-cwd"
    timestamp_project.mkdir()
    init_timestamp = run_tool(
        ["project-init", "--settings", str(collision_settings), "--config", str(collision_list), "--server", server_id, "--project", "timestamp_project", "--interactive"],
        input_text="timestamp\n",
        cwd=timestamp_project,
    )
    assert_contains(init_timestamp.stdout, "collision_resolution: timestamp_suffix", "project-init timestamp output")
    timestamp_config = load_ref(timestamp_project / ".erie-remote-ssh" / "project.local.json")
    if not re.fullmatch(r"~/workspace/timestamp_project-\d{8}-\d{6}", str(timestamp_config.get("remote_workdir", ""))):
        raise ValidationError(f"timestamp collision did not write suffixed workdir: {timestamp_config}")

    cancel_project = tmp_dir / "project-cancel-cwd"
    cancel_project.mkdir()
    cancel_result = run_tool(
        ["project-init", "--settings", str(collision_settings), "--config", str(collision_list), "--server", server_id, "--project", "cancel_project", "--interactive"],
        input_text="cancel\n",
        cwd=cancel_project,
        expected=3,
    )
    assert_contains(cancel_result.stdout, "project_init_status: cancelled", "project-init cancel output")
    if (cancel_project / ".erie-remote-ssh" / "project.local.json").exists():
        raise ValidationError("cancelled project-init should not write project config")


def load_remote_ssh_fixture(module_path: Path, module_name: str) -> Any:
    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise ValidationError(f"failed to load remote_ssh fixture: {module_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(module_name, None)
    return module


def project_root_semantics_tests(tmp_dir: Path) -> None:
    fixtures_root = tmp_dir / "project-root-fixtures"
    source_repo = fixtures_root / "source-repo"
    install_root = fixtures_root / "codex-home" / "skills" / "erie-remote-ssh"
    release_root = fixtures_root / "dist" / "erie-remote-ssh-v9.9.9"

    def write_fixture(skill_root: Path, repo_root: Path | None = None) -> Path:
        (skill_root / "scripts").mkdir(parents=True, exist_ok=True)
        (skill_root / "config").mkdir(parents=True, exist_ok=True)
        shutil.copy2(SKILL_DIR / "scripts" / "remote_ssh.py", skill_root / "scripts" / "remote_ssh.py")
        shutil.copy2(SKILL_DIR / "config" / "defaults.json", skill_root / "config" / "defaults.json")
        if repo_root is not None:
            (repo_root / "AGENTS.md").write_text("# fixture\n", encoding="utf-8")
        return skill_root / "scripts" / "remote_ssh.py"

    source_module = load_remote_ssh_fixture(
        write_fixture(source_repo / "skills" / "erie-remote-ssh", repo_root=source_repo),
        f"remote_ssh_source_fixture_{time.time_ns()}",
    )
    install_module = load_remote_ssh_fixture(
        write_fixture(install_root),
        f"remote_ssh_install_fixture_{time.time_ns()}",
    )
    release_module = load_remote_ssh_fixture(
        write_fixture(release_root),
        f"remote_ssh_release_fixture_{time.time_ns()}",
    )

    source_settings = source_module.load_settings()
    install_settings = install_module.load_settings()
    release_settings = release_module.load_settings()

    if Path(source_settings["_context"]["project_root"]) != source_repo.resolve():
        raise ValidationError(
            f"source layout should resolve project_root to repo root: {source_settings['_context']['project_root']}"
        )
    if Path(install_settings["_context"]["project_root"]) != install_root.resolve():
        raise ValidationError(
            f"installed layout should resolve project_root to skill root: {install_settings['_context']['project_root']}"
        )
    if Path(release_settings["_context"]["project_root"]) != release_root.resolve():
        raise ValidationError(
            f"release layout should resolve project_root to skill root: {release_settings['_context']['project_root']}"
        )

    source_roots = source_module.configured_upload_roots(source_settings)
    install_roots = install_module.configured_upload_roots(install_settings)
    release_roots = release_module.configured_upload_roots(release_settings)
    if source_roots != [source_repo.resolve()]:
        raise ValidationError(f"source layout default upload_roots drifted: {source_roots}")
    if install_roots != [install_root.resolve()]:
        raise ValidationError(f"installed layout default upload_roots drifted: {install_roots}")
    if release_roots != [release_root.resolve()]:
        raise ValidationError(f"release layout default upload_roots drifted: {release_roots}")


def session_mining_contract_tests(tmp_dir: Path) -> None:
    miner_path = SKILL_DIR / "scripts" / "mine_remote_ssh_sessions.py"
    if not miner_path.exists():
        raise ValidationError("missing session miner script: scripts/mine_remote_ssh_sessions.py")

    miner = load_remote_ssh_fixture(miner_path, "remote_ssh_session_miner_fixture")
    builder = getattr(miner, "build_summary", None)
    if builder is None:
        raise ValidationError("mine_remote_ssh_sessions.py must expose build_summary().")

    sessions_root = tmp_dir / "session-fixtures"
    exact_session = sessions_root / "2026" / "05" / "14" / "exact.jsonl"
    exact_session.parent.mkdir(parents=True, exist_ok=True)
    exact_lines = [
        {"timestamp": "2026-05-14T01:00:00Z", "payload": {"session_meta": {"payload": {"cwd": str(ROOT)}}}},
        {"timestamp": "2026-05-14T01:00:01Z", "payload": {"message": "same-host workspace-check Permission denied timed out"}},
    ]
    exact_session.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in exact_lines) + "\n", encoding="utf-8")

    cross_session = sessions_root / "2026" / "05" / "14" / "cross.jsonl"
    cross_lines = [
        {"timestamp": "2026-05-14T02:00:00Z", "payload": {"session_meta": {"payload": {"cwd": "F:/Other/Project"}}}},
        {"timestamp": "2026-05-14T02:00:01Z", "payload": {"message": "configure-key exec-detached ssh-config-discover Connection reset"}},
    ]
    cross_session.write_text("\n".join(json.dumps(item, ensure_ascii=False) for item in cross_lines) + "\n", encoding="utf-8")

    ignored_session = sessions_root / "2026" / "05" / "14" / "ignored.jsonl"
    ignored_session.write_text(json.dumps({"payload": {"message": "plain unrelated text"}}, ensure_ascii=False) + "\n", encoding="utf-8")

    summary = builder(sessions_root=sessions_root, target_cwd=str(ROOT))
    if summary.get("exact_cwd_session_count") != 1:
        raise ValidationError(f"session miner exact-cwd count drifted: {summary}")
    if summary.get("cross_project_session_count") != 1:
        raise ValidationError(f"session miner cross-project count drifted: {summary}")
    if summary.get("total_remote_ssh_session_count") != 2:
        raise ValidationError(f"session miner total count drifted: {summary}")

    themes = summary.get("themes") or {}
    for theme in [
        "same-host disambiguation",
        "configure-key repair",
        "workspace-check/auth",
        "exec-detached/job resume",
        "ssh-config-discover fallback",
    ]:
        if int(themes.get(theme, 0)) < 1:
            raise ValidationError(f"session miner missing theme {theme!r}: {summary}")

    failures = summary.get("typical_failure_modes") or {}
    for failure in ["permission-denied", "connection-reset", "timed-out"]:
        if int(failures.get(failure, 0)) < 1:
            raise ValidationError(f"session miner missing failure mode {failure!r}: {summary}")

    recommendations = summary.get("recommended_hardening") or []
    if not recommendations:
        raise ValidationError(f"session miner must provide recommended_hardening: {summary}")


def subprocess_decoding_tests(tmp_dir: Path) -> None:
    helper = create_invalid_utf8_helper(tmp_dir)
    try:
        result = remote_ssh.run_ssh([str(helper)], 5)
    except UnicodeDecodeError as exc:
        raise ValidationError(f"run_ssh must tolerate undecodable subprocess output: {exc}") from exc
    if result.returncode != 0:
        raise ValidationError(f"invalid utf8 helper should exit 0: {result.returncode}")
    assert_contains(result.stdout, "ok", "invalid utf8 helper stdout")
    assert_contains(result.stderr, "bad byte:", "invalid utf8 helper stderr")

    stdin_helper = create_stdin_lf_helper(tmp_dir)
    stdin_result = remote_ssh.run_ssh([str(stdin_helper)], 5, input_text="set -u\nprintf 'ok\\n'\n")
    if stdin_result.returncode != 0:
        raise ValidationError(
            "run_ssh must preserve LF-only stdin for remote shell transport\n"
            f"stdout:\n{stdin_result.stdout}\nstderr:\n{stdin_result.stderr}"
        )
    assert_contains(stdin_result.stdout, "lf-only", "stdin lf helper stdout")


def software_scan_transport_tests(tmp_dir: Path) -> None:
    settings = remote_ssh.load_settings(SKILL_DIR / "config" / "defaults.json")
    config = load_ref(create_validation_server_list(tmp_dir / "transport-fixture"))
    server = remote_ssh.select_server(config, validation_name(settings, "positive_server"), False)
    script = remote_ssh.build_software_scan_script(settings)
    command_args = remote_ssh.build_ssh_args(config, settings, server, remote_ssh.SOFTWARE_SCAN_TRANSPORT_COMMAND, False)
    command_text = remote_ssh.display_command(command_args)
    if remote_ssh.SOFTWARE_SCAN_TRANSPORT_COMMAND not in command_text:
        raise ValidationError("software scan transport must use a short remote shell command")
    if script in command_text:
        raise ValidationError("software scan transport must not inline the full scan script into the SSH command")
    if os.name == "nt" and len(command_text) >= 8191:
        raise ValidationError(
            f"software scan transport command remains too long for Windows command execution: {len(command_text)}"
        )


def ssh_error_classification_tests() -> None:
    original_run = remote_ssh.subprocess.run

    def raise_winerror_206(*args: Any, **kwargs: Any) -> Any:
        raise FileNotFoundError(206, "The filename or extension is too long")

    remote_ssh.subprocess.run = raise_winerror_206
    try:
        try:
            remote_ssh.run_ssh(["ssh", "example"], 5)
        except remote_ssh.RemoteSshError as exc:
            text = str(exc)
            assert_contains(text, "too long on Windows", "WinError 206 classification")
            assert_not_contains(text, "not found on PATH", "WinError 206 classification")
        else:
            raise ValidationError("run_ssh should translate WinError 206 into a command-length error")
    finally:
        remote_ssh.subprocess.run = original_run


def backup_collision_tests(tmp_dir: Path) -> None:
    target = tmp_dir / "backup-collision" / "server_list.local.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text('{"version": 1, "servers": []}\n', encoding="utf-8")
    first = remote_ssh.backup_file(target)
    second = remote_ssh.backup_file(target)
    if first is None or second is None:
        raise ValidationError("backup_file should create backups for existing files")
    if first == second:
        raise ValidationError(f"backup_file reused the same backup path: {first}")
    if not first.exists() or not second.exists():
        raise ValidationError(f"backup_file did not create both backups: {first}, {second}")


def negative_tests(settings_path: Path, server_list: Path, ref_config: dict, tmp_dir: Path) -> None:
    base = tool_base_args(settings_path, server_list)
    invalid_json = tmp_dir / "invalid.json"
    invalid_json.write_text("{", encoding="utf-8")
    run_tool(["list", "--settings", str(settings_path), "--config", str(invalid_json)], expected=1)

    bad_version = write_json(tmp_dir / "bad-version.json", {"version": 2, "servers": []})
    run_tool(["list", "--settings", str(settings_path), "--config", str(bad_version)], expected=1)

    bad_servers = write_json(tmp_dir / "bad-servers.json", {"version": 1, "servers": {}})
    run_tool(["list", "--settings", str(settings_path), "--config", str(bad_servers)], expected=1)

    non_object_server = write_json(tmp_dir / "non-object-server.json", {"version": 1, "servers": [1]})
    run_tool(["list", "--settings", str(settings_path), "--config", str(non_object_server)], expected=1)

    base_server = clone_first_server(ref_config)

    missing_field_config = {"version": 1, "default_key_dir": ref_config["default_key_dir"], "servers": [dict(base_server)]}
    missing_field_config["servers"][0].pop("host")
    missing_field = write_json(tmp_dir / "missing-field.json", missing_field_config)
    result = run_tool(["check", "--settings", str(settings_path), "--config", str(missing_field), "--server", base_server["id"]], expected=2)
    assert_contains(result.stdout, "missing field: host", "missing field output")

    bad_port_config = {"version": 1, "default_key_dir": ref_config["default_key_dir"], "servers": [dict(base_server)]}
    bad_port_config["servers"][0]["port"] = 0
    bad_port = write_json(tmp_dir / "bad-port.json", bad_port_config)
    run_tool(["check", "--settings", str(settings_path), "--config", str(bad_port), "--server", base_server["id"]], expected=2)

    bad_enabled_config = {"version": 1, "default_key_dir": ref_config["default_key_dir"], "servers": [dict(base_server)]}
    bad_enabled_config["servers"][0]["enabled"] = "true"
    bad_enabled = write_json(tmp_dir / "bad-enabled.json", bad_enabled_config)
    run_tool(["check", "--settings", str(settings_path), "--config", str(bad_enabled), "--server", base_server["id"]], expected=1)

    run_tool(["check", *base, "--server", "does-not-exist"], expected=1)

    ambiguous_config = {"version": 1, "default_key_dir": ref_config["default_key_dir"], "servers": [dict(base_server), dict(base_server)]}
    ambiguous = write_json(tmp_dir / "ambiguous.json", ambiguous_config)
    run_tool(["check", "--settings", str(settings_path), "--config", str(ambiguous), "--server", base_server["id"]], expected=1)

    disabled_config = {"version": 1, "default_key_dir": ref_config["default_key_dir"], "servers": [dict(base_server)]}
    disabled_config["servers"][0]["enabled"] = False
    disabled = write_json(tmp_dir / "disabled.json", disabled_config)
    run_tool(["check", "--settings", str(settings_path), "--config", str(disabled), "--server", base_server["id"]], expected=1)

    missing_key_config = {"version": 1, "default_key_dir": ref_config["default_key_dir"], "servers": [dict(base_server)]}
    missing_key_config["servers"][0]["key_name"] = "definitely_missing_remote_ssh_validation_key"
    missing_key = write_json(tmp_dir / "missing-key.json", missing_key_config)
    result = run_tool(["check", "--settings", str(settings_path), "--config", str(missing_key), "--server", base_server["id"]], expected=2)
    assert_contains(result.stdout, "key file not found", "missing key output")
    assert_contains(result.stdout, "configure-key", "missing key output")
    assert_not_contains(result.stdout, "definitely_missing_remote_ssh_validation_key", "missing key output")

    run_tool(["exec", *base, "--server", base_server["id"], "--timeout", "0", "--", "echo", "ok"], expected=2)


def discovery_and_add_tests(settings: dict[str, Any], tmp_dir: Path) -> None:
    missing_list = tmp_dir / "managed" / "server_list.local.json"
    temp_settings = copy_settings_with_server_list(
        settings,
        tmp_dir / "managed-settings.json",
        missing_list,
        default_workdir="~/workspace-from-settings",
    )

    missing_result = run_tool(["discover", "--settings", str(temp_settings)], expected=3)
    assert_contains(missing_result.stdout, "status: not_configured", "missing discover output")

    missing_json = run_tool(["discover", "--settings", str(temp_settings), "--json"], expected=3)
    missing_data = json.loads(missing_json.stdout)
    if missing_data.get("status") != "not_configured" or missing_data.get("server_list_exists") is not False:
        raise ValidationError(f"unexpected missing discover json: {missing_json.stdout}")

    run_tool(["init-config", "--settings", str(temp_settings)])
    managed_settings = load_ref(temp_settings)
    key_dir = tmp_dir / "managed-keys"
    key_dir.mkdir(parents=True, exist_ok=True)
    key_path = key_dir / "id_remote_validation"
    key_path.write_text("validation private key placeholder\n", encoding="utf-8")
    (key_dir / "id_remote_validation.pub").write_text("ssh-ed25519 validation validation@example\n", encoding="utf-8")
    managed_config = load_ref(missing_list)
    managed_config["default_key_dir"] = str(key_dir)
    write_json(missing_list, managed_config)
    managed_settings["tools"]["ssh_client"] = str(create_fake_ssh(tmp_dir))
    managed_settings["inventory"] = compact_scan_inventory()
    write_json(temp_settings, managed_settings)
    empty_result = run_tool(["discover", "--settings", str(temp_settings)], expected=4)
    assert_contains(empty_result.stdout, "status: no_enabled_ssh", "empty discover output")

    add_input = "\n\nexample.internal\n\ncodex\nid_remote_validation\n\n\nGeneral\nremote development; workspace validation\nvalidation note\ny\n"
    add_result = run_tool(["add-server", "--settings", str(temp_settings), "--interactive"], input_text=add_input)
    assert_contains(add_result.stdout, "section: connection", "add-server output")
    assert_contains(add_result.stdout, "section: metadata", "add-server output")
    assert_contains(add_result.stdout, "server_record_summary:", "add-server output")
    assert_contains(add_result.stdout, "added: server_1", "add-server output")
    assert_contains(add_result.stdout, "backup:", "add-server output")
    backups = list(missing_list.parent.glob("server_list.local.json.bak.*"))
    if not backups:
        raise ValidationError("expected add-server to create a server list backup")

    managed_config = load_ref(missing_list)
    if managed_config["servers"][0]["id"] != "server_1" or managed_config["servers"][0]["port"] != 22:
        raise ValidationError(f"unexpected added server record: {managed_config}")
    if managed_config["servers"][0]["workdir"] != "~/workspace-from-settings" or managed_config["servers"][0]["enabled"] is not True:
        raise ValidationError(f"unexpected add-server defaults: {managed_config}")
    if managed_config["servers"][0].get("category") != "General":
        raise ValidationError(f"add-server did not write category: {managed_config}")
    if managed_config["servers"][0].get("functions") != ["remote development", "workspace validation"]:
        raise ValidationError(f"add-server did not parse functions metadata: {managed_config}")
    if managed_config["servers"][0].get("software_scan", {}).get("status") != "ok":
        raise ValidationError(f"add-server did not cache software_scan: {managed_config}")

    available_result = run_tool(["discover", "--settings", str(temp_settings)])
    assert_contains(available_result.stdout, "status: available", "available discover output")

    same_host_input = "server_2\nsame-host-alt\nexample.internal\n52026\nyanghy\n\nid_remote_validation\n~/workspace\ny\n\n\nsame host alt\ny\n"
    same_host_result = run_tool(["add-server", "--settings", str(temp_settings), "--interactive"], input_text=same_host_input)
    assert_contains(same_host_result.stdout, "matching_host_count: 1", "same-host add-server output")
    assert_contains(same_host_result.stdout, "existing: server_1", "same-host add-server output")
    assert_contains(same_host_result.stdout, "port: 22", "same-host add-server output")
    assert_contains(same_host_result.stdout, "added: server_2", "same-host add-server output")
    same_host_config = load_ref(missing_list)
    if len(same_host_config["servers"]) != 2 or same_host_config["servers"][1]["port"] != 52026:
        raise ValidationError(f"same-host add-server did not add the alternate login: {same_host_config}")

    before_duplicate_identity = json.dumps(same_host_config, sort_keys=True)
    duplicate_identity_input = "server_3\nsame-host-duplicate\nexample.internal\n22\ncodex\n\n"
    duplicate_identity_result = run_tool(["add-server", "--settings", str(temp_settings), "--interactive"], expected=3, input_text=duplicate_identity_input)
    assert_contains(duplicate_identity_result.stdout, "duplicate_login: true", "duplicate identity add-server output")
    assert_contains(duplicate_identity_result.stdout, "add_server_status: cancelled", "duplicate identity add-server output")
    if before_duplicate_identity != json.dumps(load_ref(missing_list), sort_keys=True):
        raise ValidationError("duplicate host+username+port add-server attempt changed the server list")

    managed_config = load_ref(missing_list)

    before = json.dumps(managed_config, sort_keys=True)
    duplicate_input = "server_1\nanother\nhost.example\n22\nuser\nkey\n~/workspace\ny\n\n\n\ny\n"
    run_tool(["add-server", "--settings", str(temp_settings), "--interactive"], expected=1, input_text=duplicate_input)
    after_duplicate = json.dumps(load_ref(missing_list), sort_keys=True)
    if before != after_duplicate:
        raise ValidationError("duplicate add-server attempt changed the server list")

    invalid_port_input = "server_2\nserver_2\nhost.example\n0\nuser\nkey\n~/workspace\ny\n\n\n\ny\n"
    run_tool(["add-server", "--settings", str(temp_settings), "--interactive"], expected=1, input_text=invalid_port_input)
    if before != json.dumps(load_ref(missing_list), sort_keys=True):
        raise ValidationError("invalid port add-server attempt changed the server list")

    empty_host_input = "server_2\nserver_2\n\n22\nuser\nkey\n~/workspace\ny\n\n\n\ny\n"
    run_tool(["add-server", "--settings", str(temp_settings), "--interactive"], expected=1, input_text=empty_host_input)
    if before != json.dumps(load_ref(missing_list), sort_keys=True):
        raise ValidationError("empty host add-server attempt changed the server list")

    disabled_list = tmp_dir / "managed-disabled" / "server_list.local.json"
    disabled_settings = copy_settings_with_server_list(settings, tmp_dir / "managed-disabled-settings.json", disabled_list)
    run_tool(["init-config", "--settings", str(disabled_settings)])
    disabled_config = load_ref(disabled_list)
    disabled_config["default_key_dir"] = str(key_dir)
    write_json(disabled_list, disabled_config)
    disabled_input = "disabled_1\nDisabled Fixture\ndisabled.example.invalid\n22\ncodex\nid_remote_validation\n~/workspace\nn\n\n\ndisabled note\ny\n"
    disabled_add = run_tool(["add-server", "--settings", str(disabled_settings), "--interactive"], input_text=disabled_input)
    assert_contains(disabled_add.stdout, "server_record_saved: disabled_1", "disabled add-server output")
    assert_contains(disabled_add.stdout, "software_scan_status: skipped", "disabled add-server output")
    disabled_written = load_ref(disabled_list)
    if disabled_written["servers"][0]["enabled"] is not False:
        raise ValidationError(f"disabled add-server did not preserve enabled false: {disabled_written}")

    cancel_list = tmp_dir / "managed-cancel" / "server_list.local.json"
    cancel_settings = copy_settings_with_server_list(settings, tmp_dir / "managed-cancel-settings.json", cancel_list)
    run_tool(["init-config", "--settings", str(cancel_settings)])
    cancel_config = load_ref(cancel_list)
    cancel_config["default_key_dir"] = str(key_dir)
    write_json(cancel_list, cancel_config)
    before_cancel = json.dumps(load_ref(cancel_list), sort_keys=True)
    cancel_input = "cancel_add\nCancel Add\ncancel.example.invalid\n22\ncodex\nid_remote_validation\n~/workspace\ny\n\n\ncancel note\nn\n"
    cancel_add = run_tool(["add-server", "--settings", str(cancel_settings), "--interactive"], expected=3, input_text=cancel_input)
    assert_contains(cancel_add.stdout, "add_server_status: cancelled", "cancelled add-server output")
    if before_cancel != json.dumps(load_ref(cancel_list), sort_keys=True):
        raise ValidationError("add-server summary cancellation should leave the server list unchanged")


def ssh_config_fallback_tests(settings: dict[str, Any], tmp_dir: Path) -> None:
    missing_list = tmp_dir / "ssh-config-fallback" / "missing-server-list.json"
    ssh_config = tmp_dir / "ssh-config-fallback" / "ssh_config"
    included_config = tmp_dir / "ssh-config-fallback" / "included_config"
    ssh_config.parent.mkdir(parents=True, exist_ok=True)
    included_config.write_text(
        "\n".join(
            [
                "Host included-alias",
                "  HostName included-secret.example.invalid",
                "  User included-user",
                "  IdentityFile \"~/.ssh/id included\"",
                "",
            ]
        ),
        encoding="utf-8",
    )
    ssh_config.write_text(
        "\n".join(
            [
                f"Include {included_config}",
                "Host fpga-u55c",
                "  HostName \"secret-host.example.invalid\" # inline comment",
                "  User \"secret-user\"",
                "  Port 2200 # inline comment",
                "  IdentityFile ~/.ssh/id_secret # inline comment",
                "",
                "Host *",
                "  ForwardAgent no",
                "",
                "Host pattern-?",
                "  HostName pattern.example.invalid",
                "",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    temp_settings = copy_settings_with_server_list(settings, tmp_dir / "ssh-config-fallback-settings.json", missing_list)
    settings_copy = load_ref(temp_settings)
    settings_copy.setdefault("ssh", {})["config_path"] = str(ssh_config)
    settings_copy["tools"]["ssh_client"] = str(create_fake_ssh(tmp_dir / "ssh-config-fallback-fake-ssh", output="alias ok\n"))
    write_json(temp_settings, settings_copy)

    discover_result = run_tool(["discover", "--settings", str(temp_settings)], expected=3)
    assert_contains(discover_result.stdout, "status: not_configured", "ssh-config fallback discover output")
    assert_contains(discover_result.stdout, "ssh_config_fallback_available: true", "ssh-config fallback discover output")
    assert_contains(discover_result.stdout, "ssh_config_alias_count: 2", "ssh-config fallback discover output")
    assert_not_contains(discover_result.stdout, "secret-host.example.invalid", "ssh-config fallback discover output")
    assert_not_contains(discover_result.stdout, "secret-user", "ssh-config fallback discover output")
    assert_not_contains(discover_result.stdout, "included-secret.example.invalid", "ssh-config fallback discover output")

    discover_json = run_tool(["discover", "--settings", str(temp_settings), "--json"], expected=3)
    discover_data = json.loads(discover_json.stdout)
    if discover_data.get("status") != "not_configured" or discover_data.get("ssh_config_alias_count") != 2:
        raise ValidationError(f"unexpected ssh-config fallback discover json: {discover_json.stdout}")
    assert_not_contains(discover_json.stdout, "secret-host.example.invalid", "ssh-config fallback discover json")
    assert_not_contains(discover_json.stdout, "secret-user", "ssh-config fallback discover json")
    assert_not_contains(discover_json.stdout, "included-secret.example.invalid", "ssh-config fallback discover json")

    alias_result = run_tool(["ssh-config-discover", "--settings", str(temp_settings)])
    assert_contains(alias_result.stdout, "status: available", "ssh-config-discover output")
    assert_contains(alias_result.stdout, "alias: fpga-u55c", "ssh-config-discover output")
    assert_contains(alias_result.stdout, "alias: included-alias", "ssh-config-discover output")
    assert_not_contains(alias_result.stdout, "pattern-?", "ssh-config-discover output")
    assert_not_contains(alias_result.stdout, "secret-host.example.invalid", "ssh-config-discover output")
    assert_not_contains(alias_result.stdout, "secret-user", "ssh-config-discover output")
    assert_not_contains(alias_result.stdout, "included-secret.example.invalid", "ssh-config-discover output")

    alias_sensitive = run_tool(["ssh-config-discover", "--settings", str(temp_settings), "--show-sensitive"])
    assert_contains(alias_sensitive.stdout, "hostname: secret-host.example.invalid", "ssh-config-discover sensitive output")
    assert_contains(alias_sensitive.stdout, "user: secret-user", "ssh-config-discover sensitive output")
    assert_contains(alias_sensitive.stdout, "port: 2200", "ssh-config-discover sensitive output")
    assert_contains(alias_sensitive.stdout, "hostname: included-secret.example.invalid", "ssh-config-discover sensitive output")
    assert_contains(alias_sensitive.stdout, "identity_file: ~/.ssh/id included", "ssh-config-discover sensitive output")

    command_result = run_tool(["command", "--settings", str(temp_settings), "--ssh-alias", "fpga-u55c"])
    assert_contains(command_result.stdout, "ssh", "ssh-alias command output")
    assert_contains(command_result.stdout, "fpga-u55c", "ssh-alias command output")
    assert_not_contains(command_result.stdout, "secret-host.example.invalid", "ssh-alias command output")
    assert_not_contains(command_result.stdout, "secret-user", "ssh-alias command output")

    exec_result = run_tool(["exec", "--settings", str(temp_settings), "--ssh-alias", "fpga-u55c", "--", "echo", "ok"])
    assert_contains(exec_result.stdout, "alias ok", "ssh-alias exec output")
    if missing_list.exists():
        raise ValidationError("ssh-config fallback commands must not create a server list")


def cmd_guidance_tests(settings: dict[str, Any], settings_path: Path, server_list: Path, ref_config: dict, tmp_dir: Path) -> None:
    base = tool_base_args(settings_path, server_list)
    server_id = clone_first_server(ref_config)["id"]
    exec_result = run_tool(["exec", *base, "--server", server_id, "--cmd", "echo ok"], expected=2)
    assert_contains(exec_result.stderr, "Use: remote_ssh.py exec ... -- <remote command>", "exec --cmd guidance")
    assert_contains(exec_result.stderr, "received unsupported --cmd", "exec --cmd guidance")

    request_result = run_tool(["request-command", *base, "--server", server_id, "--reason", "validation", "--cmd", "echo ok"], expected=2)
    assert_contains(request_result.stderr, "Use: remote_ssh.py request-command ... -- <remote command>", "request-command --cmd guidance")

    literal_cmd = run_tool(["request-command", *base, "--server", server_id, "--reason", "literal flag", "--", "--cmd", "echo"])
    for field in ["status", "server_id", "server_name", "workdir_status", "software_cache_status", "message", "next_action"]:
        require_output_field(literal_cmd.stdout, field, "request-command output")
    request = request_from_output(literal_cmd.stdout)
    if request.get("payload", {}).get("command") != "--cmd echo":
        raise ValidationError(f"--cmd after -- should remain part of the remote command: {request}")

    literal_settings = copy_settings_with_server_list(settings, tmp_dir / "literal-cmd-settings.json", server_list)
    literal_settings_copy = load_ref(literal_settings)
    literal_settings_copy["tools"]["ssh_client"] = str(create_fake_ssh(tmp_dir / "literal-cmd-fake-ssh", output="ran\n"))
    write_json(literal_settings, literal_settings_copy)
    literal_exec = run_tool(["exec", "--settings", str(literal_settings), "--server", server_id, "--", "printf", "--cmd %s", "ok"])
    assert_contains(literal_exec.stdout, "ran", "exec literal --cmd output")


def detached_job_tests(settings: dict[str, Any], tmp_dir: Path) -> None:
    server_list = create_validation_server_list(tmp_dir / "detached-jobs")
    config = load_ref(server_list)
    server_id = config["servers"][0]["id"]
    requests_root = tmp_dir / "detached-requests"
    jobs_root = tmp_dir / "detached-local-jobs"
    temp_settings = copy_settings_with_server_list(settings, tmp_dir / "detached-settings.json", server_list, requests_dir=requests_root)
    settings_copy = load_ref(temp_settings)
    settings_copy.setdefault("jobs", {})["local_dir"] = str(jobs_root)
    settings_copy["jobs"]["remote_dir"] = ".erie-remote-ssh/jobs"
    settings_copy["jobs"]["default_tail_lines"] = 2
    settings_copy["tools"]["ssh_client"] = str(
        create_sequence_fake_ssh(
            tmp_dir / "detached-fake-ssh",
            [
                {"stdout": "job_id: job-abc\nremote_job_dir: ~/workspace/.erie-remote-ssh/jobs/job-abc\npid: 12345\nstatus: started\n", "returncode": 0},
                {"stdout": "status: running\npid: 12345\nexit_code:\n", "returncode": 0},
                {"stdout": "line two\nline three\n", "returncode": 0},
                {"stdout": "ok\n", "returncode": 0},
                {"stdout": "job_id: job-def\nremote_job_dir: ~/workspace/.erie-remote-ssh/jobs/job-def\npid: 12346\nstatus: started\n", "returncode": 0},
                {"stdout": "status: succeeded\npid: 12346\nexit_code: 0\n", "returncode": 0},
                {"stdout": "status: not_found\npid:\nexit_code:\n", "returncode": 0},
            ],
        )
    )
    write_json(temp_settings, settings_copy)

    start_result = run_tool(["exec-detached", "--settings", str(temp_settings), "--server", server_id, "--reason", "long validation", "--", "sleep", "30"])
    assert_contains(start_result.stdout, "status: started", "exec-detached output")
    assert_contains(start_result.stdout, "job_id: job-abc", "exec-detached output")
    assert_contains(start_result.stdout, "risk_category: background/detached", "exec-detached output")
    for field in ["server_id", "server_name", "workdir_status", "software_cache_status", "message", "next_action"]:
        require_output_field(start_result.stdout, field, "exec-detached output")
    manifest = jobs_root / "job-abc.json"
    if not manifest.exists():
        raise ValidationError("exec-detached did not write a local job manifest")
    manifest_data = load_ref(manifest)
    if manifest_data.get("server") != server_id or manifest_data.get("command") != "sleep 30":
        raise ValidationError(f"unexpected exec-detached manifest: {manifest_data}")

    status_result = run_tool(["status", "--settings", str(temp_settings), "--job", "job-abc"])
    assert_contains(status_result.stdout, "status: running", "job status output")
    for field in ["server_id", "server_name", "workdir_status", "software_cache_status", "message", "next_action"]:
        require_output_field(status_result.stdout, field, "job status output")

    tail_result = run_tool(["tail-log", "--settings", str(temp_settings), "--job", "job-abc"])
    assert_contains(tail_result.stdout, "line two", "tail-log output")
    assert_contains(tail_result.stdout, "line three", "tail-log output")
    assert_not_contains(tail_result.stdout, "line one", "tail-log output")

    request_result = run_tool(["request-command", "--settings", str(temp_settings), "--server", server_id, "--reason", "detached validation", "--detached", "--", "make", "hw"])
    assert_contains(request_result.stdout, "risk_category: background/detached", "request-command --detached output")
    detached_request = request_from_output(request_result.stdout)
    if detached_request.get("payload", {}).get("detached") is not True:
        raise ValidationError(f"request-command --detached did not mark the request: {detached_request}")

    run_result = run_tool(["run-request", "--settings", str(temp_settings), "--request", str(requests_root / f"{detached_request['request_id']}.json"), "--execute"])
    assert_contains(run_result.stdout, "status: started", "detached run-request output")
    assert_contains(run_result.stdout, "job_id: job-def", "detached run-request output")
    for field in ["server_id", "server_name", "workdir_status", "software_cache_status", "message", "next_action"]:
        require_output_field(run_result.stdout, field, "detached run-request output")

    succeeded = run_tool(["status", "--settings", str(temp_settings), "--server", server_id, "--job", "job-def"])
    assert_contains(succeeded.stdout, "status: succeeded", "succeeded job status output")
    missing = run_tool(["status", "--settings", str(temp_settings), "--server", server_id, "--job", "missing-job"], expected=3)
    assert_contains(missing.stdout, "status: not_found", "missing job status output")


def write_fake_skill_tree(root: Path, marker: str, include_local_server_list: bool = False) -> None:
    files = {
        "SKILL.md": f"---\nname: erie-remote-ssh\ndescription: {marker}\n---\n{marker}\n",
        "VERSION": "9.9.9\n",
        "agents/openai.yaml": f"display_name: {marker}\n",
        "config/defaults.json": '{"version": 1, "marker": "' + marker + '"}\n',
        "config/server_list.template.json": '{"version": 1, "servers": []}\n',
        "references/configuration.md": marker + "\n",
        "references/workflows.md": marker + "\n",
        "references/review-checklist.md": marker + "\n",
        "scripts/remote_ssh.py": f"# {marker}\n",
        "scripts/validate_remote_ssh.py": f"# {marker}\n",
    }
    if include_local_server_list:
        files["config/server_list.local.json"] = '{"sentinel": "source must not install"}\n'
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def install_skill_protection_tests(tmp_dir: Path) -> None:
    installer = SKILL_DIR / "scripts" / "install_skill.py"
    codex_home = tmp_dir / "codex-home"
    target = codex_home / "skills" / "erie-remote-ssh"
    source = tmp_dir / "release-source"
    write_fake_skill_tree(target, "original installed")
    local_config = target / "config" / "server_list.local.json"
    local_backup = target / "config" / "server_list.local.json.bak.20260510"
    report_file = target / "reports" / "requests" / "sentinel.json"
    stale_release_file = target / "references" / "removed-in-new-release.md"
    local_config.write_text('{"sentinel": "preserve local config"}\n', encoding="utf-8")
    local_backup.write_text('{"sentinel": "preserve local backup"}\n', encoding="utf-8")
    report_file.parent.mkdir(parents=True, exist_ok=True)
    report_file.write_text('{"sentinel": "preserve reports"}\n', encoding="utf-8")
    stale_release_file.write_text("old release-only file\n", encoding="utf-8")
    write_fake_skill_tree(source, "new release")

    env = os.environ.copy()
    env["CODEX_HOME"] = str(codex_home)
    result = subprocess.run(
        [sys.executable, str(installer), "--source", str(source), "--target", str(target)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValidationError(f"install_skill.py failed\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    assert_contains(result.stdout, "backup:", "installer output")
    assert_contains(result.stdout, "preserved: config/server_list.local.json", "installer output")
    assert_contains(result.stdout, "preserved_hash_verified: true", "installer output")
    if local_config.read_text(encoding="utf-8") != '{"sentinel": "preserve local config"}\n':
        raise ValidationError("installer overwrote config/server_list.local.json")
    if local_backup.read_text(encoding="utf-8") != '{"sentinel": "preserve local backup"}\n':
        raise ValidationError("installer overwrote server_list.local.json backup")
    if report_file.read_text(encoding="utf-8") != '{"sentinel": "preserve reports"}\n':
        raise ValidationError("installer removed reports content")
    if "new release" not in (target / "SKILL.md").read_text(encoding="utf-8"):
        raise ValidationError("installer did not update release-managed files")
    if stale_release_file.exists():
        raise ValidationError("installer left stale release-managed files from the old target")
    backups = list((codex_home / "skill_backups").glob("erie-remote-ssh-*"))
    if not backups:
        raise ValidationError("installer did not create a backup under CODEX_HOME/skill_backups")
    backup_config = backups[0] / "config" / "server_list.local.json"
    if backup_config.read_text(encoding="utf-8") != '{"sentinel": "preserve local config"}\n':
        raise ValidationError("installer backup did not contain the original local server list")

    bad_source = tmp_dir / "bad-source"
    write_fake_skill_tree(bad_source, "bad release", include_local_server_list=True)
    before_reject = local_config.read_bytes()
    reject = subprocess.run(
        [sys.executable, str(installer), "--source", str(bad_source), "--target", str(target)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if reject.returncode == 0:
        raise ValidationError("installer should reject a source containing config/server_list.local.json")
    assert_contains(reject.stderr, "Refusing to install protected local file", "installer protected file rejection")
    if local_config.read_bytes() != before_reject:
        raise ValidationError("rejected install changed config/server_list.local.json")

    runtime_source = tmp_dir / "runtime-source"
    write_fake_skill_tree(runtime_source, "runtime release")
    runtime_file = runtime_source / "reports" / "requests" / "source-runtime.json"
    runtime_file.parent.mkdir(parents=True, exist_ok=True)
    runtime_file.write_text('{"sentinel": "source runtime must not install"}\n', encoding="utf-8")
    before_runtime_reject = local_config.read_bytes()
    runtime_reject = subprocess.run(
        [sys.executable, str(installer), "--source", str(runtime_source), "--target", str(target)],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    if runtime_reject.returncode == 0:
        raise ValidationError("installer should reject a source containing reports runtime files")
    assert_contains(runtime_reject.stderr, "Refusing to install runtime artifact", "installer runtime source rejection")
    assert_not_contains(runtime_reject.stderr, "Traceback", "installer runtime source rejection")
    if local_config.read_bytes() != before_runtime_reject:
        raise ValidationError("runtime source rejection changed config/server_list.local.json")

    backup_block_home = tmp_dir / "backup-block-codex-home"
    backup_block_target = backup_block_home / "skills" / "erie-remote-ssh"
    backup_block_source = tmp_dir / "backup-block-source"
    write_fake_skill_tree(backup_block_target, "backup block original")
    write_fake_skill_tree(backup_block_source, "backup block new release")
    backup_block_config = backup_block_target / "config" / "server_list.local.json"
    backup_block_config.write_text('{"sentinel": "backup block local config"}\n', encoding="utf-8")
    backup_block_root = backup_block_home / "skill_backups"
    backup_block_root.parent.mkdir(parents=True, exist_ok=True)
    backup_block_root.write_text("path blocks backup directory\n", encoding="utf-8")
    backup_block_env = os.environ.copy()
    backup_block_env["CODEX_HOME"] = str(backup_block_home)
    backup_block = subprocess.run(
        [sys.executable, str(installer), "--source", str(backup_block_source), "--target", str(backup_block_target)],
        cwd=ROOT,
        env=backup_block_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if backup_block.returncode == 0:
        raise ValidationError("installer should fail cleanly when skill_backups is not a directory")
    assert_contains(backup_block.stderr, "Install failed", "installer backup directory error")
    assert_not_contains(backup_block.stderr, "Traceback", "installer backup directory error")
    if "backup block original" not in (backup_block_target / "SKILL.md").read_text(encoding="utf-8"):
        raise ValidationError("backup directory failure changed release-managed files")
    if backup_block_config.read_text(encoding="utf-8") != '{"sentinel": "backup block local config"}\n':
        raise ValidationError("backup directory failure changed protected local config")

    rollback_home = tmp_dir / "rollback-codex-home"
    rollback_target = rollback_home / "skills" / "erie-remote-ssh"
    rollback_source = tmp_dir / "rollback-source"
    write_fake_skill_tree(rollback_target, "rollback original")
    rollback_config = rollback_target / "config" / "server_list.local.json"
    rollback_report = rollback_target / "reports" / "requests" / "rollback.json"
    rollback_config.write_text('{"sentinel": "rollback local config"}\n', encoding="utf-8")
    rollback_report.parent.mkdir(parents=True, exist_ok=True)
    rollback_report.write_text('{"sentinel": "rollback reports"}\n', encoding="utf-8")
    write_fake_skill_tree(rollback_source, "rollback new release")
    blocking_dir = rollback_source / "config" / "server_list.local.json"
    blocking_dir.mkdir()
    (blocking_dir / "source-owned.txt").write_text("source path blocks protected config migration\n", encoding="utf-8")
    rollback_env = os.environ.copy()
    rollback_env["CODEX_HOME"] = str(rollback_home)
    rollback = subprocess.run(
        [sys.executable, str(installer), "--source", str(rollback_source), "--target", str(rollback_target)],
        cwd=ROOT,
        env=rollback_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if rollback.returncode == 0:
        raise ValidationError("installer should fail when a target path blocks release file copy")
    assert_contains(rollback.stderr, "Install failed", "installer rollback error")
    assert_not_contains(rollback.stderr, "Traceback", "installer rollback error")
    if "rollback original" not in (rollback_target / "SKILL.md").read_text(encoding="utf-8"):
        raise ValidationError("failed install did not roll back release-managed files")
    if rollback_config.read_text(encoding="utf-8") != '{"sentinel": "rollback local config"}\n':
        raise ValidationError("failed install changed protected local config")
    if rollback_report.read_text(encoding="utf-8") != '{"sentinel": "rollback reports"}\n':
        raise ValidationError("failed install changed protected reports")


def configuration_gate_tests(settings: dict[str, Any], tmp_dir: Path) -> None:
    gate_list = tmp_dir / "gate" / "server_list.local.json"
    gate_settings = copy_settings_with_server_list(settings, tmp_dir / "gate-settings.json", gate_list)
    gate_settings_data = load_ref(gate_settings)
    key_dir = tmp_dir / "gate-keys"
    gate_settings_data["tools"]["ssh_client"] = str(create_fake_ssh(tmp_dir))
    gate_settings_data["tools"]["ssh_keygen"] = str(create_fake_keygen(tmp_dir))
    gate_settings_data["inventory"] = compact_scan_inventory()
    write_json(gate_settings, gate_settings_data)

    manual_result = run_tool(["configure", "--settings", str(gate_settings), "--interactive"], input_text="\nmanual\n")
    assert_contains(manual_result.stdout, "configuration_mode: manual", "configure manual output")
    assert_contains(manual_result.stdout, "manual:", "configure manual output")
    if gate_list.exists():
        raise ValidationError("blank configuration mode must not default to script or create a server list")

    cancel_result = run_tool(["configure", "--settings", str(gate_settings), "--interactive"], expected=3, input_text="cancel\n")
    assert_contains(cancel_result.stdout, "configuration_status: cancelled", "configure cancel output")

    run_tool(["init-config", "--settings", str(gate_settings)])
    config = load_ref(gate_list)
    config["default_key_dir"] = str(key_dir)
    write_json(gate_list, config)

    add_input = "script\n\n\nmissing.example.internal\n\nremote_user_sensitive\nid_missing_key\n\n\nGate\nvalidation setup\n\ny\ngenerate\ny\nempty\n"
    add_result = run_tool(["configure", "--settings", str(gate_settings), "--interactive"], input_text=add_input)
    assert_contains(add_result.stdout, "configuration_mode: script", "configure script output")
    assert_contains(add_result.stdout, f"key_generation_target: {key_dir / 'id_missing_key'}", "configure script output")
    assert_contains(add_result.stdout, "key_generation: created", "configure script output")
    assert_contains(add_result.stdout, "manual_login_required: true", "configure script output")
    assert_contains(add_result.stdout, "authorized_keys", "configure script output")
    assert_contains(add_result.stdout, "added: server_1", "configure script output")
    assert_not_contains(add_result.stdout, "missing.example.internal", "configure script output")
    assert_not_contains(add_result.stdout, "remote_user_sensitive", "configure script output")
    if not (key_dir / "id_missing_key").exists() or not (key_dir / "id_missing_key.pub").exists():
        raise ValidationError("configure script key generation did not create private/public key files")

    before_action_cancel = json.dumps(load_ref(gate_list), sort_keys=True)
    action_cancel = run_tool(
        ["configure", "--settings", str(gate_settings), "--interactive"],
        expected=3,
        input_text="script\n\ncancel\n",
    )
    assert_contains(action_cancel.stdout, "configuration_status: cancelled", "configure action cancel output")
    if before_action_cancel != json.dumps(load_ref(gate_list), sort_keys=True):
        raise ValidationError("blank configuration action must not default to add or modify the server list")

    server_mode_cancel = run_tool(
        ["configure", "--settings", str(gate_settings), "--server", "server_1", "--interactive"],
        expected=3,
        input_text="cancel\n",
    )
    assert_contains(server_mode_cancel.stdout, "configuration_status: cancelled", "configure --server cancel output")

    update_input = "all\nUpdated Gate Server\nupdated.example.internal\n52026\nyanghy\nid_updated_key\n~/updated-workspace\ny\nUpdated Category\nVivado synthesis; Vitis validation\nupdated note\ny\ngenerate\ny\nempty\n"
    update_result = run_tool(
        ["update-server", "--settings", str(gate_settings), "--server", "server_1", "--interactive"],
        input_text=update_input,
    )
    assert_contains(update_result.stdout, "field_menu:", "update-server output")
    assert_contains(update_result.stdout, "server_record_summary:", "update-server output")
    assert_contains(update_result.stdout, "save_server_record", "update-server output")
    assert_contains(update_result.stdout, f"key_generation_target: {key_dir / 'id_updated_key'}", "update-server output")
    assert_contains(update_result.stdout, "updated: server_1", "update-server output")
    assert_contains(update_result.stdout, "key_generation: created", "update-server output")
    updated = load_ref(gate_list)
    server = updated["servers"][0]
    expected = {
        "name": "Updated Gate Server",
        "host": "updated.example.internal",
        "port": 52026,
        "username": "yanghy",
        "key_name": "id_updated_key",
        "workdir": "~/updated-workspace",
        "enabled": True,
        "category": "Updated Category",
        "functions": ["Vivado synthesis", "Vitis validation"],
        "notes": "updated note",
    }
    for key, value in expected.items():
        if server.get(key) != value:
            raise ValidationError(f"update-server did not write {key}: {updated}")
    if not list(gate_list.parent.glob("server_list.local.json.bak.*")):
        raise ValidationError("update-server should create a backup")

    updated["servers"][0]["validation"] = {
        "status": "verified",
        "method": "ssh_workspace",
        "verified_at": "2026-01-01T00:00:00Z",
        "last_error": None,
    }
    updated["servers"][0]["workspace_check"] = {
        "status": "ok",
        "checked_at": "2026-01-01T00:00:00Z",
        "message": "metadata preserve fixture",
    }
    updated["servers"][0]["software_scan"] = {"status": "ok", "tools": {"python": {"status": "installed"}}}
    write_json(gate_list, updated)
    metadata_update = run_tool(
        ["configure", "--settings", str(gate_settings), "--interactive"],
        input_text="script\nupdate\n1\nshow\nnotes\nmetadata only note\ndone\ny\n",
    )
    assert_contains(metadata_update.stdout, "configured_servers:", "configure numbered update output")
    assert_contains(metadata_update.stdout, "[1]", "configure numbered update output")
    assert_contains(metadata_update.stdout, "field_menu:", "configure numbered update output")
    metadata_written = load_ref(gate_list)["servers"][0]
    if metadata_written.get("notes") != "metadata only note":
        raise ValidationError(f"field-menu update did not change selected field: {metadata_written}")
    if metadata_written.get("host") != "updated.example.internal" or metadata_written.get("port") != 52026:
        raise ValidationError(f"field-menu update changed unselected connection fields: {metadata_written}")
    if metadata_written.get("validation", {}).get("status") != "verified" or metadata_written.get("workspace_check", {}).get("status") != "ok":
        raise ValidationError(f"metadata-only update should preserve validation/workspace caches: {metadata_written}")

    before_save_cancel = json.dumps(load_ref(gate_list), sort_keys=True)
    save_cancel = run_tool(
        ["update-server", "--settings", str(gate_settings), "--server", "server_1", "--interactive"],
        expected=3,
        input_text="notes\ntransient note\ndone\nn\n",
    )
    assert_contains(save_cancel.stdout, "server_record_summary:", "update-server save cancel output")
    assert_contains(save_cancel.stdout, "configuration_status: cancelled", "update-server save cancel output")
    if before_save_cancel != json.dumps(load_ref(gate_list), sort_keys=True):
        raise ValidationError("update-server save cancellation should leave the server list unchanged")

    missing_metadata = load_ref(gate_list)
    missing_metadata["servers"][0]["key_name"] = "missing_metadata_key"
    missing_metadata["servers"][0]["enabled"] = True
    missing_metadata["servers"][0]["validation"] = {
        "status": "verified",
        "method": "ssh_workspace",
        "verified_at": "2026-01-01T00:00:00Z",
        "last_error": None,
    }
    missing_metadata["servers"][0]["workspace_check"] = {
        "status": "ok",
        "checked_at": "2026-01-01T00:00:00Z",
        "message": "missing key metadata fixture",
    }
    missing_metadata["servers"][0]["software_scan"] = {"status": "ok", "tools": {"python": {"status": "installed"}}}
    write_json(gate_list, missing_metadata)
    missing_metadata_update = run_tool(
        ["update-server", "--settings", str(gate_settings), "--server", "server_1", "--interactive"],
        input_text="notes\nmetadata survives missing key\ndone\ny\n",
    )
    assert_contains(missing_metadata_update.stdout, "updated: server_1", "metadata-only missing key update output")
    assert_not_contains(missing_metadata_update.stdout, "missing_key_action", "metadata-only missing key update output")
    missing_metadata_written = load_ref(gate_list)["servers"][0]
    if missing_metadata_written.get("notes") != "metadata survives missing key":
        raise ValidationError(f"metadata-only update with missing key did not write notes: {missing_metadata_written}")
    if missing_metadata_written.get("enabled") is not True:
        raise ValidationError(f"metadata-only update with missing key changed enabled: {missing_metadata_written}")
    if missing_metadata_written.get("validation", {}).get("status") != "verified":
        raise ValidationError(f"metadata-only update with missing key should preserve validation cache: {missing_metadata_written}")

    before_menu_cancel = json.dumps(load_ref(gate_list), sort_keys=True)
    menu_cancel = run_tool(
        ["update-server", "--settings", str(gate_settings), "--server", "server_1", "--interactive"],
        expected=3,
        input_text="name\nShould Not Persist\ncancel\n",
    )
    assert_contains(menu_cancel.stdout, "configuration_status: cancelled", "update-server menu cancel output")
    if before_menu_cancel != json.dumps(load_ref(gate_list), sort_keys=True):
        raise ValidationError("update-server menu cancel should leave the server list unchanged")

    disabled_list = tmp_dir / "gate-disabled" / "server_list.local.json"
    disabled_settings = copy_settings_with_server_list(settings, tmp_dir / "gate-disabled-settings.json", disabled_list)
    disabled_settings_data = load_ref(disabled_settings)
    disabled_settings_data["tools"]["ssh_client"] = str(create_fake_ssh(tmp_dir))
    disabled_settings_data["tools"]["ssh_keygen"] = str(create_fake_keygen(tmp_dir))
    write_json(disabled_settings, disabled_settings_data)
    run_tool(["init-config", "--settings", str(disabled_settings)])
    disabled_config = load_ref(disabled_list)
    disabled_config["default_key_dir"] = str(tmp_dir / "gate-disabled-keys")
    write_json(disabled_list, disabled_config)
    disabled_input = "disabled_missing\nDisabled Missing\nmissing-key.example.internal\n22\ncodex\nid_disabled_missing\n~/workspace\ny\n\n\nmissing key\ny\ndisable\n"
    disabled_result = run_tool(["add-server", "--settings", str(disabled_settings), "--interactive"], input_text=disabled_input)
    assert_contains(disabled_result.stdout, "key_generation: skipped", "add-server missing key disable output")
    assert_contains(disabled_result.stdout, "server_record_saved: disabled_missing", "add-server missing key disable output")
    disabled_written = load_ref(disabled_list)
    if disabled_written["servers"][0]["enabled"] is not False:
        raise ValidationError(f"missing-key disable branch should save a disabled server: {disabled_written}")

    cancel_list = tmp_dir / "gate-cancel" / "server_list.local.json"
    cancel_settings = copy_settings_with_server_list(settings, tmp_dir / "gate-cancel-settings.json", cancel_list)
    cancel_settings_data = load_ref(cancel_settings)
    cancel_settings_data["tools"]["ssh_client"] = str(create_fake_ssh(tmp_dir))
    cancel_settings_data["tools"]["ssh_keygen"] = str(create_fake_keygen(tmp_dir))
    write_json(cancel_settings, cancel_settings_data)
    run_tool(["init-config", "--settings", str(cancel_settings)])
    cancel_config = load_ref(cancel_list)
    cancel_config["default_key_dir"] = str(tmp_dir / "gate-cancel-keys")
    write_json(cancel_list, cancel_config)
    before_missing_cancel = json.dumps(load_ref(cancel_list), sort_keys=True)
    cancel_input = "cancel_missing\nCancel Missing\nmissing-key.example.internal\n22\ncodex\nid_cancel_missing\n~/workspace\ny\n\n\nmissing key\ny\ncancel\n"
    missing_cancel = run_tool(["add-server", "--settings", str(cancel_settings), "--interactive"], expected=3, input_text=cancel_input)
    assert_contains(missing_cancel.stdout, "configuration_status: cancelled", "add-server missing key cancel output")
    if before_missing_cancel != json.dumps(load_ref(cancel_list), sort_keys=True):
        raise ValidationError("missing-key cancel branch should leave the server list unchanged")


def request_and_path_tests(settings: dict[str, Any], settings_path: Path, server_list: Path, ref_config: dict, tmp_dir: Path) -> None:
    requests_root = tmp_dir / "requests"
    downloads_root = tmp_dir / "downloads"
    temp_settings = copy_settings_with_server_list(
        settings,
        tmp_dir / "request-settings.json",
        server_list,
        requests_dir=requests_root,
        downloads_dir=downloads_root,
        upload_roots=[str(tmp_dir)],
    )
    positive_server = validation_name(settings, "positive_server", clone_first_server(ref_config)["id"])
    local_source = tmp_dir / "upload-source.txt"
    local_source.write_text("remote ssh validation upload\n", encoding="utf-8")

    base = ["--settings", str(temp_settings), "--server", positive_server]
    upload_result = run_tool(
        [
            "request-upload",
            *base,
            "--local",
            str(local_source),
            "--remote",
            "validation/upload-source.txt",
            "--reason",
            "validation",
        ]
    )
    assert_contains(upload_result.stdout, "operation: upload", "request-upload output")
    assert_redacted(upload_result.stdout, ref_config, "request-upload output")
    upload_request = request_from_output(upload_result.stdout)
    assert upload_request["operation"] == "upload"
    assert upload_request["payload"]["remote_path"] == "validation/upload-source.txt"
    if "local_upload_root" not in upload_request["payload"] or "local_relative_path" not in upload_request["payload"]:
        raise ValidationError(f"upload request must record upload root and relative path: {upload_request}")
    assert_redacted(json.dumps(upload_request), ref_config, "upload request json")

    external_root = Path(tempfile.mkdtemp(prefix="erie-upload-root-")).resolve()
    try:
        external_source = external_root / "data" / "external-upload.txt"
        external_source.parent.mkdir(parents=True)
        external_source.write_text("external upload root validation\n", encoding="utf-8")
        external_settings = copy_settings_with_server_list(
            settings,
            tmp_dir / "external-upload-settings.json",
            server_list,
            requests_dir=tmp_dir / "external-requests",
            downloads_dir=downloads_root,
            upload_roots=[str(external_root)],
        )
        external_result = run_tool(
            [
                "request-upload",
                "--settings",
                str(external_settings),
                "--server",
                positive_server,
                "--local",
                str(external_source),
                "--remote",
                "validation/external-upload.txt",
                "--reason",
                "validation",
            ]
        )
        assert_contains(external_result.stdout, "operation: upload", "external upload root output")
        external_request = request_from_output(external_result.stdout)
        if external_request["payload"].get("local_upload_root") != external_root.as_posix():
            raise ValidationError(f"external upload root was not recorded: {external_request}")
        if external_request["payload"].get("local_relative_path") != "data/external-upload.txt":
            raise ValidationError(f"external upload relative path was not recorded: {external_request}")

        cwd_settings = copy_settings_with_server_list(
            settings,
            tmp_dir / "cwd-upload-settings.json",
            server_list,
            requests_dir=tmp_dir / "cwd-requests",
            downloads_dir=downloads_root,
            upload_roots=["${cwd}"],
        )
        cwd_result = run_tool(
            [
                "request-upload",
                "--settings",
                str(cwd_settings),
                "--server",
                positive_server,
                "--local",
                str(local_source),
                "--remote",
                "validation/cwd-upload.txt",
                "--reason",
                "validation",
            ],
            cwd=tmp_dir,
        )
        assert_contains(cwd_result.stdout, "operation: upload", "cwd upload root output")
    finally:
        shutil.rmtree(external_root, ignore_errors=True)

    outside_settings = copy_settings_with_server_list(
        settings,
        tmp_dir / "outside-upload-settings.json",
        server_list,
        requests_dir=tmp_dir / "outside-requests",
        downloads_dir=downloads_root,
        upload_roots=[str(tmp_dir / "one-root")],
    )
    outside_result = run_tool(
        ["request-upload", "--settings", str(outside_settings), "--server", positive_server, "--local", str(local_source), "--remote", "validation/outside.txt", "--reason", "validation"],
        expected=1,
    )
    assert_contains(outside_result.stderr, "paths.upload_roots", "outside upload root error")

    sensitive_source = tmp_dir / ".codex" / "secret.txt"
    sensitive_source.parent.mkdir(parents=True, exist_ok=True)
    sensitive_source.write_text("sensitive upload validation\n", encoding="utf-8")
    sensitive_before_count = len(list(requests_root.glob("*.json")))
    sensitive_blocked = run_tool(
        ["request-upload", *base, "--local", str(sensitive_source), "--remote", "validation/secret.txt", "--reason", "validation"],
        expected=1,
    )
    assert_contains(sensitive_blocked.stderr, "--confirm-sensitive-local-upload", "sensitive upload without confirmation")
    if sensitive_before_count != len(list(requests_root.glob("*.json"))):
        raise ValidationError("blocked sensitive upload should not create a request file")

    sensitive_confirmed = run_tool(
        [
            "request-upload",
            *base,
            "--local",
            str(sensitive_source),
            "--remote",
            "validation/secret.txt",
            "--reason",
            "validation",
            "--confirm-sensitive-local-upload",
        ]
    )
    assert_contains(sensitive_confirmed.stdout, "risk: sensitive local upload", "sensitive upload confirmation output")
    sensitive_request = request_from_output(sensitive_confirmed.stdout)
    assert_contains(json.dumps(sensitive_request), "sensitive local upload", "sensitive request json")
    sensitive_run = run_tool(
        ["run-request", "--settings", str(temp_settings), "--request", str(requests_root / f"{sensitive_request['request_id']}.json"), "--execute"],
        expected=1,
    )
    assert_contains(sensitive_run.stderr, "--confirm-sensitive-local-upload", "sensitive run-request without confirmation")

    tampered_request_path = requests_root / f"{sensitive_request['request_id']}.json"
    tampered_request = load_ref(tampered_request_path)
    tampered_request["payload"]["local_upload_root"] = Path.home().as_posix()
    tampered_request["payload"]["local_relative_path"] = "outside.txt"
    write_json(tampered_request_path, tampered_request)
    tampered_result = run_tool(
        [
            "run-request",
            "--settings",
            str(temp_settings),
            "--request",
            str(tampered_request_path),
            "--execute",
            "--confirm-sensitive-local-upload",
        ],
        expected=1,
    )
    assert_contains(tampered_result.stderr, "paths.upload_roots", "tampered upload request error")

    delete_result = run_tool(["request-delete", *base, "--path", "validation/upload-source.txt", "--reason", "validation"])
    assert_contains(delete_result.stdout, "operation: delete", "request-delete output")
    delete_request = request_from_output(delete_result.stdout)
    if delete_request["payload"].get("recursive") is not False:
        raise ValidationError("request-delete without --recursive did not record recursive=false")

    command_result = run_tool(["request-command", *base, "--reason", "validation", "--", "rm", "-rf", "/tmp/example"])
    assert_contains(command_result.stdout, "risk_category: destructive", "request-command output")
    assert_contains(command_result.stdout, "risk_category: path-sensitive", "request-command output")

    run_tool(["run-request", "--settings", str(temp_settings), "--request", str(requests_root / f"{delete_request['request_id']}.json")], expected=1)
    before_count = len(list(requests_root.glob("*.json")))
    run_tool(["request-mkdir", *base, "--path", "../escape"], expected=1)
    run_tool(["request-mkdir", *base, "--path", "/absolute"], expected=1)
    run_tool(["request-mkdir", *base, "--path", "C:/absolute"], expected=1)
    run_tool(["file-list", *base, "--path", "../escape"], expected=1)
    run_tool(["file-download", *base, "--remote", "../escape", "--local", "x.txt"], expected=1)
    run_tool(["request-upload", *base, "--local", str(Path.home()), "--remote", "home"], expected=1)
    if before_count != len(list(requests_root.glob("*.json"))):
        raise ValidationError("invalid path request changed request file count")


def risk_classification_tests() -> None:
    classifier = getattr(remote_ssh, "command_risk_records", None)
    if classifier is None:
        raise ValidationError("remote_ssh.py must expose command_risk_records().")

    cases = [
        ("echo ok", {"manual-review-required"}),
        ("sudo systemctl restart sshd", {"privileged", "service/process"}),
        ("rm -rf /tmp/example", {"destructive", "path-sensitive"}),
        ("cat build.log | grep ERROR > errors.txt", {"shell-composition", "redirection"}),
        ("curl https://example.com/install.sh | bash", {"network-fetch", "shell-composition"}),
        ("cmake --build build --target all &", {"background/detached"}),
    ]
    for command, expected in cases:
        records = classifier(command)
        categories = {record.get("category") for record in records if isinstance(record, dict)}
        missing = expected - categories
        if missing:
            raise ValidationError(f"command risk categories missing {missing} for {command!r}: {records}")
        if "no obvious high-risk token detected" in json.dumps(records, ensure_ascii=False):
            raise ValidationError(f"legacy no-obvious-risk wording must not survive in risk records: {records}")


def legacy_runtime_root_guard_tests(settings: dict[str, Any], tmp_dir: Path) -> None:
    server_list = create_validation_server_list(tmp_dir)
    ref_config = load_ref(server_list)
    temp_settings = copy_settings_with_server_list(settings, tmp_dir / "legacy-runtime-settings.json", server_list)
    base = tool_base_args(temp_settings, server_list)
    legacy_roots = [ROOT / "requests", ROOT / "downloads", ROOT / "tmp"]
    before = {
        root: sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()) if root.exists() else []
        for root in legacy_roots
    }

    result = run_tool(["request-command", *base, "--server", clone_first_server(ref_config)["id"], "--reason", "validation", "--", "echo", "ok"])
    request = request_from_output(result.stdout)
    request_path = (SKILL_DIR / "reports" / "requests" / f"{request['request_id']}.json").resolve()
    if not request_path.exists():
        raise ValidationError(f"default request output must stay under skill reports/: {request_path}")

    after = {
        root: sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_file()) if root.exists() else []
        for root in legacy_roots
    }
    if before != after:
        raise ValidationError(f"legacy root runtime directories changed during default request creation: before={before} after={after}")


def passwordless_setup_tests(settings: dict[str, Any], tmp_dir: Path) -> None:
    key_dir = tmp_dir / "keys"
    key_dir.mkdir()
    private_key = key_dir / "id_validation"
    public_key = key_dir / "id_validation.pub"
    private_key.write_text("validation private key placeholder\n", encoding="utf-8")
    public_key.write_text("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIvalidation validation@example\n", encoding="utf-8")
    server_list = write_json(
        tmp_dir / "passwordless-server-list.json",
        {
            "version": 1,
            "default_key_dir": str(key_dir),
            "servers": [
                {
                    "id": "passwordless_1",
                    "name": "Passwordless Validation",
                    "type": "ssh",
                    "host": "example.internal",
                    "port": 22,
                    "username": "codex",
                    "key_name": "id_validation",
                    "workdir": "~/workspace",
                    "enabled": True,
                }
            ],
        },
    )
    temp_settings = copy_settings_with_server_list(settings, tmp_dir / "passwordless-settings.json", server_list)
    result = run_tool(["setup-key", "--settings", str(temp_settings), "--server", "passwordless_1"])
    assert_contains(result.stdout, "private_key_exists: true", "setup-key output")
    assert_contains(result.stdout, "public_key_exists: true", "setup-key output")
    assert_contains(result.stdout, "BatchMode=yes", "setup-key output")
    assert_contains(result.stdout, "authorized_keys", "setup-key output")
    assert_contains(result.stdout, "manual_login_required: true", "setup-key output")
    assert_not_contains(result.stdout, "example.internal", "setup-key output")
    assert_not_contains(result.stdout, "codex", "setup-key output")
    assert_not_contains(result.stdout, "id_validation", "setup-key output")

    missing_config = load_ref(server_list)
    missing_config["servers"][0]["key_name"] = "missing_key"
    missing_list = write_json(tmp_dir / "passwordless-missing-key.json", missing_config)
    missing_settings = copy_settings_with_server_list(settings, tmp_dir / "passwordless-missing-settings.json", missing_list)
    missing_result = run_tool(["setup-key", "--settings", str(missing_settings), "--server", "passwordless_1"], expected=2)
    assert_contains(missing_result.stdout, "private_key_exists: false", "missing setup-key output")
    assert_contains(missing_result.stdout, "public_key_exists: false", "missing setup-key output")
    assert_contains(missing_result.stdout, "next:", "missing setup-key output")
    assert_not_contains(missing_result.stdout, "missing_key", "missing setup-key output")


def key_only_repair_tests(settings: dict[str, Any], tmp_dir: Path) -> None:
    key_dir = tmp_dir / "repair-keys"
    key_dir.mkdir()
    server_list = write_json(
        tmp_dir / "repair-server-list.json",
        {
            "version": 1,
            "default_key_dir": str(key_dir),
            "servers": [
                {
                    "id": "repair_1",
                    "name": "Repair Validation",
                    "category": "General",
                    "functions": ["passwordless repair"],
                    "type": "ssh",
                    "host": "repair.example.internal",
                    "port": 22,
                    "username": "codex",
                    "key_name": "old_missing_key",
                    "workdir": "~/workspace",
                    "enabled": True,
                    "notes": "preserve me",
                    "validation": {
                        "status": "failed",
                        "method": "ssh_key",
                        "verified_at": None,
                        "last_error": "old validation failure",
                    },
                    "workspace_check": {
                        "status": "failed",
                        "checked_at": "2026-01-01T00:00:00Z",
                        "message": "old workspace failure",
                    },
                    "software_scan": {
                        "status": "failed",
                        "tools": {},
                        "fpga_devices": [],
                        "raw_summary": "",
                        "last_error": "old scan failure",
                    },
                }
            ],
        },
    )
    temp_settings = copy_settings_with_server_list(settings, tmp_dir / "repair-settings.json", server_list)
    settings_copy = load_ref(temp_settings)
    settings_copy["tools"]["ssh_keygen"] = str(create_fake_keygen(tmp_dir))
    settings_copy["tools"]["ssh_client"] = str(
        create_sequence_fake_ssh(
            tmp_dir,
            [
                {"stdout": "/home/codex/workspace\n", "returncode": 0},
                {"stdout": fake_scan_output(), "returncode": 0},
            ],
        )
    )
    settings_copy["inventory"] = compact_scan_inventory()
    write_json(temp_settings, settings_copy)

    noninteractive = run_tool(["configure-key", "--settings", str(temp_settings), "--server", "repair_1"], expected=1)
    assert_contains(noninteractive.stderr, "--interactive", "configure-key noninteractive error")

    before_decline = json.dumps(load_ref(server_list), sort_keys=True)
    decline_result = run_tool(
        ["configure-key", "--settings", str(temp_settings), "--server", "repair_1", "--interactive"],
        expected=3,
        input_text="id_repair_decline\ngenerate\ny\nempty\nn\n",
    )
    assert_contains(decline_result.stdout, f"key_generation_target: {key_dir / 'id_repair_decline'}", "configure-key decline output")
    assert_contains(decline_result.stdout, "key_generation: created", "configure-key decline output")
    assert_contains(decline_result.stdout, "authorized_keys", "configure-key decline output")
    assert_contains(decline_result.stdout, "configuration_status: cancelled", "configure-key decline output")
    if before_decline != json.dumps(load_ref(server_list), sort_keys=True):
        raise ValidationError("configure-key should not modify the server list before passwordless verification")

    success_result = run_tool(
        ["configure-key", "--settings", str(temp_settings), "--server", "repair_1", "--interactive"],
        input_text="id_repair_success\ngenerate\ny\nempty\ny\n",
    )
    assert_contains(success_result.stdout, f"key_generation_target: {key_dir / 'id_repair_success'}", "configure-key success output")
    assert_contains(success_result.stdout, "key_only_repair: verified", "configure-key success output")
    assert_contains(success_result.stdout, "backup:", "configure-key success output")
    assert_not_contains(success_result.stdout, "repair.example.internal", "configure-key success output")
    assert_not_contains(success_result.stdout, "codex", "configure-key success output")
    updated = load_ref(server_list)["servers"][0]
    preserved = {
        "id": "repair_1",
        "name": "Repair Validation",
        "category": "General",
        "functions": ["passwordless repair"],
        "type": "ssh",
        "host": "repair.example.internal",
        "port": 22,
        "username": "codex",
        "workdir": "~/workspace",
        "enabled": True,
        "notes": "preserve me",
    }
    for key, value in preserved.items():
        if updated.get(key) != value:
            raise ValidationError(f"configure-key changed non-key field {key}: {updated}")
    if updated.get("key_name") != "id_repair_success":
        raise ValidationError(f"configure-key did not write verified key_name: {updated}")
    if updated.get("validation", {}).get("status") != "verified":
        raise ValidationError(f"configure-key did not persist verified validation: {updated}")
    if updated.get("workspace_check", {}).get("status") != "ok":
        raise ValidationError(f"configure-key did not persist ok workspace status: {updated}")
    if updated.get("software_scan", {}).get("status") != "ok":
        raise ValidationError(f"configure-key did not refresh software scan: {updated}")

    failure_list = write_json(tmp_dir / "repair-failure-server-list.json", load_ref(server_list))
    failure_config = load_ref(failure_list)
    failure_config["servers"][0]["key_name"] = "id_repair_success"
    write_json(failure_list, failure_config)
    failure_settings = copy_settings_with_server_list(settings, tmp_dir / "repair-failure-settings.json", failure_list)
    failure_settings_copy = load_ref(failure_settings)
    failure_settings_copy["tools"]["ssh_client"] = str(
        create_sequence_fake_ssh(tmp_dir, [{"stderr": "Permission denied (publickey).\n", "returncode": 255}])
    )
    write_json(failure_settings, failure_settings_copy)
    before_failure = json.dumps(load_ref(failure_list), sort_keys=True)
    failure_result = run_tool(
        ["configure-key", "--settings", str(failure_settings), "--server", "repair_1", "--interactive"],
        expected=255,
        input_text="id_repair_other\ngenerate\ny\nempty\ny\n",
    )
    assert_contains(failure_result.stdout, f"key_generation_target: {key_dir / 'id_repair_other'}", "configure-key failure output")
    assert_contains(failure_result.stdout, "key_only_repair: verification_failed", "configure-key failure output")
    assert_contains(failure_result.stdout, "configure-key", "configure-key failure output")
    assert_not_contains(failure_result.stdout + failure_result.stderr, "repair.example.internal", "configure-key failure output")
    assert_not_contains(failure_result.stdout + failure_result.stderr, "codex", "configure-key failure output")
    auth_failure_summary = "\n".join(
        line for line in (failure_result.stdout + failure_result.stderr).splitlines() if "Permission denied" in line or "verification_failed" in line
    )
    assert_not_contains(auth_failure_summary, "id_repair_other", "configure-key failure auth summary")
    if before_failure != json.dumps(load_ref(failure_list), sort_keys=True):
        raise ValidationError("configure-key should not write candidate key_name after failed verification")

    workspace_failure = run_tool(
        ["workspace-check", "--settings", str(failure_settings), "--server", "repair_1"],
        expected=255,
    )
    assert_contains(workspace_failure.stdout, "configure-key", "workspace-check auth failure guidance")
    assert_not_contains(workspace_failure.stdout + workspace_failure.stderr, "repair.example.internal", "workspace-check auth failure output")
    assert_not_contains(workspace_failure.stdout + workspace_failure.stderr, "codex", "workspace-check auth failure output")


def script_tests(settings: dict[str, Any], tmp_dir: Path) -> None:
    empty_list = tmp_dir / "script" / "server_list.local.json"
    temp_settings = copy_settings_with_server_list(settings, tmp_dir / "script-settings.json", empty_list)
    run_tool(["init-config", "--settings", str(temp_settings)])

    bat_script = SKILL_DIR / "scripts" / "bat" / "config" / "configure_remote_ssh.bat"
    result = subprocess.run(
        ["cmd", "/c", str(bat_script), "--settings", str(temp_settings)],
        cwd=ROOT,
        text=True,
        input="cancel\n",
        capture_output=True,
        check=False,
    )
    if result.returncode != 3:
        raise ValidationError(f"batch configure script failed\nreturncode: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    sh_runner = shutil.which("sh") or shutil.which("bash")
    if sh_runner:
        sh_script = SKILL_DIR / "scripts" / "shell" / "config" / "configure_remote_ssh.sh"
        if "system32" in sh_runner.casefold() and "bash" in sh_runner.casefold():
            command = (
                "printf 'cancel\\n' | "
                f"bash {shell_compatible_path(sh_script, sh_runner)} "
                f"--settings {shell_compatible_path(temp_settings, sh_runner)}"
            )
            result = subprocess.run(
                [sh_runner, "-lc", command],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        else:
            result = subprocess.run(
                [sh_runner, shell_compatible_path(sh_script, sh_runner), "--settings", shell_compatible_path(temp_settings, sh_runner)],
                cwd=ROOT,
                text=True,
                input="cancel\n",
                capture_output=True,
                check=False,
            )
        if result.returncode != 3:
            raise ValidationError(f"shell configure script failed\nreturncode: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")

    bat_help = subprocess.run(
        ["cmd", "/c", str(SKILL_DIR / "scripts" / "bat" / "workspace" / "workspace_check.bat"), "--help"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if bat_help.returncode != 0 or "workspace-check" not in bat_help.stdout:
        raise ValidationError(f"batch workspace wrapper failed\nstdout:\n{bat_help.stdout}\nstderr:\n{bat_help.stderr}")

    ps_runner = shutil.which("powershell") or shutil.which("pwsh")
    if ps_runner:
        ps_help = subprocess.run(
            [
                ps_runner,
                "-NoProfile",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(SKILL_DIR / "scripts" / "powershell" / "files" / "file_list.ps1"),
                "--help",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if ps_help.returncode != 0 or "file-list" not in ps_help.stdout:
            raise ValidationError(f"powershell file wrapper failed\nstdout:\n{ps_help.stdout}\nstderr:\n{ps_help.stderr}")


def request_path_from_output(output: str) -> Path:
    for line in output.splitlines():
        if line.startswith("request: "):
            return Path(line.split("request: ", 1)[1].strip())
    raise ValidationError(f"request path not found in output:\n{output}")


def job_id_from_output(output: str) -> str:
    for line in output.splitlines():
        if line.startswith("job_id: "):
            return line.split("job_id: ", 1)[1].strip()
    raise ValidationError(f"job id not found in output:\n{output}")


def ssh_tests(settings: dict[str, Any], settings_path: Path, server_list: Path, tmp_dir: Path) -> None:
    ssh_settings = copy_settings_with_server_list(
        settings,
        tmp_dir / "ssh-settings.json",
        server_list,
        requests_dir=tmp_dir / "ssh-requests",
        downloads_dir=tmp_dir / "ssh-downloads",
        upload_roots=[str(tmp_dir)],
    )
    base = tool_base_args(ssh_settings, server_list)
    ssh_server = validation_name(settings, "ssh_server", validation_name(settings, "positive_server"))
    timeout = str(remote_ssh.settings_value(settings, "ssh", "default_timeout", default=remote_ssh.DEFAULT_TIMEOUT))
    exec_result = run_tool(["exec", *base, "--server", ssh_server, "--timeout", timeout, "--", "echo", "ok"])
    assert_contains(exec_result.stdout, "ok", "ssh exec output")

    inventory_result = run_tool(["inventory", *base, "--server", ssh_server, "--timeout", timeout])
    assert_contains(inventory_result.stdout, f"server: {ssh_server}", "inventory output")
    expected = remote_ssh.settings_value(settings, "validation", "expected_inventory_contains", default=[])
    if not isinstance(expected, list):
        raise ValidationError("settings.validation.expected_inventory_contains must be a list.")
    for item in expected:
        assert_contains(inventory_result.stdout, str(item), "inventory output")

    workspace_result = run_tool(["workspace-check", *base, "--server", ssh_server, "--timeout", timeout])
    assert_contains(workspace_result.stdout, "status: ok", "workspace-check output")

    remote_root = f"erie-remote-ssh-validation-{os.getpid()}"
    local_upload = tmp_dir / "ssh-upload.txt"
    local_upload.write_text("remote ssh file validation\n", encoding="utf-8")
    try:
        mkdir_result = run_tool(["request-mkdir", *base, "--server", ssh_server, "--path", remote_root, "--reason", "validation"])
        mkdir_request = request_path_from_output(mkdir_result.stdout)
        run_tool(["run-request", "--settings", str(ssh_settings), "--config", str(server_list), "--request", str(mkdir_request), "--execute", "--timeout", timeout])

        upload_result = run_tool(
            [
                "request-upload",
                *base,
                "--server",
                ssh_server,
                "--local",
                str(local_upload),
                "--remote",
                f"{remote_root}/ssh-upload.txt",
                "--reason",
                "validation",
            ]
        )
        upload_request = request_path_from_output(upload_result.stdout)
        run_tool(["run-request", "--settings", str(ssh_settings), "--config", str(server_list), "--request", str(upload_request), "--execute", "--timeout", timeout])

        list_result = run_tool(["file-list", *base, "--server", ssh_server, "--path", remote_root])
        assert_contains(list_result.stdout, "ssh-upload.txt", "file-list output")
        stat_result = run_tool(["file-stat", *base, "--server", ssh_server, "--path", f"{remote_root}/ssh-upload.txt"])
        assert_contains(stat_result.stdout, '"type": "file"', "file-stat output")

        run_tool(
            [
                "file-download",
                *base,
                "--server",
                ssh_server,
                "--remote",
                f"{remote_root}/ssh-upload.txt",
                "--local",
                "downloaded/ssh-upload.txt",
                "--timeout",
                timeout,
            ]
        )
        downloaded = tmp_dir / "ssh-downloads" / "downloaded" / "ssh-upload.txt"
        assert_contains(downloaded.read_text(encoding="utf-8"), "remote ssh file validation", "downloaded file")

        command_result = run_tool(["request-command", *base, "--server", ssh_server, "--reason", "validation", "--", "echo", "ok"])
        command_request = request_path_from_output(command_result.stdout)
        command_exec = run_tool(["run-request", "--settings", str(ssh_settings), "--config", str(server_list), "--request", str(command_request), "--execute", "--timeout", timeout])
        assert_contains(command_exec.stdout, "ok", "run-request command output")

        detached = run_tool(
            [
                "exec-detached",
                *base,
                "--server",
                ssh_server,
                "--reason",
                "validation detached smoke",
                "--timeout",
                timeout,
                "--",
                "printf 'detached line one\\n'; sleep 1; printf 'detached line two\\n'",
            ]
        )
        detached_job = job_id_from_output(detached.stdout)
        final_status = ""
        for _ in range(8):
            status_result = run_tool(
                ["status", "--settings", str(ssh_settings), "--config", str(server_list), "--job", detached_job, "--timeout", timeout],
                expected={0, 3},
            )
            final_status = status_result.stdout
            if "status: succeeded" in final_status:
                break
            time.sleep(1)
        assert_contains(final_status, "status: succeeded", "detached job status output")
        tail_result = run_tool(["tail-log", "--settings", str(ssh_settings), "--config", str(server_list), "--job", detached_job, "--lines", "2", "--timeout", timeout])
        assert_contains(tail_result.stdout, "detached line one", "detached tail output")
        assert_contains(tail_result.stdout, "detached line two", "detached tail output")
        run_tool(["exec", *base, "--server", ssh_server, "--timeout", timeout, "--", "rm", "-rf", f".erie-remote-ssh/jobs/{detached_job}"], expected={0, 1})

        run_tool(["file-list", *base, "--server", ssh_server, "--path", "../escape"], expected=1)
    finally:
        cleanup_result = run_tool(["exec", *base, "--server", ssh_server, "--timeout", timeout, "--", "rm", "-rf", remote_root], expected={0, 1})
        _ = cleanup_result


def config_override_tests(settings: dict[str, Any], settings_path: Path, server_list: Path, skill_validator: Path, tmp_dir: Path) -> None:
    copied_settings = tmp_dir / "copied-settings.json"
    source_settings = remote_ssh.settings_path(settings)
    copied_settings.write_text(source_settings.read_text(encoding="utf-8"), encoding="utf-8")
    result = run_tool(["list", "--settings", str(copied_settings), "--config", str(server_list)])
    assert_contains(result.stdout, "<redacted>", "copied settings output")

    env_settings = tmp_dir / "env-settings.json"
    env_var = "REMOTE_SSH_VALIDATION_SERVER_LIST"
    settings_copy = json.loads(source_settings.read_text(encoding="utf-8"))
    settings_copy["paths"]["default_server_list"] = f"${{env:{env_var}}}"
    env_settings.write_text(json.dumps(settings_copy, indent=2), encoding="utf-8")
    old_value = os.environ.get(env_var)
    os.environ[env_var] = str(server_list)
    try:
        result = run_tool(["list", "--settings", str(env_settings)])
        assert_contains(result.stdout, "<redacted>", "env settings output")
    finally:
        if old_value is None:
            os.environ.pop(env_var, None)
        else:
            os.environ[env_var] = old_value

    run_validator(skill_validator)


def hardcoded_path_audit() -> None:
    patterns = [
        re.compile("[A-Z]:" + re.escape("\\") + r"[A-Za-z0-9_.-]+"),
        re.compile("Users" + re.escape("\\") + r"[A-Za-z0-9_.-]+", re.IGNORECASE),
    ]
    files = [
        ROOT / "PROJECT_GOALS.md",
        ROOT / "TASK_PLAN.md",
        SKILL_DIR / "SKILL.md",
        SKILL_DIR / "references" / "configuration.md",
        SKILL_DIR / "references" / "workflows.md",
        SKILL_DIR / "references" / "review-checklist.md",
        SKILL_DIR / "scripts" / "remote_ssh.py",
        SKILL_DIR / "scripts" / "validate_remote_ssh.py",
    ]
    files.extend(path for path in (SKILL_DIR / "scripts").rglob("*") if path.is_file() and path.suffix in {".sh", ".bat", ".ps1"})
    for file_path in files:
        if not file_path.exists():
            continue
        text = file_path.read_text(encoding="utf-8")
        for pattern in patterns:
            if pattern.search(text):
                raise ValidationError(f"hardcoded local path pattern found in {file_path}: {pattern.pattern}")


def skill_frontmatter_audit() -> None:
    skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    if not skill_text.startswith("---\n"):
        raise ValidationError("SKILL.md must start with YAML frontmatter.")
    try:
        frontmatter, _body = skill_text.split("\n---\n", 1)
    except ValueError as exc:
        raise ValidationError("SKILL.md frontmatter must be closed with --- on its own line.") from exc

    keys: list[str] = []
    values: dict[str, str] = {}
    for raw_line in frontmatter.splitlines()[1:]:
        line = raw_line.strip()
        if not line:
            continue
        if ":" not in line:
            raise ValidationError(f"unsupported SKILL.md frontmatter line: {raw_line}")
        key, value = line.split(":", 1)
        key = key.strip()
        keys.append(key)
        values[key] = value.strip().strip('"').strip("'")

    if set(keys) != {"name", "description"} or len(keys) != 2:
        raise ValidationError(f"SKILL.md frontmatter must contain only name and description, got: {keys}")
    if values.get("name") != "erie-remote-ssh":
        raise ValidationError("SKILL.md frontmatter name must be erie-remote-ssh")
    description = values.get("description", "")
    if not description.startswith("Use when"):
        raise ValidationError("SKILL.md description must start with 'Use when' for conservative triggering.")

    domain_terms = [
        "FPGA",
        "RTL",
        "Verilog",
        "SystemVerilog",
        "HLS",
        "C++",
        "Python",
        "neural network",
        "GPU acceleration",
        "FPGA acceleration",
        "application testing",
        "skill testing",
    ]
    remote_terms = [
        "SSH",
        "remote server",
        "server-list JSON",
        "passwordless",
        "key-based SSH",
        "~/workspace",
        "workdir",
        "remote command",
        "troubleshoot SSH",
        "remote inventory",
    ]
    if not any(term.casefold() in description.casefold() for term in domain_terms):
        raise ValidationError("SKILL.md description must include at least one approved development/test domain trigger.")
    if not any(term.casefold() in description.casefold() for term in remote_terms):
        raise ValidationError("SKILL.md description must include at least one explicit remote SSH trigger.")
    same_host_terms = ["same-host", "multi-account", "multi-port"]
    for term in same_host_terms:
        if term.casefold() not in description.casefold():
            raise ValidationError(f"SKILL.md description must mention same-host target selection term {term!r}.")

    forbidden_process_summaries = [
        "discover then",
        "discover ->",
        "discover →",
        "check then",
        "check ->",
        "check →",
        "run then",
        "run ->",
        "run →",
    ]
    for phrase in forbidden_process_summaries:
        if phrase in description.casefold():
            raise ValidationError(f"SKILL.md description should not summarize workflow process: {phrase}")

    body = skill_text.split("\n---\n", 1)[1]
    body_lower = body.casefold()
    if "## trigger policy" not in body_lower:
        raise ValidationError("SKILL.md must include a Trigger Policy section.")
    if "do not use" not in body_lower:
        raise ValidationError("SKILL.md Trigger Policy must include when not to use the skill.")
    required_body_phrases = [
        "ask the user in the conversation",
        "do not launch `configure --interactive`",
        "do not launch `configure-key --interactive`",
        "only changes key_name and validation caches",
        "pressing enter does not choose a default",
        "log in to the remote account once",
        "exec -- echo ok",
    ]
    for phrase in required_body_phrases:
        if phrase not in body_lower:
            raise ValidationError(f"SKILL.md must document the configuration/passwordless guardrail: {phrase!r}")


def skill_identity_audit() -> None:
    if SKILL_DIR.name != "erie-remote-ssh":
        raise ValidationError(f"skill folder must be erie-remote-ssh, got {SKILL_DIR.name}")
    skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    if "name: erie-remote-ssh" not in skill_text:
        raise ValidationError("SKILL.md frontmatter must use name: erie-remote-ssh")
    agent_text = (SKILL_DIR / "agents" / "openai.yaml").read_text(encoding="utf-8")
    if "$erie-remote-ssh" not in agent_text:
        raise ValidationError("agents/openai.yaml default prompt must reference $erie-remote-ssh")
    required_prompt_terms = [
        "choices",
        "configure-key",
        "key-only",
        "mandatory software scan",
        "scan",
        "cached software",
        "workspace",
        "inventory",
        "same-host",
        "multi-account",
        "multi-port",
    ]
    agent_lower = agent_text.casefold()
    for term in required_prompt_terms:
        if term.casefold() not in agent_lower:
            raise ValidationError(f"agents/openai.yaml default prompt must mention {term!r}")


def extraneous_docs_audit() -> None:
    forbidden_names = {
        "readme.md",
        "installation.md",
        "installation_guide.md",
        "install.md",
        "quick_reference.md",
        "changelog.md",
        "changes.md",
    }
    found = [path.relative_to(SKILL_DIR).as_posix() for path in SKILL_DIR.rglob("*.md") if path.name.casefold() in forbidden_names]
    if found:
        raise ValidationError(f"extraneous documentation files inside skill: {', '.join(found)}")


def references_linked_audit() -> None:
    skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    references_dir = SKILL_DIR / "references"
    for reference in sorted(references_dir.glob("*.md")):
        link = f"references/{reference.name}"
        if link not in skill_text:
            raise ValidationError(f"{link} must be directly linked from SKILL.md")
        lines = reference.read_text(encoding="utf-8").splitlines()
        if len(lines) > 100 and "## Contents" not in "\n".join(lines[:40]):
            raise ValidationError(f"{link} is longer than 100 lines and must include a top-level Contents section")


def software_catalog_documentation_audit() -> None:
    config_text = (SKILL_DIR / "references" / "configuration.md").read_text(encoding="utf-8")
    workflow_text = (SKILL_DIR / "references" / "workflows.md").read_text(encoding="utf-8")
    combined = f"{config_text}\n{workflow_text}".casefold()
    required_terms = [
        "trusted local configuration",
        "path_scan",
        "executable_globs",
        "versions",
        "version_command",
        "install_path_command",
        "read-only over ssh",
        "raw_summary",
    ]
    for term in required_terms:
        if term not in combined:
            raise ValidationError(f"software catalog documentation must mention {term!r}")


def default_reports_path_audit(settings: dict[str, Any]) -> None:
    expected_requests = SKILL_DIR / "reports" / "requests"
    expected_downloads = SKILL_DIR / "reports" / "downloads"
    expected_validation_tmp = SKILL_DIR / "reports" / "tmp" / "validation"
    actual_requests = remote_ssh.requests_dir(settings).resolve()
    actual_downloads = remote_ssh.downloads_dir(settings).resolve()
    actual_validation_tmp = remote_ssh.resolve_config_path(
        str(remote_ssh.settings_value(settings, "paths", "validation_tmp_dir", default="${skill_dir}/reports/tmp/validation")),
        remote_ssh.settings_path(settings),
        settings["_context"],
    ).resolve()
    if actual_requests != expected_requests.resolve():
        raise ValidationError(f"default requests_dir must resolve to {expected_requests}, got {actual_requests}")
    if actual_downloads != expected_downloads.resolve():
        raise ValidationError(f"default downloads_dir must resolve to {expected_downloads}, got {actual_downloads}")
    if actual_validation_tmp != expected_validation_tmp.resolve():
        raise ValidationError(f"default validation_tmp_dir must resolve to {expected_validation_tmp}, got {actual_validation_tmp}")
    root_artifact_dirs = {
        (ROOT / "out").resolve(),
        (ROOT / "remote-validation-bundles").resolve(),
        (ROOT / "requests").resolve(),
        (ROOT / "downloads").resolve(),
        (ROOT / "tmp").resolve(),
    }
    for path in [actual_requests, actual_downloads, actual_validation_tmp]:
        try:
            path.relative_to((SKILL_DIR / "reports").resolve())
        except ValueError as exc:
            raise ValidationError(f"default report artifact path must stay inside skill reports/: {path}") from exc
        if path in root_artifact_dirs:
            raise ValidationError(f"default artifact path must not use repository root runtime directory: {path}")
    combined_docs = "\n".join(
        [
            (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8"),
            (SKILL_DIR / "references" / "configuration.md").read_text(encoding="utf-8"),
            (SKILL_DIR / "references" / "workflows.md").read_text(encoding="utf-8"),
            (SKILL_DIR / "references" / "review-checklist.md").read_text(encoding="utf-8"),
        ]
    ).casefold()
    for term in [
        "reports",
        "preserve",
        "update",
        "${skill_dir}/reports/requests",
        "${skill_dir}/reports/downloads",
        "${skill_dir}/reports/tmp/validation",
        "remote-validation-bundles",
        "root-level",
    ]:
        if term not in combined_docs:
            raise ValidationError(f"reports documentation must mention {term!r}")


def skill_local_gitignore_audit() -> None:
    gitignore = SKILL_DIR / ".gitignore"
    if not gitignore.exists():
        raise ValidationError("missing skill-local .gitignore")
    entries = {
        line.strip()
        for line in gitignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required = {
        "config/server_list.local.json",
        "config/server_list.local.json.bak.*",
        "reports/",
        "requests/",
        "downloads/",
        "tmp/",
        "logs/",
        "*.log",
    }
    missing = sorted(required - entries)
    if missing:
        raise ValidationError(f"skill-local .gitignore missing entries: {', '.join(missing)}")


def repository_gitignore_audit() -> None:
    if ROOT.resolve() == SKILL_DIR.resolve():
        return
    gitignore = ROOT / ".gitignore"
    if not gitignore.exists():
        return
    entries = {
        line.strip()
        for line in gitignore.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }
    required = {
        "skills/erie-remote-ssh/config/server_list.local.json",
        "skills/erie-remote-ssh/config/server_list.local.json.bak.*",
        "**/.erie-remote-ssh/project.local.json",
        "skills/erie-remote-ssh/reports/",
        "skills/erie-remote-ssh/requests/",
        "skills/erie-remote-ssh/downloads/",
        "skills/erie-remote-ssh/tmp/",
        "skills/erie-remote-ssh/logs/",
    }
    missing = sorted(required - entries)
    if missing:
        raise ValidationError(f"repository .gitignore missing Erie Remote SSH entries: {', '.join(missing)}")


def skill_version() -> str:
    version_path = SKILL_DIR / "VERSION"
    if not version_path.exists():
        raise ValidationError("missing VERSION file")
    version = version_path.read_text(encoding="utf-8").strip()
    if not SEMVER_PATTERN.fullmatch(version):
        raise ValidationError(f"VERSION must be SemVer without a leading 'v', got: {version!r}")
    return version


def release_artifact_base_name(version: str) -> str:
    return f"erie-remote-ssh-v{version}"


def release_artifact_naming_audit(version: str) -> None:
    dist_root = ROOT / "dist"
    if not dist_root.exists():
        return
    expected_base = release_artifact_base_name(version)
    expected_dir = dist_root / expected_base
    expected_zip = dist_root / f"{expected_base}.zip"
    legacy_dir = dist_root / "erie-remote-ssh"
    legacy_zip = dist_root / "erie-remote-ssh.zip"
    noncanonical = sorted(
        path.name
        for path in dist_root.iterdir()
        if re.fullmatch(r"erie-remote-ssh-\d+\.\d+\.\d+(?:\.zip)?", path.name)
    )
    if legacy_dir.exists() or legacy_zip.exists():
        raise ValidationError("release artifacts must be versioned as erie-remote-ssh-vx.x.x, not unversioned")
    if noncanonical:
        raise ValidationError(
            "release artifacts must use canonical v-prefixed names only; found noncanonical aliases: "
            + ", ".join(noncanonical)
        )
    if (dist_root / "manifest.json").exists() and (not expected_dir.exists() or not expected_zip.exists()):
        raise ValidationError(f"release artifacts must include {expected_dir.name}/ and {expected_zip.name}")
    manifest_path = dist_root / "manifest.json"
    if manifest_path.exists():
        manifest = load_ref(manifest_path)
        if manifest.get("directory_artifact") != expected_dir.name:
            raise ValidationError(f"release manifest directory_artifact must be {expected_dir.name}")
        if manifest.get("zip_artifact") != expected_zip.name:
            raise ValidationError(f"release manifest zip_artifact must be {expected_zip.name}")


def release_retention_policy_tests(tmp_dir: Path) -> None:
    project_root = tmp_dir / "release-retention"
    skill_root = init_release_fixture_project(project_root, version="0.1.9")
    init_git_repo(project_root)
    commit_all(project_root, "init fixture")

    dist_root = project_root / "dist"
    old_dir = dist_root / "erie-remote-ssh-v0.1.8"
    old_zip = dist_root / "erie-remote-ssh-v0.1.8.zip"
    current_dir = dist_root / "erie-remote-ssh-v0.1.9"
    current_zip = dist_root / "erie-remote-ssh-v0.1.9.zip"
    legacy_dir = dist_root / "erie-remote-ssh-0.1.9"
    legacy_zip = dist_root / "erie-remote-ssh-0.1.9.zip"

    (old_dir / "nested").mkdir(parents=True, exist_ok=True)
    (old_dir / "nested" / "sentinel.txt").write_text("keep old version\n", encoding="utf-8")
    old_zip.write_bytes(b"old-version-zip")

    current_dir.mkdir(parents=True, exist_ok=True)
    (current_dir / "stale.txt").write_text("overwrite me\n", encoding="utf-8")
    current_zip.write_bytes(b"stale-current-zip")

    legacy_dir.mkdir(parents=True, exist_ok=True)
    (legacy_dir / "legacy.txt").write_text("legacy alias\n", encoding="utf-8")
    legacy_zip.write_bytes(b"legacy-zip")

    run_fixture_build_release(project_root, skill_root)

    if not (old_dir / "nested" / "sentinel.txt").exists():
        raise ValidationError("build_release.py must preserve older versioned release directories in dist/")
    if old_zip.read_bytes() != b"old-version-zip":
        raise ValidationError("build_release.py must preserve older versioned release zip files in dist/")
    if (current_dir / "stale.txt").exists():
        raise ValidationError("build_release.py must overwrite the current same-version release directory on republish")
    if current_zip.read_bytes() == b"stale-current-zip":
        raise ValidationError("build_release.py must overwrite the current same-version release zip on republish")
    if legacy_dir.exists() or legacy_zip.exists():
        raise ValidationError("build_release.py must remove noncanonical non-v release aliases")


def release_gate_versioned_naming_tests(tmp_dir: Path) -> None:
    project_root = tmp_dir / "release-gate-versioned"
    skill_root = init_release_fixture_project(project_root, version="0.1.9")
    init_git_repo(project_root)
    commit_all(project_root, "init fixture")
    subprocess.run(["git", "branch", "release"], cwd=project_root, text=True, capture_output=True, check=True)

    pre_result = run_fixture_release_gate(project_root, "v0.1.9", phase="pre")
    if not pre_result.get("ok"):
        raise ValidationError(f"release-gate pre should accept canonical v-prefixed version names: {pre_result}")
    if pre_result.get("checks", {}).get("expected_release_dir") != "dist/erie-remote-ssh-v0.1.9":
        raise ValidationError(f"release-gate pre expected release dir should use v-prefix naming: {pre_result}")

    run_fixture_build_release(project_root, skill_root)
    commit_all(project_root, "publish fixture release")

    post_result = run_fixture_release_gate(project_root, "v0.1.9", phase="post")
    if not post_result.get("ok"):
        raise ValidationError(f"release-gate post should pass with canonical v-prefixed artifacts only: {post_result}")


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def current_git_branch() -> str:
    result = subprocess.run(
        ["git", "branch", "--show-current"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    branch = result.stdout.strip()
    if result.returncode != 0 or not branch:
        raise ValidationError(f"failed to resolve current git branch\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return branch


def current_git_status_lines() -> list[str]:
    result = subprocess.run(
        ["git", "status", "--short"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValidationError(f"failed to resolve current git status\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}")
    return [line.rstrip() for line in result.stdout.splitlines() if line.strip()]


def release_file_count(path: Path) -> int:
    count = 0
    for file_path in path.rglob("*"):
        if not file_path.is_file():
            continue
        parts = {part.casefold() for part in file_path.relative_to(path).parts}
        if "__pycache__" in parts or file_path.suffix.casefold() == ".pyc":
            continue
        count += 1
    return count


def is_release_file(path: Path, root: Path) -> bool:
    if not path.is_file():
        return False
    rel = path.relative_to(root)
    parts = [part.casefold() for part in rel.parts]
    name = path.name.casefold()
    if "__pycache__" in parts or path.suffix.casefold() == ".pyc":
        return False
    if any(part in {"reports", "requests", "downloads", "logs", "tmp"} for part in parts):
        return False
    if rel.as_posix() == "config/server_list.local.json":
        return False
    if name == "release_receipt.json":
        return False
    if name == "project.local.json":
        return False
    if name.startswith("server_list.local.json.bak") or ".bak." in name or name.endswith(".bak"):
        return False
    return True


def release_file_bytes(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(root.rglob("*"))
        if is_release_file(path, root)
    }


def zip_file_bytes(zip_path: Path) -> dict[str, bytes]:
    files: dict[str, bytes] = {}
    with zipfile.ZipFile(zip_path) as archive:
        for name in sorted(archive.namelist()):
            if name.endswith("/"):
                continue
            if name.casefold() == "release_receipt.json":
                continue
            files[name] = archive.read(name)
    return files


def assert_release_trees_match(expected: dict[str, bytes], actual: dict[str, bytes], label: str) -> None:
    missing = sorted(set(expected) - set(actual))
    extra = sorted(set(actual) - set(expected))
    mismatched = sorted(path for path in set(expected) & set(actual) if expected[path] != actual[path])
    if missing or extra or mismatched:
        raise ValidationError(
            f"{label} does not match source release files. "
            f"missing={missing[:5]} extra={extra[:5]} mismatched={mismatched[:5]}"
        )


def assert_utf8_markdown_bytes(data: bytes, label: str) -> str:
    if data.startswith(b"\xef\xbb\xbf"):
        raise ValidationError(f"{label} must be UTF-8 without BOM")
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ValidationError(f"{label} must decode as UTF-8: {exc}") from exc
    if "\ufffd" in text:
        raise ValidationError(f"{label} must not contain Unicode replacement characters")
    return text


def canary_value(text: str) -> str | None:
    return ENCODING_CANARY if ENCODING_CANARY in text else None


def assert_markdown_tree_utf8(root: Path, label: str) -> dict[str, str]:
    canaries: dict[str, str] = {}
    for file_path in sorted(root.rglob("*.md")):
        if not file_path.is_file():
            continue
        rel = file_path.relative_to(root).as_posix()
        if any(part in {"reports", "requests", "downloads", "logs", "tmp"} for part in file_path.relative_to(root).parts):
            continue
        text = assert_utf8_markdown_bytes(file_path.read_bytes(), f"{label}:{rel}")
        found = canary_value(text)
        if found:
            canaries[rel] = found
    return canaries


def assert_markdown_zip_utf8(zip_path: Path) -> dict[str, str]:
    canaries: dict[str, str] = {}
    with zipfile.ZipFile(zip_path) as archive:
        for name in sorted(archive.namelist()):
            if name.endswith("/") or not name.casefold().endswith(".md"):
                continue
            data = archive.read(name)
            text = assert_utf8_markdown_bytes(data, f"{zip_path.name}:{name}")
            found = canary_value(text)
            if found:
                canaries[name] = found
    return canaries


def markdown_encoding_guard_tests(tmp_dir: Path) -> None:
    valid = tmp_dir / "valid-canary.md"
    valid.write_text(ENCODING_CANARY + "\n", encoding="utf-8")
    assert_utf8_markdown_bytes(valid.read_bytes(), str(valid))

    bom = tmp_dir / "bom.md"
    bom.write_bytes(b"\xef\xbb\xbf# BOM\n")
    try:
        assert_utf8_markdown_bytes(bom.read_bytes(), str(bom))
    except ValidationError as exc:
        assert_contains(str(exc), "BOM", "BOM encoding guard")
    else:
        raise ValidationError("UTF-8 markdown guard accepted a BOM file")

    invalid = tmp_dir / "invalid.md"
    invalid.write_bytes(b"# invalid utf8: \xff\n")
    try:
        assert_utf8_markdown_bytes(invalid.read_bytes(), str(invalid))
    except ValidationError as exc:
        assert_contains(str(exc), "UTF-8", "invalid UTF-8 guard")
    else:
        raise ValidationError("UTF-8 markdown guard accepted invalid UTF-8 bytes")

    replacement = tmp_dir / "replacement.md"
    replacement.write_text("bad replacement \ufffd\n", encoding="utf-8")
    try:
        assert_utf8_markdown_bytes(replacement.read_bytes(), str(replacement))
    except ValidationError as exc:
        assert_contains(str(exc), "replacement", "replacement character guard")
    else:
        raise ValidationError("UTF-8 markdown guard accepted replacement characters")


def markdown_encoding_artifact_audit() -> None:
    source_canary = assert_markdown_tree_utf8(SKILL_DIR, "source skill")
    canary_path = "references/configuration.md"
    if source_canary.get(canary_path) != ENCODING_CANARY:
        raise ValidationError(f"{canary_path} must contain the Chinese UTF-8 encoding canary")

    version = skill_version()
    artifact_base = release_artifact_base_name(version)
    dist_root = ROOT / "dist"
    dist_skill = dist_root / artifact_base
    if dist_skill.exists():
        dist_canary = assert_markdown_tree_utf8(dist_skill, "dist directory")
        if dist_canary.get(canary_path) != ENCODING_CANARY:
            raise ValidationError("dist directory markdown canary does not match source")

    zip_path = dist_root / f"{artifact_base}.zip"
    if zip_path.exists():
        zip_canary = assert_markdown_zip_utf8(zip_path)
        if zip_canary.get(canary_path) != ENCODING_CANARY:
            raise ValidationError("dist zip markdown canary does not match source")


def release_artifact_consistency_audit() -> None:
    if os.environ.get("ERIE_REMOTE_SSH_SKIP_ISOLATED_VALIDATION") == "1":
        return
    version = skill_version()
    artifact_base = release_artifact_base_name(version)
    dist_root = ROOT / "dist"
    dist_skill = dist_root / artifact_base
    zip_path = dist_root / f"{artifact_base}.zip"
    if not dist_skill.exists() or not zip_path.exists():
        raise ValidationError("dist directory and zip artifact must exist; run scripts/build_release.py")
    source_files = release_file_bytes(SKILL_DIR)
    assert_release_trees_match(source_files, release_file_bytes(dist_skill), "dist directory artifact")
    assert_release_trees_match(source_files, zip_file_bytes(zip_path), "dist zip artifact")


def installed_skill_path() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    root = Path(codex_home).expanduser() if codex_home else Path.home() / ".codex"
    return root / "skills" / "erie-remote-ssh"


def installed_skill_audit() -> None:
    if os.environ.get("ERIE_REMOTE_SSH_SKIP_ISOLATED_VALIDATION") == "1":
        return
    installed = installed_skill_path()
    if not installed.exists() or installed.resolve() == SKILL_DIR.resolve():
        return
    key_files = [
        "SKILL.md",
        "agents/openai.yaml",
        "references/configuration.md",
        "references/workflows.md",
        "references/review-checklist.md",
        "scripts/install_skill.py",
        "scripts/remote_ssh.py",
        "scripts/validate_remote_ssh.py",
    ]
    for rel in key_files:
        source_path = SKILL_DIR / rel
        installed_path = installed / rel
        if not installed_path.exists():
            raise ValidationError(f"installed skill missing {rel}: {installed}")
        if source_path.read_bytes() != installed_path.read_bytes():
            raise ValidationError(f"installed skill is stale for {rel}: {installed}")
    installed_text = "\n".join(
        (installed / rel).read_text(encoding="utf-8")
        for rel in ["SKILL.md", "references/configuration.md", "references/workflows.md", "references/review-checklist.md"]
    )
    required = ["configure-key", ENCODING_CANARY, "field-menu", "skill_backups", "server_list.local.json"]
    for marker in required:
        if marker not in installed_text:
            raise ValidationError(f"installed skill missing current marker {marker!r}: {installed}")


def gitattributes_encoding_audit() -> None:
    required = {
        ".gitattributes": "working-tree-encoding=UTF-8",
        ".gitignore": "working-tree-encoding=UTF-8",
        "VERSION": "working-tree-encoding=UTF-8",
        "*.md": "working-tree-encoding=UTF-8",
        "*.json": "working-tree-encoding=UTF-8",
        "*.yaml": "working-tree-encoding=UTF-8",
    }
    if os.environ.get("ERIE_REMOTE_SSH_SKIP_ISOLATED_VALIDATION") == "1" and not (ROOT / ".gitattributes").exists():
        return
    paths = [ROOT / ".gitattributes"]
    dist_gitattributes = ROOT / "dist" / ".gitattributes"
    if dist_gitattributes.parent.exists():
        paths.append(dist_gitattributes)
    for path in paths:
        if not path.exists():
            if path == ROOT / ".gitattributes" and not (ROOT / ".git").exists():
                continue
            raise ValidationError(f"missing gitattributes file: {path}")
        text = path.read_text(encoding="utf-8")
        for pattern, attribute in required.items():
            if pattern not in text or attribute not in text:
                raise ValidationError(f"{path} must declare {pattern} {attribute}")


def release_manifest_audit(version: str) -> None:
    manifest_path = ROOT / "dist" / "manifest.json"
    if not manifest_path.exists():
        return
    manifest = load_ref(manifest_path)
    artifact_root = manifest_path.parent
    required = {
        "name",
        "version",
        "source_branch",
        "source_commit",
        "release_branch",
        "directory_artifact",
        "zip_artifact",
        "zip_sha256",
        "file_count",
        "release_created_at",
        "excludes",
    }
    missing = sorted(required - set(manifest))
    if missing:
        raise ValidationError(f"release manifest missing fields: {', '.join(missing)}")
    if manifest.get("name") != "erie-remote-ssh":
        raise ValidationError("release manifest name must be erie-remote-ssh")
    if manifest.get("version") != version:
        raise ValidationError(f"release manifest version must match VERSION {version}")
    expected_source_branch = current_git_branch()
    if manifest.get("source_branch") != expected_source_branch:
        raise ValidationError(f"release manifest source_branch must be {expected_source_branch}")
    if manifest.get("release_branch") != "release":
        raise ValidationError("release manifest release_branch must be release")
    expected_base = release_artifact_base_name(version)
    if manifest.get("directory_artifact") != expected_base:
        raise ValidationError(f"release manifest directory_artifact must be {expected_base}")
    if manifest.get("zip_artifact") != f"{expected_base}.zip":
        raise ValidationError(f"release manifest zip_artifact must be {expected_base}.zip")
    source_commit = str(manifest.get("source_commit", ""))
    if source_commit != "working-tree" and not re.fullmatch(r"[0-9a-f]{40}", source_commit):
        raise ValidationError("release manifest source_commit must be a full lowercase Git hash or working-tree")
    if source_commit == "working-tree" and manifest.get("source_dirty") is not True:
        raise ValidationError("release manifest source_dirty must be true when source_commit is working-tree")
    if current_git_branch() == "master":
        if source_commit == "working-tree" or manifest.get("source_dirty") is True:
            raise ValidationError("formal release artifacts on master must not be built from a dirty worktree")
        if current_git_status_lines():
            raise ValidationError("formal release verification on master requires a clean git status")
    directory_artifact = artifact_root / str(manifest.get("directory_artifact"))
    zip_artifact = artifact_root / str(manifest.get("zip_artifact"))
    expected_directory = ROOT / "dist" / expected_base
    if directory_artifact.resolve() != expected_directory.resolve():
        raise ValidationError("release manifest directory_artifact must point to the expected dist skill directory")
    if not zip_artifact.exists():
        raise ValidationError(f"release manifest zip_artifact does not exist: {zip_artifact}")
    if file_sha256(zip_artifact) != manifest.get("zip_sha256"):
        raise ValidationError("release manifest zip_sha256 does not match the zip artifact")
    actual_file_count = release_file_count(directory_artifact)
    if manifest.get("file_count") != actual_file_count:
        raise ValidationError(f"release manifest file_count must be {actual_file_count}")
    excludes = manifest.get("excludes")
    if not isinstance(excludes, list) or "config/server_list.local.json" not in excludes:
        raise ValidationError("release manifest excludes must include config/server_list.local.json")
    if "project.local.json" not in excludes:
        raise ValidationError("release manifest excludes must include project.local.json")
    if "reports/" not in excludes:
        raise ValidationError("release manifest excludes must include reports/")


def design_pattern_audit() -> None:
    skill_text = (SKILL_DIR / "SKILL.md").read_text(encoding="utf-8")
    lower_text = skill_text.casefold()
    required_paths = [
        SKILL_DIR / "scripts" / "remote_ssh.py",
        SKILL_DIR / "scripts" / "install_skill.py",
        SKILL_DIR / "config" / "server_list.template.json",
        SKILL_DIR / "references" / "review-checklist.md",
    ]
    for path in required_paths:
        if not path.exists():
            raise ValidationError(f"missing required skill design artifact: {path.relative_to(SKILL_DIR).as_posix()}")
    required_mentions = [
        "scripts/remote_ssh.py",
        "scripts/install_skill.py",
        "config/server_list.template.json",
        "references/review-checklist.md",
        "configure-key",
        "--accept-new-host-key",
        "explicitly",
    ]
    for mention in required_mentions:
        if mention.casefold() not in lower_text:
            raise ValidationError(f"SKILL.md must mention {mention!r}")
    control_path = ROOT / ".agents" / "agents-control.json"
    if control_path.exists():
        control = load_ref(control_path)
        patterns = ((control.get("skill_design_contract") or {}).get("patterns")) or []
        if "Generator" in patterns:
            raise ValidationError("Generator must not remain in the primary declared design pattern set for RemoteSSH.")

    pipeline_terms = ["discover", "configure", "setup-key", "check", "workspace-check", "run-request"]
    last_position = -1
    for term in pipeline_terms:
        position = lower_text.find(term, last_position + 1)
        if position == -1:
            raise ValidationError(f"SKILL.md hard-checkpoint pipeline missing or out of order: {term}")
        last_position = position


def template_tests() -> None:
    template_path = SKILL_DIR / "config" / "server_list.template.json"
    if not template_path.exists():
        raise ValidationError("missing config/server_list.template.json")
    template = load_ref(template_path)
    if template.get("version") != 1:
        raise ValidationError("server_list.template.json must use version 1")
    if not isinstance(template.get("servers"), list):
        raise ValidationError("server_list.template.json must contain a servers array")
    text = template_path.read_text(encoding="utf-8")
    forbidden = ["10.201.", "FPGA-Server", "GPU-HPC", "id_ed25519_fpga", "inventory_snapshot"]
    for item in forbidden:
        assert_not_contains(text, item, "server list template")


def ref_dependency_audit() -> None:
    scanned_files = [
        SKILL_DIR / "SKILL.md",
        SKILL_DIR / "config" / "defaults.json",
        SKILL_DIR / "references" / "configuration.md",
        SKILL_DIR / "references" / "workflows.md",
        SKILL_DIR / "references" / "review-checklist.md",
        SKILL_DIR / "scripts" / "remote_ssh.py",
        SKILL_DIR / "scripts" / "validate_remote_ssh.py",
    ]
    forbidden_patterns = ["../" + "../ref", "ref/" + "server_list.local.json", "\\ref\\" + "server_list.local.json"]
    for file_path in scanned_files:
        text = file_path.read_text(encoding="utf-8")
        for pattern in forbidden_patterns:
            assert_not_contains(text, pattern, str(file_path))


def script_layout_audit() -> None:
    misplaced = [
        path
        for path in (SKILL_DIR / "scripts").iterdir()
        if path.is_file() and path.suffix.casefold() in {".bat", ".sh", ".ps1"}
    ]
    if misplaced:
        names = ", ".join(path.name for path in misplaced)
        raise ValidationError(f"misplaced platform scripts in scripts root: {names}")


def isolated_no_ref_tests(skill_validator: Path, tmp_dir: Path) -> None:
    if os.environ.get("ERIE_REMOTE_SSH_SKIP_ISOLATED_VALIDATION") == "1":
        return

    isolated_root = tmp_dir / "isolated-no-ref-project"
    isolated_skill = isolated_root / "erie-remote-ssh"
    if isolated_root.exists():
        shutil.rmtree(isolated_root)
    isolated_root.mkdir(parents=True)
    shutil.copytree(
        SKILL_DIR,
        isolated_skill,
        ignore=shutil.ignore_patterns(
            "__pycache__",
            "*.pyc",
            "reports",
            "server_list.local.json",
            "*.bak",
            "*.bak.*",
            "*.log",
        ),
    )

    if (isolated_root / "ref").exists():
        raise ValidationError("isolated validation fixture unexpectedly contains a repository-level ref directory")

    child_env = os.environ.copy()
    child_env["ERIE_REMOTE_SSH_SKIP_ISOLATED_VALIDATION"] = "1"
    child_env["REMOTE_SSH_SKILL_VALIDATOR"] = str(skill_validator)
    result = subprocess.run(
        [sys.executable, str(isolated_skill / "scripts" / "validate_remote_ssh.py")],
        cwd=isolated_root,
        env=child_env,
        text=True,
        capture_output=True,
        check=False,
    )
    if result.returncode != 0:
        raise ValidationError(
            "isolated no-ref validation failed\n"
            f"returncode: {result.returncode}\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )


def cleanup_generated_dirs(tmp_dir: Path, cleanup_roots: list[Path]) -> None:
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir, ignore_errors=True)
    for root in cleanup_roots:
        if root.exists() and root != ROOT and not any(root.iterdir()):
            root.rmdir()
    pycache = SKILL_DIR / "scripts" / "__pycache__"
    if pycache.exists():
        shutil.rmtree(pycache, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the Erie Remote SSH skill and helper CLI.")
    parser.add_argument("--settings", type=Path, help="Path to Erie Remote SSH settings JSON.")
    parser.add_argument("--server-list", type=Path, help="Server list JSON. Overrides settings.")
    parser.add_argument("--skill-validator", type=Path, help="quick_validate.py path. Overrides settings.")
    parser.add_argument("--with-ssh", action="store_true", help="Run real SSH exec and inventory checks.")
    args = parser.parse_args()

    settings = remote_ssh.load_settings(args.settings)
    settings_path = remote_ssh.settings_path(settings)
    skill_validator = resolve_skill_validator(settings, args.skill_validator)
    tmp_base = remote_ssh.resolve_config_path(
        str(remote_ssh.settings_value(settings, "paths", "validation_tmp_dir", default="${skill_dir}/reports/tmp/validation")),
        settings_path,
        settings["_context"],
    )
    if remote_ssh.sensitive_local_upload_reasons(tmp_base):
        tmp_base = (Path(tempfile.gettempdir()) / "erie-remote-ssh-validation").resolve()
    tmp_dir = tmp_base / f"run-{os.getpid()}"
    cleanup_roots = [tmp_base, tmp_base.parent]
    if tmp_dir.exists():
        shutil.rmtree(tmp_dir)
    tmp_dir.mkdir(parents=True)
    try:
        version = skill_version()
        assurance_scope_tests()
        governance_window_tests()
        markdown_encoding_guard_tests(tmp_dir)
        release_artifact_naming_audit(version)
        markdown_encoding_artifact_audit()
        release_artifact_consistency_audit()
        installed_skill_audit()
        gitattributes_encoding_audit()
        release_manifest_audit(version)
        skill_frontmatter_audit()
        skill_identity_audit()
        governance_alignment_tests()
        extraneous_docs_audit()
        references_linked_audit()
        software_catalog_documentation_audit()
        default_reports_path_audit(settings)
        skill_local_gitignore_audit()
        repository_gitignore_audit()
        design_pattern_audit()
        template_tests()
        ref_dependency_audit()
        server_list = create_validation_server_list(tmp_dir)
        ref_config = load_ref(server_list)
        run_validator(skill_validator)
        positive_tests(settings, settings_path, server_list, ref_config)
        choices_tests(settings, settings_path, server_list, ref_config, tmp_dir)
        negative_tests(settings_path, server_list, ref_config, tmp_dir)
        subprocess_decoding_tests(tmp_dir)
        software_scan_transport_tests(tmp_dir)
        ssh_error_classification_tests()
        backup_collision_tests(tmp_dir)
        release_retention_policy_tests(tmp_dir)
        release_gate_versioned_naming_tests(tmp_dir)
        software_scan_tests(settings, tmp_dir)
        workspace_check_writeback_tests(settings, tmp_dir)
        project_workdir_tests(settings, tmp_dir)
        project_root_semantics_tests(tmp_dir)
        session_mining_contract_tests(tmp_dir)
        discovery_and_add_tests(settings, tmp_dir)
        ssh_config_fallback_tests(settings, tmp_dir)
        cmd_guidance_tests(settings, settings_path, server_list, ref_config, tmp_dir)
        detached_job_tests(settings, tmp_dir)
        install_skill_protection_tests(tmp_dir)
        configuration_gate_tests(settings, tmp_dir)
        request_and_path_tests(settings, settings_path, server_list, ref_config, tmp_dir)
        risk_classification_tests()
        legacy_runtime_root_guard_tests(settings, tmp_dir)
        passwordless_setup_tests(settings, tmp_dir)
        key_only_repair_tests(settings, tmp_dir)
        script_tests(settings, tmp_dir)
        config_override_tests(settings, settings_path, server_list, skill_validator, tmp_dir)
        script_layout_audit()
        hardcoded_path_audit()
        isolated_no_ref_tests(skill_validator, tmp_dir)
        if args.with_ssh:
            if args.server_list is None:
                raise ValidationError("--with-ssh requires --server-list for a real SSH configuration.")
            real_server_list = args.server_list.resolve()
            before_hash = file_sha256(real_server_list)
            copied_real_server_list = tmp_dir / "real-ssh-server-list.copy.json"
            copied_real_server_list.write_bytes(real_server_list.read_bytes())
            ssh_tests(settings, settings_path, copied_real_server_list, tmp_dir)
            after_hash = file_sha256(real_server_list)
            if before_hash != after_hash:
                raise ValidationError("real --server-list input changed during --with-ssh validation")
    finally:
        cleanup_generated_dirs(tmp_dir, cleanup_roots)

    report = remote_ssh.build_assurance_report(with_ssh=bool(args.with_ssh), real_ssh_verified=bool(args.with_ssh))
    print("status: passed")
    print(f"release_version: {report['release_version']}")
    print(f"governance_version: {report['governance_version']}")
    print(f"verified_scopes: {', '.join(report['verified_scopes'])}")
    print(f"real_ssh_status: {report['real_ssh']['status']}")
    print(f"real_ssh_required_flags: {' '.join(report['real_ssh']['required_flags'])}")
    print(f"message: {report['message']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
