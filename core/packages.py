"""Safe staging helpers for prebuilt OpenWrt IPK and APK archives."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import tempfile

from .models import PrebuiltPackageSpec


PACKAGE_SUFFIXES = frozenset({".apk", ".ipk"})
MAX_PACKAGE_SIZE = 512 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class PrebuiltPackageError(ValueError):
    """A selected archive cannot be staged safely."""


def package_sha256(path: Path) -> str:
    """Return the SHA-256 of one regular archive without loading it all in memory."""

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_filename(source: Path, digest: str) -> str:
    suffix = source.suffix.lower()
    stem = source.name[: -len(source.suffix)] if source.suffix else source.name
    name = _SAFE_NAME.sub("_", stem).strip("._") or "package"
    name = f"{name}{suffix}"
    return f"{digest[:16]}-{name}"


def stage_prebuilt_package(source: Path, destination: Path) -> PrebuiltPackageSpec:
    """Atomically copy a user-selected archive into the project's private staging area."""

    source = source.expanduser().resolve()
    if source.suffix.lower() not in PACKAGE_SUFFIXES:
        raise PrebuiltPackageError("预编译软件包只支持 .ipk 或 .apk 文件。")
    try:
        status = source.stat()
    except OSError as exc:
        raise PrebuiltPackageError(f"无法读取预编译软件包：{source}: {exc}") from exc
    if not source.is_file():
        raise PrebuiltPackageError(f"预编译软件包不是普通文件：{source}")
    if status.st_size <= 0:
        raise PrebuiltPackageError("预编译软件包为空文件。")
    if status.st_size > MAX_PACKAGE_SIZE:
        raise PrebuiltPackageError("预编译软件包超过 512 MiB，拒绝导入。")

    destination = destination.expanduser().resolve()
    try:
        destination.mkdir(parents=True, exist_ok=True)
        digest = package_sha256(source)
    except OSError as exc:
        raise PrebuiltPackageError(f"无法读取或创建预编译软件包目录：{exc}") from exc
    filename = _safe_filename(source, digest)
    target = destination / filename
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise PrebuiltPackageError(f"预编译软件包目标已被占用：{filename}")
    if target.is_file():
        try:
            existing_digest = package_sha256(target)
        except OSError as exc:
            raise PrebuiltPackageError(f"无法校验已导入软件包：{exc}") from exc
        if existing_digest == digest:
            return PrebuiltPackageSpec(filename=filename, sha256=digest)

    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{filename}.", suffix=".tmp", dir=destination
    )
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        temporary.chmod(0o644)
        if package_sha256(temporary) != digest:
            raise PrebuiltPackageError("复制后的预编译软件包校验失败。")
        temporary.replace(target)
    except OSError as exc:
        raise PrebuiltPackageError(f"无法保存预编译软件包：{exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)
    return PrebuiltPackageSpec(filename=filename, sha256=digest)


def verify_staged_package(directory: Path, package: PrebuiltPackageSpec) -> Path:
    """Return a verified staged archive, rejecting path escape and later modifications."""

    directory = directory.expanduser().resolve()
    raw_candidate = directory / package.filename
    candidate = raw_candidate.resolve()
    if (
        not candidate.is_relative_to(directory)
        or raw_candidate.is_symlink()
        or not candidate.is_file()
    ):
        raise PrebuiltPackageError(f"预编译软件包不存在：{package.filename}")
    if package_sha256(candidate) != package.sha256:
        raise PrebuiltPackageError(f"预编译软件包校验失败：{package.filename}")
    return candidate
