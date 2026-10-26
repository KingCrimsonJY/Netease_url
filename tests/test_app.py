from urllib.parse import unquote

import pytest

import music_downloader
from main import create_app
from music_downloader import MusicDownloader
from storage import CacheStore
from tests.conftest import JPEG, FakeFetcher


@pytest.fixture
def app(settings, fake_client, monkeypatch):
    monkeypatch.setattr(music_downloader, "fetch_cover", lambda *a, **k: (JPEG, "image/jpeg"))
    fetcher = FakeFetcher(fake_client, delay=0.2)
    dl = MusicDownloader(fake_client, CacheStore(settings.cache_dir), settings, fetcher=fetcher)
    app = create_app(settings, client=fake_client, downloader=dl)
    app.fetcher = fetcher
    return app


@pytest.fixture
def http(app):
    return app.test_client()


def wait_job(app, job_id):
    job = app.extensions["netease"].jobs.get(job_id)
    assert job.finished.wait(10)
    return job


def test_health(http):
    r = http.get("/health")
    assert r.status_code == 200 and r.get_json()["data"]["service"] == "running"


def test_song_url_reports_actual_level(http):
    d = http.get("/song?id=1&level=hires&type=url").get_json()["data"]
    assert d["level"] == "lossless" and d["requested_level"] == "hires" and d["downgraded"] is True
    r = http.get("/song?id=1&level=hires&type=url&strict=1")
    assert r.status_code == 409 and r.get_json()["error_code"] == "QUALITY_DOWNGRADED"


def test_song_url_unavailable_is_an_error(http, fake_client):
    fake_client.unavailable[1] = 404
    r = http.get("/song?id=1&type=url")
    assert r.status_code == 404 and r.get_json()["success"] is False
    assert r.get_json()["error_code"] == "UNAVAILABLE"


@pytest.mark.parametrize(
    "query", ["/song?id=abc", "/song", "/song?id=1&level=ultra", "/download?id=x"]
)
def test_invalid_input(http, query):
    r = http.get(query)
    assert r.status_code == 400 and r.get_json()["error_code"] == "INVALID_INPUT"


def test_async_job_and_file_download(app, http):
    assert http.get("/api/files/1/lossless").status_code == 404
    r = http.post("/api/jobs", json={"id": "https://music.163.com/#/song?id=1", "level": "hires"})
    assert r.status_code == 202
    job_id = r.get_json()["data"]["id"]
    # 同一首歌、同一音质的任务进行中时复用同一个任务
    assert (
        http.post("/api/jobs", json={"id": 1, "level": "hires"}).get_json()["data"]["id"] == job_id
    )
    wait_job(app, job_id)
    d = http.get(f"/api/jobs/{job_id}").get_json()["data"]
    assert d["status"] == "done" and d["progress"] == 100
    assert d["result"]["quality"] == "lossless" and d["result"]["downgraded"] is True
    assert d["file_url"] == "/api/files/1/lossless"
    f = http.get(d["file_url"])
    assert f.status_code == 200 and f.mimetype == "audio/flac"
    assert unquote(f.headers["X-Download-Filename"]) == "陈奕迅 - 孤勇者.flac"
    assert "filename*=UTF-8''" in f.headers["Content-Disposition"]
    partial = http.get(d["file_url"], headers={"Range": "bytes=0-3"})
    assert partial.status_code == 206 and partial.data == b"fLaC"
    assert app.fetcher.calls == 1


def test_failed_job_reports_reason(app, http, fake_client):
    fake_client.unavailable[1] = -110
    job_id = http.post("/api/jobs", data={"id": "1", "level": "lossless"}).get_json()["data"]["id"]
    wait_job(app, job_id)
    d = http.get(f"/api/jobs/{job_id}").get_json()["data"]
    assert d["status"] == "failed" and d["error_code"] == "NEED_VIP_OR_PURCHASE"
    assert http.get("/api/jobs/nope").status_code == 404


def test_legacy_download_endpoint(http):
    j = http.post("/Download", data={"id": "2", "quality": "standard", "format": "json"}).get_json()
    assert j["success"] and j["data"]["filename"] == "TWICE - What is Love？.mp3"
    assert "file_path" not in j["data"]  # 不再暴露服务器绝对路径
    f = http.get("/download?id=3&quality=standard")
    assert f.status_code == 200 and f.mimetype == "audio/mpeg"
    assert unquote(f.headers["X-Download-Filename"]) == "TWICE - What is Love？.mp3"


def test_legacy_download_errors_map_to_status(http, fake_client):
    fake_client.unavailable[1] = 404
    r = http.get("/download?id=1&format=json")
    assert r.status_code == 404 and r.get_json()["error_code"] == "UNAVAILABLE"
