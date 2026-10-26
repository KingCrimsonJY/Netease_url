"""下载流程：获取链接 → 查缓存 → 下载（断点续传、md5 校验）→ 写标签 → 原子提交 → 容量控制。

命令行：
    python music_downloader.py info 1901371647 --level jymaster
    python music_downloader.py download 1901371647 --level lossless --out Music
    python music_downloader.py batch songs.txt --level exhigh --out Music --workers 5
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import os
import re
import shutil
import sys
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

import links
import lyrics as lyrics_mod
from config import Settings
from levels import level_name
from music_api import USER_AGENT, NeteaseClient, NeteaseError, TrackSource
from naming import build_filename
from storage import CacheEntry, CacheStore
from tagging import (
    EXT_BY_FORMAT,
    TAG_VERSION,
    TaggingError,
    build_tags,
    fetch_cover,
    read_song_id,
    sniff_format,
    write_tags,
)
from utils import atomic_write_text, format_size

log = logging.getLogger("netease.downloader")

ProgressCallback = Callable[[str, int, int], None]  # (阶段, 已完成字节, 总字节)
MAX_TAG_ATTEMPTS = 3
CHUNK = 256 * 1024

DOWNLOAD_ERRORS = {
    "CHECKSUM_MISMATCH": "下载的文件 md5 校验失败",
    "DOWNLOAD_FAILED": "下载失败",
    "QUALITY_DOWNGRADED": "实际音质与请求的不一致",
}


class DownloadError(Exception):
    def __init__(self, code: str, message: str | None = None):
        self.code = code
        self.message = message or DOWNLOAD_ERRORS.get(code, code)
        super().__init__(self.message)


DownloadException = DownloadError  # 兼容旧名称


@dataclass
class DownloadResult:
    entry: CacheEntry
    source: TrackSource
    cached: bool
    filename: str
    warnings: list[str] = field(default_factory=list)

    @property
    def path(self) -> Path:
        return self.entry.path

    def to_dict(self) -> dict[str, Any]:
        song = self.entry.song
        return {
            "music_id": self.entry.song_id,
            "name": song.get("title", ""),
            "artist": ", ".join(song.get("artists") or []),
            "album": song.get("album", ""),
            "quality": self.entry.level,
            "requested_quality": self.source.requested_level,
            "downgraded": self.entry.level != self.source.requested_level,
            "quality_name": level_name(self.entry.level),
            "file_type": self.entry.ext.lstrip("."),
            "file_size": self.entry.size,
            "file_size_formatted": format_size(self.entry.size),
            "filename": self.filename,
            "duration": self.source.duration_ms,
            "bitrate": self.entry.meta.get("bitrate"),
            "sample_rate": self.entry.meta.get("sample_rate"),
            "tag_status": self.entry.meta.get("tag_status"),
            "warnings": self.warnings or self.entry.meta.get("tag_warnings") or [],
            "cached": self.cached,
        }


def file_md5(path: Path) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


class MusicDownloader:
    def __init__(
        self,
        client: NeteaseClient,
        store: CacheStore,
        settings: Settings,
        *,
        fetcher: Callable[[TrackSource, Path, ProgressCallback], None] | None = None,
    ):
        self.client = client
        self.store = store
        self.settings = settings
        self.http = requests.Session()
        self.http.headers["User-Agent"] = USER_AGENT
        retry = Retry(
            total=3, connect=3, read=0, backoff_factor=0.5, status_forcelist=(500, 502, 503, 504)
        )
        self.http.mount("https://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
        self.http.mount("http://", HTTPAdapter(max_retries=retry, pool_maxsize=16))
        self.fetch = fetcher or self._fetch

    # ----------- 对外接口 -----------

    def download(
        self,
        song_id: int,
        level: str,
        *,
        strict: bool = False,
        progress: ProgressCallback | None = None,
    ) -> DownloadResult:
        progress = progress or (lambda *_: None)
        progress("resolving", 0, 0)
        source = self.client.song_url(song_id, level)
        if not source.available:
            raise DownloadError(source.error_code, source.error_message)
        if strict and source.downgraded:
            raise DownloadError(
                "QUALITY_DOWNGRADED",
                f"实际音质为{level_name(source.level)}，与请求的{level_name(level)}不一致",
            )
        actual = source.level or level
        cached = True
        warnings: list[str] = []
        entry = self._usable(song_id, actual, source)
        if entry is None or self._needs_retag(entry):
            with self.store.lock(song_id, actual):
                entry = self._usable(song_id, actual, source)
                if entry is None:
                    cached = False
                    entry, warnings = self._download_locked(source, progress)
                elif self._needs_retag(entry):
                    entry, warnings = self._retag_locked(entry, source)
        self.store.touch(entry)
        self.store.evict(protect={entry.path})
        progress("done", entry.size, entry.size)
        result = DownloadResult(entry, source, cached, self.display_name(entry), warnings)
        if self.settings.library_dir:
            try:
                self.export(result, self.settings.library_dir)
            except OSError as e:
                log.warning("导出到音乐库失败: %s", e)
        return result

    def display_name(self, entry: CacheEntry, template: str | None = None) -> str:
        s = self.settings
        info = {**entry.song, "level": entry.level, "id": entry.song_id}
        return build_filename(
            info,
            entry.ext,
            template=template or s.filename_template,
            separator=s.artist_separator,
            max_artists=s.max_artists_in_filename,
            fullwidth=s.fullwidth_replace,
        )

    def export(
        self, result: DownloadResult, target_dir: Path, *, template: str | None = None
    ) -> Path:
        """按友好文件名导出（同盘硬链接，否则复制）。同名但不是同一首歌时自动加专辑名或 ID 区分。"""
        target_dir.mkdir(parents=True, exist_ok=True)
        entry = result.entry
        name = self.display_name(entry, template)
        stem, ext = name[: -len(entry.ext)], entry.ext
        album = entry.song.get("album") or ""
        candidates = [name]
        if album:
            candidates.append(f"{stem} ({album}){ext}")
        candidates.append(f"{stem} [{entry.song_id}]{ext}")
        for candidate in candidates:
            dst = target_dir / build_filename(
                {"id": entry.song_id, "title": candidate[: -len(ext)]},
                ext,
                template="{title}",
                fullwidth=self.settings.fullwidth_replace,
            )
            if dst.exists():
                if read_song_id(dst) != entry.song_id:
                    continue  # 同名的另一首歌
                if dst.stat().st_size == entry.size:
                    break  # 已经导出过
            tmp = dst.with_name(f".{dst.name}.tmp")
            tmp.unlink(missing_ok=True)
            try:
                os.link(entry.path, tmp)
            except OSError:
                shutil.copy2(entry.path, tmp)
            os.replace(tmp, dst)
            break
        else:  # pragma: no cover - 候选名都被其他歌曲占用
            raise OSError(f"无法为 {name} 找到可用的文件名")
        lyric_text = entry.meta.get("lyrics") or ""
        if lyric_text and self.settings.lyrics_mode in ("lrc", "both"):
            atomic_write_text(dst.with_suffix(".lrc"), lyric_text)
        return dst

    # ----------- 缓存判断 -----------

    def _usable(self, song_id: int, level: str, source: TrackSource) -> CacheEntry | None:
        entry = self.store.find(song_id, level)
        if entry is None:
            return None
        if source.md5 and entry.meta.get("source_md5") not in (None, source.md5):
            log.info("歌曲 %s/%s 的源文件已更新，重新下载", song_id, level)
            return None
        return entry

    @staticmethod
    def _needs_retag(entry: CacheEntry) -> bool:
        if int(entry.meta.get("tag_version") or 0) < TAG_VERSION:
            return True
        return (
            entry.meta.get("tag_status") != "ok"
            and int(entry.meta.get("tag_attempts") or 0) < MAX_TAG_ATTEMPTS
        )

    # ----------- 下载与标签（调用方持有锁） -----------

    def _download_locked(
        self, source: TrackSource, progress: ProgressCallback
    ) -> tuple[CacheEntry, list[str]]:
        song_id, level = source.song_id, source.level
        self.store.remove(song_id, level)  # 清掉损坏或过期的旧条目
        part = self.store.part_path(song_id, level)
        part.parent.mkdir(parents=True, exist_ok=True)
        for attempt in (1, 2):
            try:
                self.fetch(source, part, progress)
                break
            except DownloadError as e:
                if e.code != "CHECKSUM_MISMATCH" or attempt == 2:
                    raise
                log.warning("歌曲 %s 的 md5 校验失败，重新下载", song_id)
        fmt = sniff_format(part)
        ext = EXT_BY_FORMAT.get(fmt or "") or source.ext or ".bin"
        progress("tagging", source.size, source.size)
        meta, warnings = self._tag(part, source)
        meta.update(
            source_md5=source.md5,
            source_size=source.size,
            type=source.type,
            bitrate=source.bitrate,
            sample_rate=source.sample_rate,
            duration_ms=source.duration_ms,
        )
        entry = self.store.commit(song_id, level, part, ext, meta)
        log.info("已缓存 %s/%s (%s)", song_id, level, format_size(entry.size))
        return entry, warnings

    def _retag_locked(self, entry: CacheEntry, source: TrackSource) -> tuple[CacheEntry, list[str]]:
        part = self.store.part_path(entry.song_id, entry.level)
        shutil.copyfile(entry.path, part)
        attempts = int(entry.meta.get("tag_attempts") or 0) + 1
        meta, warnings = self._tag(part, source, attempts=attempts)
        try:
            entry = self.store.commit(
                entry.song_id, entry.level, part, entry.ext, {**entry.meta, **meta}
            )
        except PermissionError:
            part.unlink(missing_ok=True)  # Windows 上文件正在被读取，下次再重写
        return entry, warnings

    def _tag(
        self, path: Path, source: TrackSource, attempts: int = 1
    ) -> tuple[dict[str, Any], list[str]]:
        s = self.settings
        warnings: list[str] = []
        song = self.client.song_detail(source.song_id)
        album = None
        album_id = (song.get("al") or {}).get("id")
        if album_id:
            try:
                album = self.client.album_tag_info(album_id)
            except NeteaseError:
                warnings.append("专辑信息获取失败")
        lyric_text = ""
        try:
            lyr = self.client.lyrics(source.song_id)
            if not lyr.no_lyrics:
                lyric_text = lyrics_mod.build(lyr.lrc, lyr.tlyric, translation=s.lyrics_translation)
        except NeteaseError:
            warnings.append("歌词获取失败")
        pic_url = (song.get("al") or {}).get("picUrl") or ""
        cover = fetch_cover(pic_url, s.cover_size, session=self.http) if pic_url else None
        if pic_url and cover is None:
            warnings.append("封面获取失败")
        tags = build_tags(song, album, lyric_text, cover)
        status = "partial" if warnings else "ok"
        try:
            write_tags(
                path,
                tags,
                id3_version=s.id3_version,
                embed_lyrics=s.lyrics_mode in ("embed", "both"),
            )
        except TaggingError as e:
            status = "failed"
            warnings.append(str(e))
            log.warning("歌曲 %s 写入标签失败: %s", source.song_id, e)
        meta = {
            "tag_version": TAG_VERSION,
            "tag_status": status,
            "tag_warnings": warnings,
            "tag_attempts": attempts,
            "song": tags.snapshot(),
            "lyrics": lyric_text,
        }
        return meta, warnings

    def _fetch(self, source: TrackSource, part: Path, progress: ProgressCallback) -> None:
        """下载到 .part：支持续传，网络中断自动重试，最后校验大小和 md5。"""
        expected = source.size
        last_error: Exception | None = None
        for attempt in range(4):
            have = part.stat().st_size if part.exists() else 0
            if expected and have > expected:
                part.unlink()
                have = 0
            if expected and have == expected:
                break
            headers = {"Range": f"bytes={have}-"} if have else {}
            if have:
                log.info("续传歌曲 %s：已有 %s", source.song_id, format_size(have))
            try:
                with self.http.get(
                    source.url, headers=headers, stream=True, timeout=(10, 60)
                ) as resp:
                    if resp.status_code == 416:
                        part.unlink(missing_ok=True)
                        continue
                    if resp.status_code not in (200, 206):
                        raise DownloadError("DOWNLOAD_FAILED", f"下载失败: HTTP {resp.status_code}")
                    if have and resp.status_code == 200:
                        have = 0  # 服务器没有按 Range 返回，从头下载
                    total = expected or have + int(resp.headers.get("Content-Length") or 0)
                    done = have
                    with open(part, "ab" if have else "wb") as f:
                        for chunk in resp.iter_content(CHUNK):
                            f.write(chunk)
                            done += len(chunk)
                            progress("downloading", done, total)
                if not expected or part.stat().st_size >= expected:
                    break
            except requests.RequestException as e:
                last_error = e
                time.sleep(1 + attempt)
        else:
            raise DownloadError(
                "DOWNLOAD_FAILED", f"下载失败: {last_error or '多次重试后仍未完成'}"
            )
        progress("verifying", expected, expected)
        size = part.stat().st_size
        if expected and size != expected:
            part.unlink(missing_ok=True)
            raise DownloadError("DOWNLOAD_FAILED", f"文件大小不符（{size} != {expected}）")
        if source.md5 and file_md5(part) != source.md5:
            part.unlink(missing_ok=True)
            raise DownloadError("CHECKSUM_MISMATCH")


# ============ 命令行 ============


def _parse_song_id(text: str) -> int | None:
    """歌曲 ID、链接或分享文本；兼容 "10825点" 这类只含一个数字的行。"""
    try:
        return links.parse(text, "song").id
    except links.LinkError:
        m = re.fullmatch(r"\D*?(\d{1,20})\D*", text.strip())
        return int(m.group(1)) if m and "music.163.com" not in text else None


def _build(settings: Settings) -> MusicDownloader:
    store = CacheStore(settings.cache_dir, settings.max_cache_bytes)
    store.cleanup_parts()
    return MusicDownloader(NeteaseClient.from_settings(settings), store, settings)


def _cmd_info(dl: MusicDownloader, args) -> int:
    song = dl.client.song_detail(args.id)
    source = dl.client.song_url(args.id, args.level)
    artists = "/".join(a.get("name", "") for a in song.get("ar") or [])
    print(f"歌曲: {song.get('name')} - {artists}")
    print(f"专辑: {(song.get('al') or {}).get('name', '')}")
    if not source.available:
        print(f"无法获取 {level_name(args.level)}: {source.error_message}")
        return 1
    flag = "（已降级）" if source.downgraded else ""
    print(
        f"音质: {level_name(source.level)}{flag}  {source.type}  {source.sample_rate}Hz  "
        f"{source.bitrate // 1000}kbps  {format_size(source.size)}"
    )
    return 0


def _cmd_download(dl: MusicDownloader, args) -> int:
    result = dl.download(args.id, args.level, strict=args.strict)
    path = dl.export(result, Path(args.out), template=args.template)
    flag = ""
    if result.source.downgraded:
        flag = f"（请求 {level_name(args.level)}，实际 {level_name(result.entry.level)}）"
    print(f"已保存: {path}{flag}")
    for w in result.warnings:
        print(f"  注意: {w}")
    return 0


def _cmd_batch(dl: MusicDownloader, args) -> int:
    ids: list[int] = []
    for n, line in enumerate(Path(args.file).read_text(encoding="utf-8-sig").splitlines(), 1):
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        song_id = _parse_song_id(line)
        if song_id is None:
            print(f"第 {n} 行无法识别，已跳过: {line.strip()!r}")
        else:
            ids.append(song_id)
    ids = list(dict.fromkeys(ids))
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    existing = set() if args.force else {read_song_id(p) for p in out.iterdir() if p.is_file()}
    todo = [i for i in ids if i not in existing]
    print(
        f"共 {len(ids)} 首，其中 {len(ids) - len(todo)} 首已在 {out} 中，开始下载 {len(todo)} 首..."
    )
    failed: dict[int, str] = {}

    def run(song_id: int) -> str:
        result = dl.download(song_id, args.level, strict=args.strict)
        path = dl.export(result, out, template=args.template)
        note = f"（实际 {level_name(result.entry.level)}）" if result.source.downgraded else ""
        return f"{path.name}{note}"

    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run, i): i for i in todo}
        for done, future in enumerate(as_completed(futures), 1):
            song_id = futures[future]
            try:
                print(f"[{done}/{len(todo)}] 完成: {future.result()}")
            except (DownloadError, NeteaseError) as e:
                failed[song_id] = e.message
                print(f"[{done}/{len(todo)}] 失败 {song_id}: {e.message}")
            except Exception as e:  # 单首失败不影响其他歌曲
                failed[song_id] = str(e)
                print(f"[{done}/{len(todo)}] 失败 {song_id}: {e}")
    if failed:
        print(f"\n{len(failed)} 首下载失败：")
        for song_id, reason in failed.items():
            print(f"  {song_id}: {reason}")
        return 1
    print("\n全部完成")
    return 0


def main(argv: list[str] | None = None) -> int:
    settings = Settings.from_env()
    parser = argparse.ArgumentParser(description="网易云音乐下载器")
    sub = parser.add_subparsers(dest="command", required=True)

    def common(p: argparse.ArgumentParser, out: bool = True) -> None:
        p.add_argument(
            "--level", "-l", default=settings.default_level, help="音质，默认 %(default)s"
        )
        if out:
            p.add_argument("--out", "-o", default="downloads", help="保存目录，默认 %(default)s")
            p.add_argument(
                "--template", "-t", default=None, help="文件名模板，例如 '{title}-{artists}'"
            )
            p.add_argument("--strict", action="store_true", help="实际音质与请求不一致时视为失败")

    p = sub.add_parser("info", help="查看歌曲信息和实际可得的音质")
    p.add_argument("id", type=_parse_song_id)
    common(p, out=False)
    p = sub.add_parser("download", help="下载单首歌曲")
    p.add_argument("id", type=_parse_song_id)
    common(p)
    p = sub.add_parser("batch", help="批量下载（文件中每行一个 ID 或链接）")
    p.add_argument("file")
    p.add_argument("--workers", "-w", type=int, default=settings.max_concurrent_downloads)
    p.add_argument("--force", action="store_true", help="目标目录已有同一首歌时也重新导出")
    common(p)
    args = parser.parse_args(argv)
    if getattr(args, "id", 1) is None:
        parser.error("无法识别的歌曲 ID 或链接")
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    dl = _build(settings)
    handler = {"info": _cmd_info, "download": _cmd_download, "batch": _cmd_batch}[args.command]
    try:
        return handler(dl, args)
    except (DownloadError, NeteaseError) as e:
        print(f"失败: {e.message}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
