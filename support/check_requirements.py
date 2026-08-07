"""Check the minimal GUI runtime without depending on pip internals."""

from __future__ import annotations

from importlib import import_module
from pathlib import Path
import shutil
import subprocess
import sys


def dependency_entries(path: Path) -> tuple[str, ...]:
    """Read package names while allowing comments and alternative names."""

    return tuple(
        token
        for raw_line in path.read_text(encoding="utf-8-sig").splitlines()
        for token in raw_line.split("#", 1)[0].split()
    )


def main() -> int:
    if sys.version_info < (3, 10):
        print("需要 Python 3.10 或更高版本。", file=sys.stderr)
        return 1
    try:
        import_module("PySide6")
    except (ImportError, OSError) as exc:
        print(f"无法导入 PySide6：{exc}", file=sys.stderr)
        return 1
    root = Path(__file__).resolve().parent.parent
    dependency_file = root / "support" / "dependencies.txt"
    if not dependency_file.is_file():
        print(f"未找到系统依赖清单：{dependency_file}", file=sys.stderr)
        return 1
    if shutil.which("dpkg-query"):
        try:
            entries = dependency_entries(dependency_file)
        except (OSError, UnicodeError) as exc:
            print(f"无法读取系统依赖清单：{exc}", file=sys.stderr)
            return 1
        missing = []
        for entry in entries:
            installed = False
            for package in entry.split("|"):
                result = subprocess.run(
                    ["dpkg-query", "-W", "-f=${Status}", package],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                if result.returncode == 0 and result.stdout.strip() == "install ok installed":
                    installed = True
                    break
            if not installed:
                missing.append(entry)
        if missing:
            print("缺少系统依赖：" + " ".join(missing), file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
