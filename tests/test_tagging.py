import shutil
from pathlib import Path

import pytest
from mutagen.flac import FLAC
from mutagen.id3 import ID3, TIT2
from mutagen.mp4 import MP4

from tagging import (
    TaggingError,
    TrackTags,
    build_tags,
    cover_url,
    detect_image_mime,
    ms_to_date,
    read_song_id,
    sniff_format,
    write_tags,
)

FIXTURES = Path(__file__).parent / "fixtures"
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 64

SONG = {
    "id": 1931534562,
    "name": "UNDERSTAND",
    "ar": [{"name": "keshi"}, {"name": "Guest"}],
    "al": {"name": "GABRIEL", "picUrl": "http://p1.music.126.net/x/1.jpg"},
    "no": 9,
    "cd": "01",
    "publishTime": 0,
}
ALBUM = {"name": "GABRIEL", "artists": ["keshi"], "track_total": 12, "disc_total": 1,
         "publish_time": 1648137600000, "company": "Island Records"}  # fmt: skip
LRC = "[00:00.000] 作词 : Elie Rizk\n[00:00.500] 作曲 : Casey Luong\n[00:10.00]line"


@pytest.fixture
def tags() -> TrackTags:
    return build_tags(SONG, ALBUM, LRC, (JPEG, "image/jpeg"))


def copy_fixture(tmp_path: Path, name: str) -> Path:
    dst = tmp_path / name
    shutil.copy(FIXTURES / name, dst)
    return dst


def test_build_tags(tags):
    assert tags.artists == ["keshi", "Guest"]
    assert tags.album_artists == ["keshi"]  # 专辑艺人来自专辑详情，而不是这首歌的全部艺人
    assert (tags.track, tags.track_total, tags.disc, tags.disc_total) == (9, 12, 1, 1)
    assert tags.date == "2022-03-25" and tags.year == "2022"
    assert (tags.lyricist, tags.composer) == ("Elie Rizk", "Casey Luong")


def test_ms_to_date_handles_unknown_and_pre_1970():
    assert ms_to_date(0) == ""
    assert ms_to_date(-220924800000) == "1963-01-01"


def test_flac(tmp_path, tags):
    path = copy_fixture(tmp_path, "silence.flac")
    write_tags(path, tags)
    audio = FLAC(path)
    assert audio["ARTIST"] == ["keshi", "Guest"]
    assert audio["TRACKNUMBER"] == ["9"] and audio["TRACKTOTAL"] == ["12"]
    assert audio["DATE"] == ["2022-03-25"] and audio["LABEL"] == ["Island Records"]
    assert audio["LYRICS"] == [LRC] and audio["COMPOSER"] == ["Casey Luong"]
    assert "ENCODER" not in audio  # 原有标签被清除
    [pic] = audio.pictures
    assert pic.type == 3 and pic.mime == "image/jpeg" and pic.data == JPEG
    assert read_song_id(path) == 1931534562


def test_mp3_id3v23(tmp_path, tags):
    path = copy_fixture(tmp_path, "silence.mp3")
    old = ID3()
    old.add(TIT2(encoding=3, text="旧标题"))
    old.save(path)
    write_tags(path, tags, id3_version=3)
    id3 = ID3(path)
    assert id3.version[:2] == (2, 3)
    assert str(id3["TIT2"]) == "UNDERSTAND" and len(id3.getall("TIT2")) == 1
    assert id3["TPE1"].text == ["keshi/Guest"]  # v2.3 多值以 / 连接
    assert str(id3["TRCK"]) == "9/12" and str(id3["TPE2"]) == "keshi"
    assert str(id3["TPUB"]) == "Island Records" and str(id3["TEXT"]) == "Elie Rizk"
    apic = id3.getall("APIC")[0]
    assert apic.type == 3 and apic.data == JPEG
    assert id3.getall("USLT")[0].text == LRC
    assert read_song_id(path) == 1931534562


def test_mp4(tmp_path, tags):
    path = copy_fixture(tmp_path, "silence.m4a")
    write_tags(path, tags)
    t = MP4(path).tags
    assert t["\xa9nam"] == ["UNDERSTAND"] and t["\xa9ART"] == ["keshi/Guest"]
    assert t["trkn"] == [(9, 12)] and t["disk"] == [(1, 1)]
    assert t["\xa9day"] == ["2022-03-25"] and t["\xa9lyr"] == [LRC]
    assert bytes(t["covr"][0]) == JPEG
    assert read_song_id(path) == 1931534562


def test_lyrics_can_be_left_out(tmp_path, tags):
    path = copy_fixture(tmp_path, "silence.flac")
    write_tags(path, tags, embed_lyrics=False)
    assert "LYRICS" not in FLAC(path)


def test_unknown_format(tmp_path, tags):
    path = tmp_path / "x.bin"
    path.write_bytes(b"not audio at all")
    assert sniff_format(path) is None
    with pytest.raises(TaggingError):
        write_tags(path, tags)


def test_sniff_formats():
    assert sniff_format(FIXTURES / "silence.flac") == "flac"
    assert sniff_format(FIXTURES / "silence.mp3") == "mp3"
    assert sniff_format(FIXTURES / "silence.m4a") == "mp4"


def test_cover_helpers():
    assert cover_url("http://p1.music.126.net/a/1.jpg?param=300y300", 1200) == (
        "https://p1.music.126.net/a/1.jpg?imageView&thumbnail=1200y1200&type=jpg&quality=90"
    )
    assert cover_url("http://p1.music.126.net/a/1.jpg", 0) == "https://p1.music.126.net/a/1.jpg"
    assert detect_image_mime(JPEG) == "image/jpeg"
    assert detect_image_mime(b"\x89PNG\r\n\x1a\nxxxx") == "image/png"
    assert detect_image_mime(b"<html>") is None


def test_mp3_id3v24_keeps_slash_in_artist_names(tmp_path):
    song = {**SONG, "ar": [{"name": "AC/DC"}, {"name": "Guest"}]}
    path = copy_fixture(tmp_path, "silence.mp3")
    write_tags(path, build_tags(song, ALBUM, LRC, None), id3_version=4)
    id3 = ID3(path)
    assert id3.version[:2] == (2, 4)
    assert id3["TPE1"].text == ["AC/DC", "Guest"]
