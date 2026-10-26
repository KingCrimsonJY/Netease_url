import hashlib
import threading
from dataclasses import replace

import pytest
import responses
from mutagen.flac import FLAC
from mutagen.id3 import ID3

import music_downloader
from music_downloader import DownloadError, MusicDownloader
from storage import CacheStore
from tagging import read_song_id
from tests.conftest import JPEG, FakeFetcher


def test_cache_key_is_actual_level(downloader, fake_client, fetcher):
    # 请求 hires 实际只有 lossless：缓存在 lossless 名下，之后请求 lossless 直接命中
    r1 = downloader.download(1, "hires")
    assert r1.entry.level == "lossless" and r1.source.downgraded and not r1.cached
    r2 = downloader.download(1, "lossless")
    assert r2.cached and r2.path == r1.path
    assert fetcher.calls == 1


def test_different_levels_never_share_files(downloader, fake_client):
    std = downloader.download(1, "standard")
    ex = downloader.download(1, "exhigh")
    assert std.path != ex.path
    assert std.path.read_bytes() != ex.path.read_bytes()
    jm = downloader.download(1, "jymaster")
    ll = downloader.download(1, "lossless")
    assert jm.path != ll.path and jm.entry.level == "jymaster" and ll.entry.level == "lossless"


def test_same_name_different_songs_are_separate(downloader):
    a = downloader.download(2, "standard")
    b = downloader.download(3, "standard")
    assert a.filename == b.filename == "TWICE - What is Love？.mp3"
    assert a.path != b.path
    assert read_song_id(a.path) == 2 and read_song_id(b.path) == 3


def test_concurrent_requests_download_once(settings, fake_client, monkeypatch):
    monkeypatch.setattr(music_downloader, "fetch_cover", lambda *a, **k: (JPEG, "image/jpeg"))
    fetcher = FakeFetcher(fake_client, delay=0.3)
    dl = MusicDownloader(fake_client, CacheStore(settings.cache_dir), settings, fetcher=fetcher)
    results, errors = [], []

    def run():
        try:
            results.append(dl.download(1, "lossless"))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=run) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors and fetcher.calls == 1
    assert len({r.path for r in results}) == 1
    assert sum(not r.cached for r in results) == 1
    assert FLAC(results[0].path)["TITLE"] == ["孤勇者"]


def test_checksum_mismatch_retries_once(downloader, fetcher):
    fetcher.corrupt_times = 1
    assert downloader.download(1, "lossless").entry.level == "lossless"
    assert fetcher.calls == 2
    fetcher.corrupt_times = 2
    with pytest.raises(DownloadError) as e:
        downloader.download(1, "exhigh")
    assert e.value.code == "CHECKSUM_MISMATCH"


def test_strict_and_unavailable(downloader, fake_client):
    with pytest.raises(DownloadError) as e:
        downloader.download(1, "hires", strict=True)
    assert e.value.code == "QUALITY_DOWNGRADED"
    fake_client.unavailable[1] = -110
    with pytest.raises(DownloadError) as e:
        downloader.download(1, "lossless")
    assert e.value.code == "NEED_VIP_OR_PURCHASE"


def test_corrupted_cache_is_redownloaded(downloader, fetcher):
    first = downloader.download(1, "lossless")
    first.path.write_bytes(b"truncated")
    again = downloader.download(1, "lossless")
    assert not again.cached and fetcher.calls == 2
    assert FLAC(again.path)["TITLE"] == ["孤勇者"]


def test_upstream_file_change_is_redownloaded(downloader, fake_client, fetcher):
    downloader.download(1, "lossless")
    fake_client.payload_override[(1, "lossless")] = fake_client.payload(1, "lossless") + b"v2"
    assert not downloader.download(1, "lossless").cached
    assert fetcher.calls == 2


def test_tags_written_and_reported(downloader):
    r = downloader.download(1, "standard")
    id3 = ID3(r.path)
    assert str(id3["TIT2"]) == "孤勇者" and str(id3["TRCK"]) == "1/10"
    assert id3.getall("APIC")[0].data == JPEG and str(id3["TEXT"]) == "唐恬"
    info = r.to_dict()
    assert (
        info["tag_status"] == "ok" and info["quality"] == "standard" and info["file_type"] == "mp3"
    )


def test_tag_failure_is_reported_and_retried(downloader, fake_client, fetcher):
    fake_client.payload_override[(1, "lossless")] = b"not-an-audio-file" * 10
    r = downloader.download(1, "lossless")
    assert r.entry.meta["tag_status"] == "failed" and r.warnings
    downloader.download(1, "lossless")  # 自动重试写标签，但不重新下载
    assert downloader.store.find(1, "lossless").meta["tag_attempts"] == 2
    assert fetcher.calls == 1


def test_export_to_library(downloader, tmp_path):
    lib = tmp_path / "Music"
    a = downloader.download(2, "standard")
    b = downloader.download(3, "standard")
    pa = downloader.export(a, lib)
    pb = downloader.export(b, lib)
    assert pa.name == "TWICE - What is Love？.mp3"
    assert pb.name == "TWICE - What is Love？ (专辑).mp3"  # 同名不同歌，用专辑名区分
    assert downloader.export(a, lib) == pa  # 再次导出同一首歌不会产生新文件
    assert sorted(p.name for p in lib.iterdir()) == sorted([pa.name, pb.name])
    custom = downloader.export(a, tmp_path / "Other", template="{title}-{artists}")
    assert custom.name == "What is Love？-TWICE.mp3"


@responses.activate
def test_real_fetch_resumes_with_range(settings, fake_client):
    data = fake_client.payload(1, "lossless")
    source = fake_client.song_url(1, "lossless")

    def cdn(request):
        start = int(request.headers["Range"].split("=")[1].rstrip("-"))
        return 206, {"Content-Range": f"bytes {start}-{len(data) - 1}/{len(data)}"}, data[start:]

    responses.add_callback(responses.GET, source.url, callback=cdn)
    dl = MusicDownloader(fake_client, CacheStore(settings.cache_dir), settings)
    part = dl.store.part_path(1, "lossless")
    part.parent.mkdir(parents=True)
    part.write_bytes(data[:100])  # 上次中断留下的前 100 字节
    dl.fetch(source, part, lambda *a: None)
    assert part.read_bytes() == data
    assert responses.calls[0].request.headers["Range"] == "bytes=100-"
    assert hashlib.md5(part.read_bytes()).hexdigest() == source.md5


@responses.activate
def test_real_fetch_rejects_bad_md5(settings, fake_client):
    source = replace(fake_client.song_url(1, "lossless"), md5="0" * 32)
    responses.get(source.url, body=fake_client.payload(1, "lossless"))
    dl = MusicDownloader(fake_client, CacheStore(settings.cache_dir), settings)
    part = dl.store.part_path(1, "lossless")
    part.parent.mkdir(parents=True)
    with pytest.raises(DownloadError) as e:
        dl.fetch(source, part, lambda *a: None)
    assert e.value.code == "CHECKSUM_MISMATCH" and not part.exists()
