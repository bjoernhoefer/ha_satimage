"""Atomic file replacement: readers see the old or the new file, never a part."""

from __future__ import annotations

import os
import threading
from pathlib import Path


def write_bytes_atomic(path: Path, data: bytes) -> None:
    path = Path(path)
    partial = path.with_name(f"{path.name}.{os.getpid()}-{threading.get_ident()}.part")
    try:
        with open(partial, "wb") as handle:
            handle.write(data)
        os.replace(partial, path)
    except BaseException:
        partial.unlink(missing_ok=True)
        raise


def write_text_atomic(path: Path, text: str, encoding: str = "utf-8") -> None:
    write_bytes_atomic(path, text.encode(encoding))
