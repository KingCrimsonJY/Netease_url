import pytest
import responses

from links import LinkError, Target, parse, resolve_short_link

SONG = Target("song", 1901371647)


@pytest.mark.parametrize(
    "text",
    [
        "1901371647",
        " 1901371647 ",
        "https://music.163.com/song?id=1901371647",
        "https://music.163.com/#/song?id=1901371647&userid=123",
        "https://y.music.163.com/m/song?id=1901371647&uct2=x",
        "https://music.163.com/song/1901371647/",
        "music.163.com/song?id=1901371647",
        "分享陈奕迅的单曲《孤勇者》: https://music.163.com/song?id=1901371647&userid=1 (来自@网易云音乐)",
    ],
)
def test_song_inputs(text):
    assert parse(text) == SONG


def test_other_kinds_and_expect():
    assert parse("https://music.163.com/#/playlist?id=3778678", "playlist") == Target(
        "playlist", 3778678
    )
    assert parse("https://music.163.com/album?id=137142551") == Target("album", 137142551)
    assert parse("3778678", "playlist") == Target("playlist", 3778678)
    with pytest.raises(LinkError, match="歌单链接"):
        parse("https://music.163.com/playlist?id=1", "song")


@pytest.mark.parametrize(
    "text", ["", "abc", "-1", "https://evil.example/song?id=1", "https://music.163.com/song"]
)
def test_invalid_inputs(text):
    with pytest.raises(LinkError):
        parse(text)


@responses.activate
def test_short_link_is_followed_only_within_allowlist():
    responses.get("https://163cn.tv/abc", status=302,
                  headers={"Location": "https://y.music.163.com/m/song?id=1901371647"})  # fmt: skip
    assert parse("分享: https://163cn.tv/abc 来自网易云") == SONG
    assert len(responses.calls) == 1  # 不会去请求 music.163.com 本身


@responses.activate
def test_short_link_to_foreign_host_is_rejected():
    responses.get(
        "https://163cn.tv/evil", status=302, headers={"Location": "http://127.0.0.1:8080/admin"}
    )
    with pytest.raises(LinkError, match="不是网易云"):
        resolve_short_link("https://163cn.tv/evil")
    assert len(responses.calls) == 1  # 没有请求内网地址


def test_non_allowlisted_hosts_are_never_requested():
    calls = []
    assert parse("https://163cn.tv.evil.com/x https://music.163.com/song?id=1",
          resolver=lambda u: calls.append(u) or u) == Target("song", 1)  # fmt: skip
    assert calls == []
