#!/usr/bin/env python3
"""Read-only mining of RemoteSSH-related Codex session history."""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


REMOTE_SESSION_PATTERNS = [
    re.compile(pattern, re.IGNORECASE)
    for pattern in [
        r"erie-remote-ssh",
        r"remote_ssh\.py",
        r"workspace-check",
        r"configure-key",
        r"request-command",
        r"exec-detached",
        r"ssh-config-discover",
        r"same-host",
        r"server_list\.local\.json",
        r"setup-key",
    ]
]

THEME_PATTERNS: dict[str, list[re.Pattern[str]]] = {
    "same-host disambiguation": [re.compile(r"same-host", re.IGNORECASE)],
    "configure-key repair": [re.compile(r"configure-key", re.IGNORECASE)],
    "workspace-check/auth": [
        re.compile(r"workspace-check", re.IGNORECASE),
        re.compile(r"permission denied", re.IGNORECASE),
        re.compile(r"publickey", re.IGNORECASE),
        re.compile(r"timed out", re.IGNORECASE),
        re.compile(r"connection reset", re.IGNORECASE),
    ],
    "exec-detached/job resume": [
        re.compile(r"exec-detached", re.IGNORECASE),
        re.compile(r"\bstatus --job\b", re.IGNORECASE),
        re.compile(r"\btail-log\b", re.IGNORECASE),
    ],
    "ssh-config-discover fallback": [re.compile(r"ssh-config-discover", re.IGNORECASE)],
}

FAILURE_PATTERNS: dict[str, re.Pattern[str]] = {
    "permission-denied": re.compile(r"permission denied|publickey", re.IGNORECASE),
    "connection-reset": re.compile(r"connection reset", re.IGNORECASE),
    "timed-out": re.compile(r"timed out", re.IGNORECASE),
}


def normalize_cwd(value: str | Path | None) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text:
        return ""
    try:
        return Path(text).resolve(strict=False).as_posix().casefold()
    except OSError:
        return text.replace("\\", "/").casefold()


def extract_cwd(record: dict[str, Any]) -> str | None:
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    if payload.get("cwd"):
        return str(payload["cwd"])
    for key in ["session_meta", "meta"]:
        maybe_meta = payload.get(key)
        if not isinstance(maybe_meta, dict):
            continue
        maybe_payload = maybe_meta.get("payload")
        if isinstance(maybe_payload, dict) and maybe_payload.get("cwd"):
            return str(maybe_payload["cwd"])
        if maybe_meta.get("cwd"):
            return str(maybe_meta["cwd"])
    return None


def analyze_session_file(path: Path, target_cwd: str) -> dict[str, Any] | None:
    normalized_target = normalize_cwd(target_cwd)
    cwd = ""
    matched = False
    themes_hit: set[str] = set()
    failures_hit: set[str] = set()

    with path.open("r", encoding="utf-8") as handle:
        for raw_line in handle:
            line = raw_line.rstrip("\n")
            lowered = line.casefold()
            if not matched and any(pattern.search(lowered) for pattern in REMOTE_SESSION_PATTERNS):
                matched = True

            for theme, patterns in THEME_PATTERNS.items():
                if theme not in themes_hit and any(pattern.search(lowered) for pattern in patterns):
                    themes_hit.add(theme)
            for failure, pattern in FAILURE_PATTERNS.items():
                if failure not in failures_hit and pattern.search(lowered):
                    failures_hit.add(failure)

            if not cwd:
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                extracted = extract_cwd(record)
                if extracted:
                    cwd = extracted

    if not matched:
        return None

    normalized_cwd = normalize_cwd(cwd)
    scope = "cross-project"
    if normalized_target and normalized_cwd == normalized_target:
        scope = "exact-cwd"

    return {
        "path": str(path),
        "cwd": cwd,
        "scope": scope,
        "themes": sorted(themes_hit),
        "failure_modes": sorted(failures_hit),
    }


def recommended_hardening(themes: dict[str, int]) -> list[str]:
    recommendations: list[str] = []
    if themes.get("same-host disambiguation", 0):
        recommendations.append("Keep same-host disambiguation explicit in choices output and user prompts.")
    if themes.get("configure-key repair", 0):
        recommendations.append("Preserve key-only repair flows and avoid broad SSH mutation guidance.")
    if themes.get("workspace-check/auth", 0):
        recommendations.append("Keep workspace-check authentication failures mapped to concrete next actions.")
    if themes.get("exec-detached/job resume", 0):
        recommendations.append("Preserve detached-job manifests, status, and tail-log resume contracts.")
    if themes.get("ssh-config-discover fallback", 0):
        recommendations.append("Keep SSH-config fallback read-only, redacted by default, and separate from server-list mutation.")
    return recommendations


def build_summary(sessions_root: str | Path, target_cwd: str | Path) -> dict[str, Any]:
    root = Path(sessions_root)
    summary = {
        "source_root": str(root.resolve()),
        "target_cwd": str(target_cwd),
        "total_remote_ssh_session_count": 0,
        "exact_cwd_session_count": 0,
        "cross_project_session_count": 0,
        "themes": {name: 0 for name in THEME_PATTERNS},
        "typical_failure_modes": {name: 0 for name in FAILURE_PATTERNS},
        "recommended_hardening": [],
        "session_samples": {"exact-cwd": [], "cross-project": []},
    }
    if not root.exists():
        summary["recommended_hardening"] = ["No session root was found; keep relying on repo-local validation until session evidence becomes available."]
        return summary

    for path in sorted(root.rglob("*.jsonl")):
        analyzed = analyze_session_file(path, str(target_cwd))
        if analyzed is None:
            continue
        summary["total_remote_ssh_session_count"] += 1
        scope = str(analyzed["scope"])
        if scope == "exact-cwd":
            summary["exact_cwd_session_count"] += 1
        else:
            summary["cross_project_session_count"] += 1
        if len(summary["session_samples"][scope]) < 5:
            summary["session_samples"][scope].append(str(path))
        for theme in analyzed["themes"]:
            summary["themes"][theme] += 1
        for failure in analyzed["failure_modes"]:
            summary["typical_failure_modes"][failure] += 1

    summary["recommended_hardening"] = recommended_hardening(summary["themes"])
    return summary


def default_sessions_root() -> Path:
    return Path.home() / ".codex" / "sessions"


def main() -> int:
    parser = argparse.ArgumentParser(description="Summarize RemoteSSH-related Codex session history without mutating any state.")
    parser.add_argument("--sessions-root", type=Path, default=default_sessions_root(), help="Root directory containing Codex .jsonl session logs.")
    parser.add_argument("--target-cwd", type=Path, default=Path.cwd(), help="Workspace cwd used to split exact-cwd vs cross-project sessions.")
    parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON output.")
    args = parser.parse_args()

    summary = build_summary(args.sessions_root, args.target_cwd)
    if args.pretty:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
