"""在线测试：访问真实的网易云接口，需要会员 cookie。默认跳过。

运行：
    NETEASE_COOKIE_FILE=/path/to/cookie.txt pytest -m live
（Windows PowerShell: $env:NETEASE_COOKIE_FILE="D:\\path\\cookie.txt"; pytest -m live）
歌曲的可用音质可能随版权变化，失败时先确认歌曲本身的情况。
"""

import hashlib
import os
from dataclasses import replace
from pathlib import Path

import pytest
import requests
from mutagen import File as MutagenFile

from config import Settings
from music_api import CookieStore, NeteaseClient, parse_cookie_string
from music_downloader import MusicDownloader
from storage import CacheStore

pytestmark = pytest.mark.live

GU_YONG_ZHE = 1901371647  # 陈奕迅《孤勇者》：有 sky / jymaster / vivid，没有杜比
UNDERSTAND = 1931534562  # keshi《UNDERSTAND》：有杜比全景声
BIG_PLAYLIST = 2799009845  # 一万首以上的公开歌单


@pytest.fixture(scope="module")
def client() -> NeteaseClient:
    if os.environ.get("NETEASE_COOKIE"):
        return NeteaseClient(parse_cookie_string(os.environ["NETEASE_COOKIE"]))
    path = os.environ.get("NETEASE_COOKIE_FILE")
    if not path or not Path(path).is_file():
        pytest.skip("未设置 NETEASE_COOKIE 或 NETEASE_COOKIE_FILE")
    return NeteaseClient(CookieStore(path))


def test_logged_in(client):
    assert client.account()["logged_in"]


@pytest.mark.parametrize("level", ["standard", "higher", "exhigh", "lossless", "hires",
                                   "jyeffect", "sky", "vivid", "jymaster"])  # fmt: skip
def test_levels_are_not_silently_downgraded(client, level):
    source = client.song_url(GU_YONG_ZHE, level)
    assert source.available, source.error_message
    assert source.level == level


def test_dolby_and_fallback(client):
    assert client.song_url(UNDERSTAND, "dolby").level == "dolby"
    fallback = client.song_url(GU_YONG_ZHE, "dolby")
    assert fallback.available and fallback.downgraded


def test_md5_matches_download(client):
    source = client.song_url(GU_YONG_ZHE, "standard")
    data = requests.get(source.url, timeout=60).content
    assert len(data) == source.size and hashlib.md5(data).hexdigest() == source.md5


def test_big_playlist_is_complete(client):
    result = client.playlist(BIG_PLAYLIST)
    assert result["total"] > 10000
    assert len(result["tracks"]) + result["unavailable"] == result["total"]


def test_end_to_end_download_with_tags(client, tmp_path):
    settings = replace(Settings.from_env({}), data_dir=tmp_path)
    dl = MusicDownloader(client, CacheStore(settings.cache_dir), settings)
    result = dl.download(UNDERSTAND, "dolby")
    assert result.entry.ext == ".m4a" and result.entry.meta["tag_status"] == "ok"
    tags = MutagenFile(result.path).tags
    assert tags["\xa9nam"] == ["UNDERSTAND"] and tags["trkn"][0][1] > 1 and tags["covr"]
    assert dl.download(UNDERSTAND, "dolby").cached
