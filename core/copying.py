"""Safe materialized copies for tool-managed build inputs."""

from __future__ import annotations

from collections.abc import Callable
import os
from pathlib import Path
import shutil
import stat
import tempfile


class MaterializedCopyError(ValueError):
    """A source tree cannot be copied as regular files and directories."""


def _force_rmtree(path: Path) -> None:
    def repair(function: Callable[[str], object], name: str, _error: object) -> None:
        candidate = Path(name)
        for item in (candidate.parent, candidate):
            if item.exists() and not item.is_symlink():
                item.chmod(item.stat().st_mode | 0o700)
        function(name)

    shutil.rmtree(path, onerror=repair)


def _copy_node(
    source: Path,
    destination: Path,
    boundary: Path,
    ancestors: frozenset[tuple[int, int]],
) -> None:
    try:
        resolved = source.resolve(strict=True)
        if not resolved.is_relative_to(boundary):
            raise MaterializedCopyError(f"复制源链接指向项目之外：{source}")
        status = resolved.stat()
    except (OSError, RuntimeError) as exc:
        raise MaterializedCopyError(f"无法解析复制源：{source}: {exc}") from exc
    if stat.S_ISREG(status.st_mode):
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(resolved, destination, follow_symlinks=True)
        except OSError as exc:
            raise MaterializedCopyError(f"无法复制普通文件：{source}: {exc}") from exc
        return
    if not stat.S_ISDIR(status.st_mode):
        raise MaterializedCopyError(f"复制源包含不支持的特殊文件：{source}")
    identity = (status.st_dev, status.st_ino)
    if identity in ancestors:
        raise MaterializedCopyError(f"复制源包含循环目录链接：{source}")
    try:
        destination.mkdir()
        for child in sorted(resolved.iterdir(), key=lambda path: path.name):
            _copy_node(child, destination / child.name, boundary, ancestors | {identity})
        shutil.copystat(resolved, destination, follow_symlinks=True)
    except MaterializedCopyError:
        raise
    except OSError as exc:
        raise MaterializedCopyError(f"无法复制目录：{source}: {exc}") from exc


def copy_materialized_tree(source: Path, destination: Path, boundary: Path) -> None:
    """Copy a tree atomically while replacing safe links with real content."""

    boundary = boundary.expanduser().resolve()
    destination = destination.expanduser()
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_root = Path(
        tempfile.mkdtemp(prefix=f".{destination.name}.copy-", dir=destination.parent)
    )
    temporary = temporary_root / "tree"
    previous = temporary_root / "previous"
    moved_previous = False
    try:
        _copy_node(source, temporary, boundary, frozenset())
        if os.path.lexists(destination):
            destination.replace(previous)
            moved_previous = True
        try:
            temporary.replace(destination)
        except OSError:
            if moved_previous and os.path.lexists(previous):
                previous.replace(destination)
            raise
    except MaterializedCopyError:
        raise
    except OSError as exc:
        raise MaterializedCopyError(f"无法替换实体复制目标：{destination}: {exc}") from exc
    finally:
        if temporary_root.exists():
            _force_rmtree(temporary_root)
