"""下载缓存：按 (歌曲ID, 实际音质) 存放，原子提交，同一个键同时只有一个写入者。

目录结构：
    cache/{song_id}/{level}{ext}         音频文件（已写好标签）
    cache/{song_id}/{level}.json         元数据：md5、大小、标签状态、歌曲信息、最近访问时间
    cache/{song_id}/{level}.part         下载中的临时文件（可断点续传）
    cache/.locks/{song_id}-{level}.lock  跨进程锁（命令行与服务同时运行时也安全）

读取方只会看到"音频文件 + 元数据"都已提交的完整条目；大小与元数据不符的条目视为损坏。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from filelock import FileLock, Timeout

from utils import atomic_write_text

log = logging.getLogger("netease.storage")

TOUCH_INTERVAL = 60  # 最近访问时间的最小更新间隔（秒），避免频繁写元数据
PART_MAX_AGE = 24 * 3600


@dataclass
class CacheEntry:
    path: Path
    meta: dict[str, Any]

    @property
    def song_id(self) -> int:
        return int(self.meta["song_id"])

    @property
    def level(self) -> str:
        return self.meta["level"]

    @property
    def ext(self) -> str:
        return self.path.suffix

    @property
    def size(self) -> int:
        return int(self.meta.get("size") or 0)

    @property
    def song(self) -> dict[str, Any]:
        return self.meta.get("song") or {}


def _replace(src: Path, dst: Path, attempts: int = 5) -> None:
    """os.replace；Windows 上目标文件正被读取时会短暂失败，稍等重试。"""
    for i in range(attempts):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if i == attempts - 1:
                raise
            time.sleep(0.2 * (i + 1))


class CacheStore:
    def __init__(self, root: Path, max_bytes: int = 0):
        self.root = Path(root)
        self.max_bytes = max_bytes
        self.lock_dir = self.root / ".locks"
        self.lock_dir.mkdir(parents=True, exist_ok=True)
        self._guard = threading.Lock()
        self._thread_locks: dict[str, threading.Lock] = {}
        self._evict_lock = threading.Lock()

    # ----------- 路径 -----------

    def entry_dir(self, song_id: int) -> Path:
        return self.root / str(int(song_id))

    def meta_path(self, song_id: int, level: str) -> Path:
        return self.entry_dir(song_id) / f"{level}.json"

    def part_path(self, song_id: int, level: str) -> Path:
        return self.entry_dir(song_id) / f"{level}.part"

    # ----------- 锁 -----------

    def _thread_lock(self, key: str) -> threading.Lock:
        with self._guard:
            return self._thread_locks.setdefault(key, threading.Lock())

    @contextmanager
    def lock(self, song_id: int, level: str, *, blocking: bool = True) -> Iterator[bool]:
        """同一 (歌曲, 音质) 的独占锁。blocking=False 时拿不到锁就产出 False。"""
        key = f"{int(song_id)}-{level}"
        tlock = self._thread_lock(key)
        if not tlock.acquire(blocking=blocking):
            yield False
            return
        try:
            flock = FileLock(self.lock_dir / f"{key}.lock", timeout=-1 if blocking else 0)
            try:
                flock.acquire()
            except Timeout:
                yield False
                return
            try:
                yield True
            finally:
                flock.release()
        finally:
            tlock.release()

    # ----------- 读取 -----------

    def _read_meta(self, path: Path) -> dict[str, Any] | None:
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def find(self, song_id: int, level: str) -> CacheEntry | None:
        meta = self._read_meta(self.meta_path(song_id, level))
        if not meta or not meta.get("file"):
            return None
        path = self.entry_dir(song_id) / meta["file"]
        try:
            if path.stat().st_size != int(meta.get("size") or -1):
                log.warning("缓存文件大小与记录不符，视为损坏: %s", path)
                return None
        except OSError:
            return None
        return CacheEntry(path, meta)

    def entries(self) -> Iterator[CacheEntry]:
        for meta_file in self.root.glob("*/*.json"):
            meta = self._read_meta(meta_file)
            if meta and meta.get("file"):
                path = meta_file.parent / meta["file"]
                if path.exists():
                    yield CacheEntry(path, meta)

    def usage(self) -> dict[str, int]:
        count = size = 0
        for entry in self.entries():
            count += 1
            size += entry.size
        return {"files": count, "bytes": size}

    # ----------- 写入 -----------

    def commit(
        self, song_id: int, level: str, part: Path, ext: str, meta: dict[str, Any]
    ) -> CacheEntry:
        """把已校验、已写标签的 .part 原子地提交为正式条目。调用方必须持有锁。"""
        directory = self.entry_dir(song_id)
        final = directory / f"{level}{ext}"
        with open(part, "rb+") as f:
            os.fsync(f.fileno())
        _replace(part, final)
        # 同一音质如果以前存过其他扩展名，删掉旧文件
        for stale in directory.glob(f"{level}.*"):
            if stale not in (final, self.meta_path(song_id, level)) and stale.suffix not in (
                ".part",
            ):
                stale.unlink(missing_ok=True)
        now = time.time()
        meta = {
            **meta,
            "song_id": int(song_id),
            "level": level,
            "file": final.name,
            "size": final.stat().st_size,
            "created_at": meta.get("created_at", now),
            "last_access": now,
        }
        atomic_write_text(self.meta_path(song_id, level), json.dumps(meta, ensure_ascii=False))
        return CacheEntry(final, meta)

    def update_meta(self, entry: CacheEntry, **changes: Any) -> CacheEntry:
        entry.meta.update(changes)
        atomic_write_text(
            self.meta_path(entry.song_id, entry.level), json.dumps(entry.meta, ensure_ascii=False)
        )
        return entry

    def touch(self, entry: CacheEntry) -> None:
        if time.time() - float(entry.meta.get("last_access") or 0) < TOUCH_INTERVAL:
            return
        try:
            self.update_meta(entry, last_access=time.time())
        except OSError:
            pass

    def remove(self, song_id: int, level: str) -> None:
        """删除条目（调用方持有锁）。先删元数据，读取方立即视其为不存在。"""
        meta_path = self.meta_path(song_id, level)
        meta = self._read_meta(meta_path) or {}
        meta_path.unlink(missing_ok=True)
        if meta.get("file"):
            try:
                (self.entry_dir(song_id) / meta["file"]).unlink(missing_ok=True)
            except PermissionError:
                log.warning("文件正在被读取，暂时无法删除: %s", meta["file"])

    # ----------- 维护 -----------

    def evict(self, protect: set[Path] | None = None) -> list[Path]:
        """总大小超过上限时，按最近访问时间删除最旧的条目。正在被写的条目会跳过。"""
        if self.max_bytes <= 0:
            return []
        removed: list[Path] = []
        with self._evict_lock:
            entries = sorted(self.entries(), key=lambda e: float(e.meta.get("last_access") or 0))
            total = sum(e.size for e in entries)
            for entry in entries:
                if total <= self.max_bytes:
                    break
                if protect and entry.path in protect:
                    continue
                with self.lock(entry.song_id, entry.level, blocking=False) as locked:
                    if not locked:
                        continue
                    self.remove(entry.song_id, entry.level)
                if not entry.path.exists():
                    total -= entry.size
                    removed.append(entry.path)
        if removed:
            log.info("缓存超过上限，已清理 %d 个文件", len(removed))
        return removed

    def cleanup_parts(self, max_age: float = PART_MAX_AGE) -> int:
        """删除长时间没有继续的下载临时文件。"""
        count = 0
        cutoff = time.time() - max_age
        for part in self.root.glob("*/*.part"):
            song_id, level = part.parent.name, part.stem
            try:
                if part.stat().st_mtime > cutoff:
                    continue
            except OSError:
                continue
            with self.lock(int(song_id), level, blocking=False) as locked:
                if locked:
                    part.unlink(missing_ok=True)
                    count += 1
        return count
