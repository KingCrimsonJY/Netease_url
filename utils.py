"""通用小工具。"""

from __future__ import annotations

import os
import tempfile
import threading
import time
from collections import OrderedDict
from collections.abc import Callable, Hashable
from pathlib import Path
from typing import Any


class TTLCache:
    """线程安全、带过期时间和容量上限的简单缓存。"""

    def __init__(self, ttl: float, maxsize: int = 256):
        self.ttl = ttl
        self.maxsize = maxsize
        self._data: OrderedDict[Hashable, tuple[float, Any]] = OrderedDict()
        self._lock = threading.Lock()

    def get(self, key: Hashable) -> Any | None:
        with self._lock:
            item = self._data.get(key)
            if item is None:
                return None
            expires, value = item
            if expires < time.monotonic():
                del self._data[key]
                return None
            self._data.move_to_end(key)
            return value

    def set(self, key: Hashable, value: Any) -> None:
        with self._lock:
            self._data[key] = (time.monotonic() + self.ttl, value)
            self._data.move_to_end(key)
            while len(self._data) > self.maxsize:
                self._data.popitem(last=False)

    def get_or_set(self, key: Hashable, factory: Callable[[], Any]) -> Any:
        value = self.get(key)
        if value is None:
            value = factory()
            self.set(key, value)
        return value

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


def format_size(size: int | None) -> str:
    if not size:
        return "0B"
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.2f}{unit}" if unit != "B" else f"{int(value)}B"
        value /= 1024
    return f"{value:.2f}GB"


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """先写同目录临时文件再 os.replace，保证读取方永远看到完整内容。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def atomic_write_text(path: Path, text: str) -> None:
    atomic_write_bytes(path, text.encode("utf-8"))


def mask_secret(value: str | None, keep: int = 4) -> str:
    if not value:
        return ""
    if len(value) <= keep * 2:
        return "*" * len(value)
    return f"{value[:keep]}…{value[-keep:]}"
