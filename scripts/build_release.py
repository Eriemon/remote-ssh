#!/usr/bin/env python3
"""Build deterministic Erie Remote SSH release artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import shutil
import stat
import subprocess
import zipfile
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
ROOT = SKILL_DIR.parent
DIST_ROOT = ROOT / "dist"
DIST_SKILL = DIST_ROOT / "erie-remote-ssh"
ZIP_PATH = DIST_ROOT / "erie-remote-ssh.zip"
MANIFEST_PATH = DIST_ROOT / "manifest.json"
ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


def should_exclude(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    parts = [part.casefold() for part in rel.parts]
    name = path.name.casefold()
    if ".git" in parts:
        return True
    if "__pycache__" in parts or path.suffix.casefold() == ".pyc":
        return True
    if any(part in {"reports", "requests", "downloads", "logs", "tmp"} for part in parts):
        return True
    if rel.as_posix() == "config/server_list.local.json":
        return True
    if name.startswith("server_list.local.json.bak") or ".bak." in name or name.endswith(".bak"):
        return True
    return False


def release_files(root: Path) -> list[Path]:
    return [path for path in sorted(root.rglob("*")) if path.is_file() and not should_exclude(path, root)]


def remove_readonly(func, path, _exc_info) -> None:
    os.chmod(path, stat.S_IWRITE)
    func(path)


def copy_release_tree() -> None:
    if DIST_SKILL.exists():
        shutil.rmtree(DIST_SKILL, onerror=remove_readonly)
    DIST_SKILL.parent.mkdir(parents=True, exist_ok=True)
    for source in release_files(SKILL_DIR):
        rel = source.relative_to(SKILL_DIR)
        target = DIST_SKILL / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)


def write_dist_gitattributes() -> None:
    text = "\n".join(
        [
            ".gitattributes text eol=lf working-tree-encoding=UTF-8",
            ".gitignore text eol=lf working-tree-encoding=UTF-8",
            "VERSION text eol=lf working-tree-encoding=UTF-8",
            "*.py text eol=lf",
            "*.md text eol=lf working-tree-encoding=UTF-8",
            "*.json text eol=lf working-tree-encoding=UTF-8",
            "*.yaml text eol=lf working-tree-encoding=UTF-8",
            "*.yml text eol=lf working-tree-encoding=UTF-8",
            "*.sh text eol=lf",
            "*.bat text eol=crlf",
            "*.ps1 text eol=crlf",
            "",
        ]
    )
    (DIST_ROOT / ".gitattributes").write_text(text, encoding="utf-8", newline="\n")


def build_zip() -> None:
    if ZIP_PATH.exists():
        ZIP_PATH.unlink()
    with zipfile.ZipFile(ZIP_PATH, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in release_files(DIST_SKILL):
            rel = source.relative_to(DIST_SKILL).as_posix()
            info = zipfile.ZipInfo(rel, ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, source.read_bytes())


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_output(args: list[str]) -> str:
    result = subprocess.run(["git", *args], cwd=SKILL_DIR, text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def source_state() -> tuple[str, str, bool]:
    branch = git_output(["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown"
    commit = git_output(["rev-parse", "HEAD"])
    dirty = subprocess.run(["git", "diff", "--quiet"], cwd=SKILL_DIR, check=False).returncode != 0
    dirty = dirty or subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=SKILL_DIR, check=False).returncode != 0
    return branch, ("working-tree" if dirty else commit), dirty


def skill_version() -> str:
    return (SKILL_DIR / "VERSION").read_text(encoding="utf-8").strip()


def write_manifest() -> None:
    branch, commit, dirty = source_state()
    manifest = {
        "name": "erie-remote-ssh",
        "version": skill_version(),
        "source_branch": branch,
        "source_commit": commit,
        "source_dirty": dirty,
        "release_branch": "release",
        "directory_artifact": "erie-remote-ssh",
        "zip_artifact": "erie-remote-ssh.zip",
        "zip_sha256": file_sha256(ZIP_PATH),
        "file_count": len(release_files(DIST_SKILL)),
        "release_created_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "validation_commands": [
            r"python .\erie-remote-ssh\scripts\validate_remote_ssh.py",
            r"python C:\Users\17677\.codex\skills\.system\skill-creator\scripts\quick_validate.py .\erie-remote-ssh",
        ],
        "excludes": [
            "config/server_list.local.json",
            "*.bak",
            "reports/",
            "requests/",
            "downloads/",
            "logs/",
            "tmp/",
            ".git/",
            "__pycache__/",
            "*.pyc",
        ],
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Erie Remote SSH release artifacts.")
    parser.parse_args()
    DIST_ROOT.mkdir(parents=True, exist_ok=True)
    copy_release_tree()
    write_dist_gitattributes()
    build_zip()
    write_manifest()
    print(f"built: {DIST_SKILL}")
    print(f"zip: {ZIP_PATH}")
    print(f"manifest: {MANIFEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
