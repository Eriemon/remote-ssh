#!/usr/bin/env python3
"""Build deterministic Erie Remote SSH release artifacts."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import shutil
import subprocess
import zipfile
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
ROOT = SKILL_DIR.parents[1]
DIST_ROOT = ROOT / "dist"
MANIFEST_PATH = DIST_ROOT / "manifest.json"
ZIP_TIMESTAMP = (2026, 1, 1, 0, 0, 0)


def should_exclude(path: Path, root: Path) -> bool:
    rel = path.relative_to(root)
    parts = [part.casefold() for part in rel.parts]
    name = path.name.casefold()
    if "__pycache__" in parts or path.suffix.casefold() == ".pyc":
        return True
    if any(part in {"reports", "requests", "downloads", "logs", "tmp"} for part in parts):
        return True
    if rel.as_posix() == "config/server_list.local.json":
        return True
    if name == "project.local.json":
        return True
    if name.startswith("server_list.local.json.bak") or ".bak." in name or name.endswith(".bak"):
        return True
    return False


def release_files(root: Path) -> list[Path]:
    return [path for path in sorted(root.rglob("*")) if path.is_file() and not should_exclude(path, root)]


def skill_version() -> str:
    return (SKILL_DIR / "VERSION").read_text(encoding="utf-8").strip()


def artifact_base_name() -> str:
    return f"erie-remote-ssh-v{skill_version()}"


def dist_skill_path() -> Path:
    return DIST_ROOT / artifact_base_name()


def zip_path() -> Path:
    return DIST_ROOT / f"{artifact_base_name()}.zip"


def remove_legacy_artifacts() -> None:
    legacy_dir = DIST_ROOT / "erie-remote-ssh"
    legacy_zip = DIST_ROOT / "erie-remote-ssh.zip"
    if legacy_dir.exists():
        shutil.rmtree(legacy_dir)
    if legacy_zip.exists():
        legacy_zip.unlink()


def copy_release_tree() -> None:
    dist_skill = dist_skill_path()
    if dist_skill.exists():
        shutil.rmtree(dist_skill)
    dist_skill.parent.mkdir(parents=True, exist_ok=True)
    for source in release_files(SKILL_DIR):
        rel = source.relative_to(SKILL_DIR)
        target = dist_skill / rel
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
    dist_skill = dist_skill_path()
    artifact_zip = zip_path()
    if artifact_zip.exists():
        artifact_zip.unlink()
    with zipfile.ZipFile(artifact_zip, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for source in release_files(dist_skill):
            rel = source.relative_to(dist_skill).as_posix()
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
    result = subprocess.run(["git", *args], cwd=ROOT, text=True, capture_output=True, check=False)
    return result.stdout.strip() if result.returncode == 0 else ""


def source_state() -> tuple[str, str, bool]:
    branch = git_output(["rev-parse", "--abbrev-ref", "HEAD"]) or "unknown"
    commit = git_output(["rev-parse", "HEAD"])
    dirty = subprocess.run(["git", "diff", "--quiet"], cwd=ROOT, check=False).returncode != 0
    dirty = dirty or subprocess.run(["git", "diff", "--cached", "--quiet"], cwd=ROOT, check=False).returncode != 0
    return branch, ("working-tree" if dirty else commit), dirty


def write_manifest() -> None:
    branch, commit, dirty = source_state()
    artifact_name = artifact_base_name()
    dist_skill = dist_skill_path()
    artifact_zip = zip_path()
    manifest = {
        "name": "erie-remote-ssh",
        "version": skill_version(),
        "source_branch": branch,
        "source_commit": commit,
        "source_dirty": dirty,
        "release_branch": "release",
        "directory_artifact": artifact_name,
        "zip_artifact": f"{artifact_name}.zip",
        "zip_sha256": file_sha256(artifact_zip),
        "file_count": len(release_files(dist_skill)),
        "release_created_at": dt.datetime.now(dt.UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "validation_commands": [
            r"python .\skills\erie-remote-ssh\scripts\validate_remote_ssh.py",
            r"python <skill-creator-quick-validate.py> .\skills\erie-remote-ssh",
        ],
        "excludes": [
            "config/server_list.local.json",
            "project.local.json",
            "*.bak",
            "reports/",
            "requests/",
            "downloads/",
            "logs/",
            "tmp/",
            "__pycache__/",
            "*.pyc",
        ],
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8", newline="\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build Erie Remote SSH release artifacts.")
    parser.parse_args()
    DIST_ROOT.mkdir(parents=True, exist_ok=True)
    remove_legacy_artifacts()
    copy_release_tree()
    write_dist_gitattributes()
    build_zip()
    write_manifest()
    print(f"built: {dist_skill_path()}")
    print(f"zip: {zip_path()}")
    print(f"manifest: {MANIFEST_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
