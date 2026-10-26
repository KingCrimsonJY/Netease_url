import json
import re
from urllib.parse import parse_qs

import pytest
import responses

from music_api import (
    API_BASE,
    EAPI_BASE,
    WEB_BASE,
    CookieStore,
    NeteaseClient,
    NeteaseError,
    TrackSource,
    parse_cookie_string,
)

SONG_URL = EAPI_BASE + "/song/enhance/player/url/v1"


def cookie_header(call) -> dict[str, str]:
    return parse_cookie_string(call.request.headers["Cookie"])


def url_item(**kw):
    item = {"id": 1, "url": "https://m.example/1.flac", "level": "lossless", "type": "flac",
            "size": 100, "md5": "ABC", "br": 900000, "sr": 44100, "time": 1000, "code": 200,
            "expi": 1200, "freeTrialInfo": None}  # fmt: skip
    item.update(kw)
    return item


@responses.activate
def test_device_defaults_are_merged_into_every_request():
    responses.post(SONG_URL, json={"code": 200, "data": [url_item()]})
    responses.post(API_BASE + "/v3/song/detail", json={"code": 200, "songs": [], "privileges": []})
    client = NeteaseClient({"MUSIC_U": "u"})
    client.song_url(1, "lossless")
    client.song_details_raw([1])
    for call in responses.calls:
        sent = cookie_header(call)
        assert sent["MUSIC_U"] == "u"
        assert sent["os"] == "pc"
        assert sent["appver"] == "3.1.17.204416"


@responses.activate
def test_user_cookie_wins_but_vivid_forces_android():
    responses.post(SONG_URL, json={"code": 200, "data": [url_item(level="vivid", type="av3a")]})
    client = NeteaseClient({"MUSIC_U": "u", "appver": "9.9.9"})
    client.song_url(1, "lossless")
    assert cookie_header(responses.calls[0])["appver"] == "9.9.9"
    client.song_url(1, "vivid")
    sent = cookie_header(responses.calls[1])
    assert (sent["os"], sent["appver"]) == ("android", "9.5.61")


@responses.activate
def test_song_url_payload_per_level():
    responses.post(SONG_URL, json={"code": 200, "data": [url_item()]})
    client = NeteaseClient({})
    captured = []
    real = client.eapi_response

    def spy(path, payload, **kw):
        captured.append(dict(payload))
        return real(path, payload, **kw)

    client.eapi_response = spy
    client.song_url(1, "sky")
    client.song_url(1, "dolby")
    assert captured[0]["immerseType"] == "c51"
    assert captured[1]["encodeType"] == "mp4"


def test_track_source_flags():
    s = TrackSource.from_api(url_item(level="lossless"), "jymaster")
    assert s.available and s.downgraded and s.error_code is None
    assert s.ext == ".flac" and s.md5 == "abc"
    assert TrackSource.from_api(url_item(type="mp4"), "dolby").ext == ".m4a"
    assert TrackSource.from_api(url_item(type="av3a"), "vivid").ext == ".m4a"
    assert (
        TrackSource.from_api(url_item(url=None, code=-110), "sky").error_code
        == "NEED_VIP_OR_PURCHASE"
    )
    assert TrackSource.from_api(url_item(url=None, code=404), "sky").error_code == "UNAVAILABLE"
    trial = TrackSource.from_api(url_item(freeTrialInfo={"start": 0, "end": 30}), "lossless")
    assert not trial.available and trial.error_code == "TRIAL_ONLY"


@responses.activate
def test_song_details_batches_and_keeps_order():
    def detail(request):
        ids = [x["id"] for x in json.loads(parse_qs(request.body)["c"][0])]
        songs = [{"id": i, "name": str(i)} for i in ids if i != 7]  # 7 号已下架
        return (
            200,
            {},
            json.dumps({"code": 200, "songs": songs[::-1], "privileges": [{"id": i} for i in ids]}),
        )

    responses.add_callback(responses.POST, API_BASE + "/v3/song/detail", callback=detail)
    ids = list(range(1200, 0, -1))
    songs = NeteaseClient({}).song_details(ids)
    assert len(responses.calls) == 3
    assert [s["id"] for s in songs] == [i for i in ids if i != 7]
    assert songs[0]["privilege"] == {"id": 1200}


@responses.activate
def test_playlist_requests_all_track_ids():
    responses.post(
        WEB_BASE + "/api/v6/playlist/detail",
        json={"code": 200, "playlist": {"id": 9, "name": "p", "trackCount": 3,
                                        "trackIds": [{"id": 1}, {"id": 2}, {"id": 3}]}},
    )  # fmt: skip
    responses.post(API_BASE + "/v3/song/detail",
                   json={"code": 200, "songs": [{"id": 2, "name": "b", "ar": [{"name": "x"}], "al": {}}]})  # fmt: skip
    result = NeteaseClient({}).playlist(9, offset=1, limit=1)
    sent = parse_qs(responses.calls[0].request.body)
    assert sent["n"] == ["100000"]
    assert result["total"] == 3 and [t["id"] for t in result["tracks"]] == [2]


@responses.activate
def test_errors_are_raised_as_netease_error():
    responses.post(re.compile(r".*/cloudsearch/pc"), json={"code": 400, "message": "参数错误"})
    with pytest.raises(NeteaseError, match="参数错误"):
        NeteaseClient({}).search("x")
    responses.post(API_BASE + "/song/lyric", status=404)
    with pytest.raises(NeteaseError, match="HTTP 404"):
        NeteaseClient({}).lyric_raw(1)
    with pytest.raises(ValueError):
        NeteaseClient({}).search("x", type=2)


def test_parse_cookie_string_variants():
    assert parse_cookie_string("Cookie: a=1; b=2\nc=3=4;;  ") == {"a": "1", "b": "2", "c": "3=4"}
    assert parse_cookie_string("") == {}


def test_cookie_store_reloads_after_change(tmp_path):
    path = tmp_path / "cookie.txt"
    store = CookieStore(path)
    assert store.get() == {}
    path.write_text("MUSIC_U=a", encoding="utf-8")
    assert store.get() == {"MUSIC_U": "a"}
    store.save({"MUSIC_U": "b", "__csrf": "c"})
    assert store.get() == {"MUSIC_U": "b", "__csrf": "c"}
