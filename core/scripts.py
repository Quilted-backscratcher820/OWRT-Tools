"""Safe preparation and integrity checks for user-selected build scripts."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

from .models import ScriptSpec


MAX_SCRIPT_SIZE = 4 * 1024 * 1024
_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


class ScriptError(ValueError):
    """A selected build script cannot be prepared or verified."""


def script_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _script_filename(source: Path, digest: str) -> str:
    stem = _SAFE_NAME.sub("_", source.stem).strip("._") or "script"
    return f"{digest[:16]}-{stem}.sh"


def stage_build_script(source: Path, destination: Path) -> ScriptSpec:
    """Copy, convert, syntax-check and authorize one shell script atomically."""

    source = source.expanduser().resolve()
    if source.suffix.lower() != ".sh":
        raise ScriptError("自定义脚本只支持 .sh 文件。")
    try:
        status = source.stat()
    except OSError as exc:
        raise ScriptError(f"无法读取自定义脚本：{source}: {exc}") from exc
    if not source.is_file():
        raise ScriptError(f"自定义脚本不是普通文件：{source}")
    if status.st_size <= 0:
        raise ScriptError("自定义脚本为空文件。")
    if status.st_size > MAX_SCRIPT_SIZE:
        raise ScriptError("自定义脚本超过 4 MiB，拒绝导入。")
    try:
        with source.open("rb") as handle:
            if b"\0" in handle.read(4096):
                raise ScriptError("自定义脚本包含空字节，不是文本脚本。")
    except OSError as exc:
        raise ScriptError(f"无法读取自定义脚本：{source}: {exc}") from exc

    dos2unix = shutil.which("dos2unix")
    bash = shutil.which("bash")
    if not dos2unix:
        raise ScriptError("未找到 dos2unix，无法转换脚本换行格式。")
    if not bash:
        raise ScriptError("未找到 bash，无法校验脚本语法。")

    raw_destination = destination.expanduser()
    if raw_destination.is_symlink():
        raise ScriptError("脚本暂存目录不能是符号链接。")
    destination = raw_destination.resolve()
    try:
        destination.mkdir(parents=True, exist_ok=True)
        descriptor, temporary_name = tempfile.mkstemp(
            prefix=".selected-script.", suffix=".sh", dir=destination
        )
        os.close(descriptor)
    except OSError as exc:
        raise ScriptError(f"无法创建脚本暂存目录：{exc}") from exc
    temporary = Path(temporary_name)
    try:
        shutil.copyfile(source, temporary)
        converted = subprocess.run(
            (dos2unix, "--", str(temporary)),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if converted.returncode:
            detail = converted.stderr.strip() or converted.stdout.strip()
            raise ScriptError(f"dos2unix 转换失败：{detail or converted.returncode}")
        checked = subprocess.run(
            (bash, "-n", "--", str(temporary)),
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
        if checked.returncode:
            detail = checked.stderr.strip() or checked.stdout.strip()
            raise ScriptError(f"脚本语法校验失败：{detail or checked.returncode}")
        digest = script_sha256(temporary)
        filename = _script_filename(source, digest)
        target = destination / filename
        if target.is_symlink() or (target.exists() and not target.is_file()):
            raise ScriptError(f"脚本暂存目标已被占用：{filename}")
        temporary.chmod(0o755)
        if target.is_file() and script_sha256(target) == digest:
            target.chmod(0o755)
        else:
            temporary.replace(target)
        return ScriptSpec(filename=filename, sha256=digest)
    except ScriptError:
        raise
    except subprocess.TimeoutExpired as exc:
        raise ScriptError("脚本转换或语法校验超时。") from exc
    except OSError as exc:
        raise ScriptError(f"无法准备自定义脚本：{exc}") from exc
    finally:
        temporary.unlink(missing_ok=True)


def verify_staged_script(directory: Path, script: ScriptSpec) -> Path:
    """Return an executable staged script after path and digest validation."""

    raw_directory = directory.expanduser()
    if raw_directory.is_symlink():
        raise ScriptError("自定义脚本暂存目录不能是符号链接。")
    directory = raw_directory.resolve()
    raw_candidate = directory / script.filename
    candidate = raw_candidate.resolve()
    if (
        not candidate.is_relative_to(directory)
        or raw_candidate.is_symlink()
        or not candidate.is_file()
    ):
        raise ScriptError(f"自定义脚本不存在：{script.filename}")
    try:
        if script_sha256(candidate) != script.sha256:
            raise ScriptError(f"自定义脚本校验失败：{script.filename}")
        if not os.access(candidate, os.X_OK):
            raise ScriptError(f"自定义脚本缺少执行权限：{script.filename}")
    except OSError as exc:
        raise ScriptError(f"无法校验自定义脚本：{exc}") from exc
    return candidate
