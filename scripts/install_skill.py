#!/usr/bin/env python3
"""Install Erie Remote SSH while preserving user-local runtime files."""

from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import os
import shutil
import sys
import tempfile
from pathlib import Path


SKILL_NAME = "erie-remote-ssh"
PROTECTED_EXACT = {
    Path("config/server_list.local.json"),
}
FORBIDDEN_SOURCE_RUNTIME_DIRS = {"reports", "requests", "downloads", "tmp", "logs"}


class InstallError(Exception):
    pass


def codex_home() -> Path:
    value = os.environ.get("CODEX_HOME")
    return Path(value).expanduser().resolve() if value else (Path.home() / ".codex").resolve()


def default_target() -> Path:
    return codex_home() / "skills" / SKILL_NAME


def relative_posix(path: Path) -> str:
    return path.as_posix()


def path_is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def is_protected(rel: Path) -> bool:
    rel_posix = rel.as_posix()
    name = rel.name
    if rel in PROTECTED_EXACT:
        return True
    if rel_posix.startswith("reports/"):
        return True
    if rel.parent.as_posix() == "config" and name.startswith("server_list.local.json.bak"):
        return True
    return False


def is_forbidden_source_local_config(rel: Path) -> bool:
    name = rel.name
    return rel in PROTECTED_EXACT or (rel.parent.as_posix() == "config" and name.startswith("server_list.local.json.bak"))


def is_forbidden_source_runtime_artifact(rel: Path) -> bool:
    return bool(rel.parts) and rel.parts[0].casefold() in FORBIDDEN_SOURCE_RUNTIME_DIRS


def source_files(source: Path) -> list[Path]:
    return sorted(path for path in source.rglob("*") if path.is_file())


def validate_source(source: Path) -> None:
    if not source.exists() or not source.is_dir():
        raise InstallError(f"Source skill directory not found: {source}")
    if not (source / "SKILL.md").exists():
        raise InstallError(f"Source does not look like a skill directory: {source}")
    protected = [path.relative_to(source) for path in source_files(source) if is_forbidden_source_local_config(path.relative_to(source))]
    if protected:
        names = ", ".join(relative_posix(path) for path in protected)
        raise InstallError(f"Refusing to install protected local file(s) from source: {names}")
    runtime = [path.relative_to(source) for path in source_files(source) if is_forbidden_source_runtime_artifact(path.relative_to(source))]
    if runtime:
        names = ", ".join(relative_posix(path) for path in runtime)
        raise InstallError(f"Refusing to install runtime artifact(s) from source: {names}")


def validate_install_paths(source: Path, target: Path) -> None:
    if target.name != SKILL_NAME:
        raise InstallError(f"Target directory must be named {SKILL_NAME}: {target}")
    if source == target:
        raise InstallError("Source and target must be different directories.")
    if path_is_relative_to(source, target):
        raise InstallError("Source must not be inside the target directory.")
    if path_is_relative_to(target, source):
        raise InstallError("Target must not be inside the source directory.")
    if target == target.parent or target == Path(target.anchor):
        raise InstallError(f"Refusing unsafe target directory: {target}")
    if target in {Path.home().resolve(), codex_home()}:
        raise InstallError(f"Refusing unsafe target directory: {target}")


def backup_target(target: Path) -> Path | None:
    if not target.exists():
        return None
    backup_root = codex_home() / "skill-backups"
    backup_root.mkdir(parents=True, exist_ok=True)
    timestamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = backup_root / f"{SKILL_NAME}-{timestamp}"
    suffix = 1
    while backup.exists():
        backup = backup_root / f"{SKILL_NAME}-{timestamp}-{suffix}"
        suffix += 1
    shutil.copytree(target, backup)
    return backup


def protected_target_files(target: Path) -> list[Path]:
    if not target.exists():
        return []
    return sorted(path for path in target.rglob("*") if path.is_file() and is_protected(path.relative_to(target)))


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def protected_snapshot(target: Path) -> dict[str, str]:
    return {relative_posix(path.relative_to(target)): file_sha256(path) for path in protected_target_files(target)}


def verify_protected_snapshot(target: Path, snapshot: dict[str, str]) -> None:
    current = protected_snapshot(target)
    missing = sorted(path for path in snapshot if path not in current)
    changed = sorted(path for path, digest in snapshot.items() if current.get(path) != digest)
    if missing or changed:
        details = []
        if missing:
            details.append("missing=" + ",".join(missing))
        if changed:
            details.append("changed=" + ",".join(changed))
        raise InstallError("Protected installed files changed during install: " + "; ".join(details))


def restore_backup(target: Path, backup: Path | None, remove_partial_new_target: bool) -> None:
    if backup is not None:
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(backup, target)
        return
    if remove_partial_new_target and target.exists():
        shutil.rmtree(target)


def copy_release_to_staging(source: Path, staging: Path) -> None:
    for file_path in source_files(source):
        rel = file_path.relative_to(source)
        if is_protected(rel):
            continue
        destination = staging / rel
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(file_path, destination)


def copy_target_protected_files(target: Path, staging: Path) -> list[str]:
    preserved: list[str] = []
    if not target.exists():
        return preserved

    for protected in sorted(PROTECTED_EXACT):
        source = target / protected
        if source.exists():
            destination = staging / protected
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination)
            preserved.append(relative_posix(protected))

    config_dir = target / "config"
    if config_dir.exists():
        for source in sorted(config_dir.glob("server_list.local.json.bak*")):
            if source.is_file():
                rel = source.relative_to(target)
                destination = staging / rel
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source, destination)
                preserved.append(relative_posix(rel))

    reports = target / "reports"
    if reports.exists():
        destination = staging / "reports"
        shutil.copytree(reports, destination, dirs_exist_ok=True)
        preserved.append("reports/")

    return preserved


def create_staging_dir(target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=f".{target.name}.install-", dir=str(target.parent)))


def replace_target_with_staging(target: Path, staging: Path) -> None:
    if target.exists():
        shutil.rmtree(target)
    shutil.move(str(staging), str(target))


def install(source: Path, target: Path) -> tuple[Path | None, list[str], bool]:
    source = source.resolve()
    target = target.resolve()
    validate_source(source)
    validate_install_paths(source, target)
    target_existed = target.exists()
    backup: Path | None = None
    protected_before: dict[str, str] = {}
    preserved: list[str] = []
    staging: Path | None = None
    try:
        backup = backup_target(target)
        protected_before = protected_snapshot(target)
        staging = create_staging_dir(target)
        copy_release_to_staging(source, staging)
        preserved.extend(copy_target_protected_files(target, staging))
        verify_protected_snapshot(staging, protected_before)
        replace_target_with_staging(target, staging)
        staging = None
        verify_protected_snapshot(target, protected_before)
    except InstallError as exc:
        restore_backup(target, backup, remove_partial_new_target=not target_existed)
        raise InstallError(f"Install failed; restored backup when available: {exc}") from exc
    except Exception as exc:
        restore_backup(target, backup, remove_partial_new_target=not target_existed)
        raise InstallError(f"Install failed; restored backup when available: {exc}") from exc
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    for protected in sorted(PROTECTED_EXACT):
        if (target / protected).exists() and relative_posix(protected) not in preserved:
            preserved.append(relative_posix(protected))
    reports = target / "reports"
    if reports.exists() and "reports/" not in preserved:
        preserved.append("reports/")
    config_dir = target / "config"
    if config_dir.exists():
        for backup_file in sorted(config_dir.glob("server_list.local.json.bak*")):
            rel = backup_file.relative_to(target)
            rel_text = relative_posix(rel)
            if rel_text not in preserved:
                preserved.append(rel_text)
    return backup, preserved, True


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Safely install the Erie Remote SSH skill.")
    parser.add_argument("--source", type=Path, required=True, help="Release or skill source directory.")
    parser.add_argument("--target", type=Path, default=default_target(), help="Installed skill directory.")
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        backup, preserved, preserved_hash_verified = install(args.source, args.target)
    except InstallError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if backup is not None:
        print(f"backup: {backup}")
    else:
        print("backup: none")
    print(f"target: {args.target.resolve()}")
    for item in preserved:
        print(f"preserved: {item}")
    print(f"preserved_hash_verified: {str(preserved_hash_verified).lower()}")
    print("status: installed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
