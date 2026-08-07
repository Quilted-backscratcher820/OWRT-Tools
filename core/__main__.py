"""Canonical module entry point used by the Linux/WSL launcher."""

from __future__ import annotations

from pathlib import Path

from .gui import run


if __name__ == "__main__":
    raise SystemExit(run(Path(__file__).resolve().parent.parent))
