from dataclasses import replace

import pytest

from main import create_app
from music_downloader import MusicDownloader
from security import RateLimiter
from storage import CacheStore


def make_app(settings, fake_client, **overrides):
    settings = replace(settings, **overrides)
    dl = MusicDownloader(
        fake_client, CacheStore(settings.cache_dir), settings, fetcher=lambda *a: None
    )
    return create_app(settings, client=fake_client, downloader=dl)


@pytest.fixture
def secured(settings, fake_client):
    return make_app(settings, fake_client, api_token="s3cret").test_client()


def test_token_required_except_public_pages(secured):
    assert secured.get("/health").status_code == 200
    assert secured.get("/").status_code == 200
    r = secured.get("/api/info")
    assert r.status_code == 401 and r.get_json()["error_code"] == "UNAUTHORIZED"
    assert secured.get("/api/info", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert secured.get("/api/info", headers={"Authorization": "Bearer s3cret"}).status_code == 200
    assert secured.get("/api/info", headers={"X-API-Token": "s3cret"}).status_code == 200
    assert secured.get("/api/info?token=s3cret").status_code == 200


def test_no_cors_by_default_but_security_headers(settings, fake_client):
    http = make_app(settings, fake_client).test_client()
    r = http.get("/api/info", headers={"Origin": "https://evil.example"})
    assert "Access-Control-Allow-Origin" not in r.headers
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["Referrer-Policy"] == "no-referrer"


def test_cors_allowlist(settings, fake_client):
    http = make_app(settings, fake_client, cors_origins=("https://a.example",)).test_client()
    ok = http.get("/api/info", headers={"Origin": "https://a.example"})
    assert ok.headers["Access-Control-Allow-Origin"] == "https://a.example"
    bad = http.get("/api/info", headers={"Origin": "https://b.example"})
    assert "Access-Control-Allow-Origin" not in bad.headers
    pre = http.open("/api/jobs", method="OPTIONS", headers={"Origin": "https://a.example"})
    assert pre.status_code == 200 and "Authorization" in pre.headers["Access-Control-Allow-Headers"]


def test_rate_limit_applies_to_upstream_endpoints_only(settings, fake_client):
    http = make_app(settings, fake_client, rate_limit_per_minute=2).test_client()
    codes = [http.get("/song?id=1&type=url").status_code for _ in range(3)]
    assert codes == [200, 200, 429]
    assert all(http.get("/api/info").status_code == 200 for _ in range(5))


def test_rate_limiter_refills():
    limiter = RateLimiter(60)
    assert all(limiter.allow("ip") for _ in range(60))
    assert not limiter.allow("ip") and limiter.allow("other")
    assert RateLimiter(0).allow("x")


def test_status_reports_membership_expiry(settings, fake_client):
    d = make_app(settings, fake_client).test_client().get("/api/status").get_json()["data"]
    acc = d["account"]
    assert acc["logged_in"] and acc["vip_name"] == "黑胶SVIP" and acc["days_left"] == 11
    assert d["cookie_configured"] and d["warnings"] == []


def test_status_without_cookie(settings, fake_client):
    fake_client.cookie = {}
    d = make_app(settings, fake_client).test_client().get("/api/status").get_json()["data"]
    assert d["account"] == {"logged_in": False} and "MUSIC_U" in d["warnings"][0]


def test_qr_login_flow_saves_cookie(settings, fake_client):
    settings.cookie_file.write_text("MUSIC_U=old; appver=1.0", encoding="utf-8")
    http = make_app(settings, fake_client).test_client()
    d = http.post("/api/login/qr").get_json()["data"]
    assert d["key"] == "key-0123456789" and d["qr_svg"].lstrip().startswith("<")
    states = [http.get(f"/api/login/qr/check?key={d['key']}").get_json()["data"] for _ in range(3)]
    assert [s["status"] for s in states] == ["waiting", "scanned", "success"]
    assert "测试用户" in states[1]["message"]
    saved = settings.cookie_file.read_text(encoding="utf-8")
    assert "MUSIC_U=new" in saved and "appver=1.0" in saved and "__csrf=c" in saved
    assert (
        settings.cookie_file.with_name("cookie.txt.bak")
        .read_text(encoding="utf-8")
        .startswith("MUSIC_U=old")
    )
    assert http.get("/api/login/qr/check?key=../../x").status_code == 400
