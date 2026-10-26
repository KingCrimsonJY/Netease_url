import hashlib
import threading
from dataclasses import replace
from pathlib import Path

import pytest

import music_downloader
from config import Settings
from music_api import Lyrics, NeteaseError, TrackSource
from music_downloader import MusicDownloader
from storage import CacheStore

FIXTURES = Path(__file__).parent / "fixtures"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64
AUDIO = {
    "mp3": (FIXTURES / "silence.mp3").read_bytes(),
    "flac": (FIXTURES / "silence.flac").read_bytes(),
    "mp4": (FIXTURES / "silence.m4a").read_bytes(),
}
# 每个音质"实际返回"的文件类型
LEVEL_TYPES = {"standard": "mp3", "exhigh": "mp3", "lossless": "flac", "hires": "flac",
               "jymaster": "flac", "dolby": "mp4"}  # fmt: skip


def song(song_id: int, title: str = "孤勇者", artists=("陈奕迅",)) -> dict:
    return {
        "id": song_id,
        "name": title,
        "ar": [{"name": a} for a in artists],
        "al": {"id": 99, "name": "专辑", "picUrl": "http://p1.music.126.net/x/1.jpg"},
        "no": 1,
        "cd": "01",
        "privilege": {"plLevel": "jymaster"},
    }


class FakeClient:
    """模拟 NeteaseClient：可配置每个音质实际返回什么、歌曲是否可用。"""

    def __init__(self):
        self.songs = {
            1: song(1),
            2: song(2, "What is Love?", ["TWICE"]),
            3: song(3, "What is Love?", ["TWICE"]),
        }
        self.downgrade: dict[str, str] = {"hires": "lossless"}  # 请求 -> 实际
        self.unavailable: dict[int, int] = {}  # song_id -> code
        self.payload_override: dict[tuple[int, str], bytes] = {}
        self.url_calls = 0

    def payload(self, song_id: int, level: str) -> bytes:
        data = self.payload_override.get((song_id, level))
        if data is None:
            # 每首歌、每个音质的内容都不同，便于验证没有串用
            data = AUDIO[LEVEL_TYPES.get(level, "flac")] + f"{song_id}-{level}".encode()
        return data

    def song_url(self, song_id: int, level: str) -> TrackSource:
        self.url_calls += 1
        if song_id in self.unavailable:
            return TrackSource.from_api({"id": song_id, "code": self.unavailable[song_id]}, level)
        actual = self.downgrade.get(level, level)
        data = self.payload(song_id, actual)
        type_ = LEVEL_TYPES.get(actual, "flac")
        return TrackSource.from_api(
            {"id": song_id, "url": f"https://cdn.example/{song_id}/{actual}", "level": actual,
             "type": type_, "size": len(data), "md5": hashlib.md5(data).hexdigest(), "code": 200},
            level,
        )  # fmt: skip

    def song_detail(self, song_id: int) -> dict:
        if song_id not in self.songs:
            raise NeteaseError("未找到这首歌曲", 404)
        return self.songs[song_id]

    def album_tag_info(self, album_id: int) -> dict:
        return {"name": "专辑", "artists": ["陈奕迅"], "track_total": 10, "disc_total": 1,
                "publish_time": 1636300800000, "company": "Universal"}  # fmt: skip

    def lyrics(self, song_id: int) -> Lyrics:
        return Lyrics(lrc="[00:00.000] 作词 : 唐恬\n[00:01.00]歌词")


class FakeFetcher:
    """模拟 CDN 下载：把对应内容写进 .part，并统计调用次数。"""

    def __init__(self, client: FakeClient, delay: float = 0.0):
        self.client = client
        self.calls = 0
        self.corrupt_times = 0
        self.delay = delay
        self.lock = threading.Lock()

    def __call__(self, source: TrackSource, part: Path, progress) -> None:
        with self.lock:
            self.calls += 1
            corrupt = self.corrupt_times > 0
            if corrupt:
                self.corrupt_times -= 1
        if self.delay:
            threading.Event().wait(self.delay)
        data = self.client.payload(source.song_id, source.level)
        part.write_bytes(data)
        progress("downloading", len(data), len(data))
        if corrupt:
            part.unlink()
            raise music_downloader.DownloadError("CHECKSUM_MISMATCH")


@pytest.fixture
def settings(tmp_path) -> Settings:
    return replace(Settings.from_env({}), data_dir=tmp_path / "data", cover_size=0)


@pytest.fixture
def fake_client() -> FakeClient:
    return FakeClient()


@pytest.fixture
def fetcher(fake_client) -> FakeFetcher:
    return FakeFetcher(fake_client)


@pytest.fixture
def downloader(settings, fake_client, fetcher, monkeypatch) -> MusicDownloader:
    monkeypatch.setattr(music_downloader, "fetch_cover", lambda *a, **k: (JPEG, "image/jpeg"))
    store = CacheStore(settings.cache_dir, settings.max_cache_bytes)
    return MusicDownloader(fake_client, store, settings, fetcher=fetcher)
