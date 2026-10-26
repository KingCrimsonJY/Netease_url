"""异步下载任务：后台线程池执行，前端轮询进度，完成后由浏览器原生下载文件。

任务状态只保存在内存里，所以服务以单进程多线程方式运行（waitress）。
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Any

from levels import level_name
from music_api import NeteaseError
from music_downloader import DownloadError, DownloadResult, MusicDownloader

log = logging.getLogger("netease.jobs")

STAGE_NAMES = {
    "queued": "排队中",
    "resolving": "获取链接",
    "downloading": "下载中",
    "verifying": "校验中",
    "tagging": "写入标签",
    "done": "已完成",
    "failed": "失败",
}


@dataclass
class Job:
    id: str
    song_id: int
    level: str
    strict: bool = False
    status: str = "queued"
    downloaded: int = 0
    total: int = 0
    result: DownloadResult | None = None
    error_code: str | None = None
    error: str | None = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)
    finished: threading.Event = field(default_factory=threading.Event, repr=False)

    @property
    def done(self) -> bool:
        return self.finished.is_set()

    def to_dict(self) -> dict[str, Any]:
        progress = 100 if self.status == "done" else 0
        if self.status not in ("done", "failed") and self.total:
            progress = min(99, int(self.downloaded * 100 / self.total))
        data: dict[str, Any] = {
            "id": self.id,
            "song_id": self.song_id,
            "requested_level": self.level,
            "requested_quality_name": level_name(self.level),
            "status": self.status,
            "status_name": STAGE_NAMES.get(self.status, self.status),
            "progress": progress,
            "downloaded": self.downloaded,
            "total": self.total,
            "error_code": self.error_code,
            "error": self.error,
        }
        if self.result is not None:
            entry = self.result.entry
            data["result"] = self.result.to_dict()
            data["file_url"] = f"/api/files/{entry.song_id}/{entry.level}"
        return data


class JobManager:
    def __init__(self, downloader: MusicDownloader, max_workers: int = 3, ttl: float = 3600):
        self.downloader = downloader
        self.ttl = ttl
        self._executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="download")
        self._jobs: dict[str, Job] = {}
        self._active: dict[tuple[int, str, bool], str] = {}
        self._lock = threading.Lock()

    def submit(self, song_id: int, level: str, *, strict: bool = False) -> Job:
        """提交下载；同一首歌、同一音质的任务正在进行时直接返回那个任务。"""
        key = (int(song_id), level, strict)
        with self._lock:
            self._prune()
            active_id = self._active.get(key)
            if active_id and active_id in self._jobs:
                return self._jobs[active_id]
            job = Job(uuid.uuid4().hex, int(song_id), level, strict)
            self._jobs[job.id] = job
            self._active[key] = job.id
        self._executor.submit(self._run, job, key)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def _run(self, job: Job, key: tuple[int, str, bool]) -> None:
        def progress(stage: str, done: int, total: int) -> None:
            if stage != "done":
                job.status = stage
            job.downloaded, job.total = done, total
            job.updated_at = time.time()

        try:
            job.result = self.downloader.download(
                job.song_id, job.level, strict=job.strict, progress=progress
            )
            job.status = "done"
        except DownloadError as e:
            job.status, job.error_code, job.error = "failed", e.code, e.message
        except NeteaseError as e:
            job.status, job.error_code, job.error = "failed", "UPSTREAM_ERROR", e.message
        except Exception:
            log.exception("下载任务 %s 异常", job.id)
            job.status, job.error_code, job.error = "failed", "INTERNAL_ERROR", "服务器内部错误"
        finally:
            job.updated_at = time.time()
            with self._lock:
                if self._active.get(key) == job.id:
                    del self._active[key]
            job.finished.set()

    def _prune(self) -> None:
        cutoff = time.time() - self.ttl
        for job_id in [j.id for j in self._jobs.values() if j.done and j.updated_at < cutoff]:
            del self._jobs[job_id]

    def shutdown(self) -> None:
        self._executor.shutdown(wait=False, cancel_futures=True)
