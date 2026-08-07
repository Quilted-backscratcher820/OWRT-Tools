"""Consistent timestamps for GUI and persisted operation logs."""

from __future__ import annotations

from datetime import datetime
import re


LOG_TIMESTAMP_FORMAT = "%Y/%m/%d %H:%M:%S"
_STAMPED_LINE = re.compile(r"^\d{4}/\d{2}/\d{2} \d{2}:\d{2}:\d{2}(?: |$)")


def timestamp_log_text(
    message: str,
    now: datetime | None = None,
    *,
    preserve_existing: bool = True,
) -> str:
    """Prefix every unstamped line while preserving already stamped worker output."""

    stamp = (now or datetime.now()).strftime(LOG_TIMESTAMP_FORMAT)
    lines = message.rstrip("\n").splitlines() or [""]
    return "\n".join(
        line
        if preserve_existing and _STAMPED_LINE.match(line)
        else f"{stamp} {line}"
        for line in lines
    )
