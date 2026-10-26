"""用 mutagen 写入标签、封面和歌词（就地修改标签区，不重新封装音频）。

字段映射参考 MusicBrainz Picard 的标签对照表：
FLAC 用 Vorbis 注释（多值字段），MP3 用 ID3v2.3（多值以 / 分隔），M4A 用 iTunes atom。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from mutagen.flac import FLAC, Picture
from mutagen.id3 import (
    APIC,
    ID3,
    TALB,
    TCOM,
    TDRC,
    TEXT,
    TIT2,
    TPE1,
    TPE2,
    TPOS,
    TPUB,
    TRCK,
    TXXX,
    USLT,
    Encoding,
    PictureType,
)
from mutagen.mp4 import MP4, MP4Cover, MP4FreeForm

import lyrics as lyrics_mod

# 标签方案的版本号；修改写入规则后递增，已缓存的文件会在下次使用时重写标签
TAG_VERSION = 2  # 2: MP3 默认改为 ID3v2.4
SONG_ID_KEY = "NETEASE_SONG_ID"
_CST = timezone(timedelta(hours=8))
_CJK = re.compile(r"[぀-ヿ㐀-鿿가-힯]")


class TaggingError(Exception):
    """无法识别文件格式或写入失败。"""


@dataclass
class TrackTags:
    song_id: int
    title: str
    artists: list[str]
    album: str = ""
    album_artists: list[str] = field(default_factory=list)
    track: int = 0
    track_total: int = 0
    disc: int = 0
    disc_total: int = 0
    date: str = ""
    composer: str = ""
    lyricist: str = ""
    label: str = ""
    lyrics: str = ""
    cover: bytes | None = None
    cover_mime: str = ""

    @property
    def year(self) -> str:
        return self.date[:4]

    def snapshot(self) -> dict[str, Any]:
        """命名和展示需要的信息（存进缓存元数据，不含封面和歌词）。"""
        return {
            "id": self.song_id,
            "title": self.title,
            "artists": self.artists,
            "album": self.album,
            "album_artists": self.album_artists,
            "track": self.track,
            "disc": self.disc,
            "year": self.year,
            "date": self.date,
        }


def ms_to_date(ms: int | None) -> str:
    """网易云的发行时间（毫秒，北京时间 0 点）转为 YYYY-MM-DD；0 表示未知。"""
    if not ms:
        return ""
    moment = datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=int(ms))
    return moment.astimezone(_CST).strftime("%Y-%m-%d")


def _int(value: Any) -> int:
    m = re.search(r"\d+", str(value or ""))
    return int(m.group()) if m else 0


def build_tags(
    song: dict[str, Any],
    album: dict[str, Any] | None = None,
    lyric_text: str = "",
    cover: tuple[bytes, str] | None = None,
) -> TrackTags:
    """song 为歌曲详情；album 为 NeteaseClient.album_tag_info() 的结果。"""
    album = album or {}
    al = song.get("al") or {}
    artists = [a.get("name") for a in song.get("ar") or [] if a.get("name")]
    credit = lyrics_mod.credits(lyric_text)
    return TrackTags(
        song_id=int(song["id"]),
        title=song.get("name") or "",
        artists=artists,
        album=album.get("name") or al.get("name") or "",
        album_artists=album.get("artists") or artists[:1],
        track=_int(song.get("no")),
        track_total=int(album.get("track_total") or 0),
        disc=_int(song.get("cd")) or 1,
        disc_total=int(album.get("disc_total") or 0),
        date=ms_to_date(album.get("publish_time") or song.get("publishTime")),
        composer=credit.get("composer", ""),
        lyricist=credit.get("lyricist", ""),
        label=album.get("company") or "",
        lyrics=lyric_text,
        cover=cover[0] if cover else None,
        cover_mime=cover[1] if cover else "",
    )


# ============ 封面 ============


def detect_image_mime(data: bytes) -> str | None:
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    return None


def cover_url(pic_url: str, size: int) -> str:
    """size>0 时让图片服务缩放并转为 JPEG（?param= 只缩放不转格式，原图常是几 MB 的 PNG）。"""
    base = pic_url.split("?", 1)[0].replace("http://", "https://", 1)
    if size <= 0:
        return base
    return f"{base}?imageView&thumbnail={size}y{size}&type=jpg&quality=90"


def fetch_cover(
    pic_url: str, size: int, session: requests.Session | None = None, timeout: float = 15
) -> tuple[bytes, str] | None:
    if not pic_url:
        return None
    http = session or requests
    for url in dict.fromkeys((cover_url(pic_url, size), cover_url(pic_url, 0))):
        try:
            resp = http.get(url, timeout=timeout)
            resp.raise_for_status()
        except requests.RequestException:
            continue
        mime = detect_image_mime(resp.content)
        if mime:
            return resp.content, mime
    return None


# ============ 格式识别 ============


def sniff_format(path: Path) -> str | None:
    """按文件头识别容器：flac / mp3 / mp4 / ogg。"""
    with open(path, "rb") as f:
        head = f.read(12)
        if head[:3] == b"ID3" and len(head) >= 10:
            # 跳过 ID3v2 头（有的 FLAC 前面也带 ID3）
            size = (head[6] << 21) | (head[7] << 14) | (head[8] << 7) | head[9]
            f.seek(10 + size)
            after = f.read(4)
            return "flac" if after == b"fLaC" else "mp3"
    if head[:4] == b"fLaC":
        return "flac"
    if head[4:8] == b"ftyp":
        return "mp4"
    if head[:4] == b"OggS":
        return "ogg"
    if len(head) >= 2 and head[0] == 0xFF and head[1] & 0xE0 == 0xE0:
        return "mp3"
    return None


EXT_BY_FORMAT = {"flac": ".flac", "mp3": ".mp3", "mp4": ".m4a", "ogg": ".ogg"}


# ============ 写入 ============


def write_tags(
    path: Path, tags: TrackTags, *, id3_version: int = 3, embed_lyrics: bool = True
) -> None:
    fmt = sniff_format(path)
    writer = {"flac": _write_flac, "mp3": _write_id3, "mp4": _write_mp4}.get(fmt or "")
    if writer is None:
        raise TaggingError(f"不支持写入标签的格式: {fmt or '未知'}")
    lyric_text = tags.lyrics if embed_lyrics else ""
    try:
        if writer is _write_id3:
            _write_id3(path, tags, lyric_text, id3_version)
        else:
            writer(path, tags, lyric_text)
    except TaggingError:
        raise
    except Exception as e:
        raise TaggingError(f"写入 {fmt} 标签失败: {e}") from e


def _numbering(n: int, total: int) -> str:
    if not n:
        return ""
    return f"{n}/{total}" if total else str(n)


def _write_flac(path: Path, tags: TrackTags, lyric_text: str) -> None:
    audio = FLAC(path)
    audio.clear_pictures()
    if audio.tags is None:
        audio.add_tags()
    else:
        audio.tags.clear()
    fields: dict[str, Any] = {
        "TITLE": tags.title,
        "ARTIST": tags.artists,
        "ALBUM": tags.album,
        "ALBUMARTIST": tags.album_artists,
        "TRACKNUMBER": str(tags.track) if tags.track else "",
        "TRACKTOTAL": str(tags.track_total) if tags.track_total else "",
        "TOTALTRACKS": str(tags.track_total) if tags.track_total else "",
        "DISCNUMBER": str(tags.disc) if tags.disc else "",
        "DISCTOTAL": str(tags.disc_total) if tags.disc_total else "",
        "TOTALDISCS": str(tags.disc_total) if tags.disc_total else "",
        "DATE": tags.date,
        "COMPOSER": tags.composer,
        "LYRICIST": tags.lyricist,
        "LABEL": tags.label,
        "LYRICS": lyric_text,
        SONG_ID_KEY: str(tags.song_id),
    }
    for key, value in fields.items():
        if value:
            audio[key] = value
    if tags.cover:
        pic = Picture()
        pic.type = PictureType.COVER_FRONT
        pic.mime = tags.cover_mime or "image/jpeg"
        pic.desc = "Cover"
        pic.data = tags.cover
        audio.add_picture(pic)
    audio.save(deleteid3=True)


def _write_id3(path: Path, tags: TrackTags, lyric_text: str, version: int) -> None:
    enc = Encoding.UTF8  # 保存为 v2.3 时 mutagen 会自动转成 UTF-16
    id3 = ID3()
    id3.add(TIT2(encoding=enc, text=tags.title))
    if tags.artists:
        id3.add(TPE1(encoding=enc, text=tags.artists))
    if tags.album:
        id3.add(TALB(encoding=enc, text=tags.album))
    if tags.album_artists:
        id3.add(TPE2(encoding=enc, text=tags.album_artists))
    if tags.track:
        id3.add(TRCK(encoding=enc, text=_numbering(tags.track, tags.track_total)))
    if tags.disc:
        id3.add(TPOS(encoding=enc, text=_numbering(tags.disc, tags.disc_total)))
    if tags.date:
        id3.add(TDRC(encoding=enc, text=tags.date))
    if tags.composer:
        id3.add(TCOM(encoding=enc, text=tags.composer))
    if tags.lyricist:
        id3.add(TEXT(encoding=enc, text=tags.lyricist))
    if tags.label:
        id3.add(TPUB(encoding=enc, text=tags.label))
    if lyric_text:
        lang = "chi" if _CJK.search(lyric_text) else "eng"
        id3.add(USLT(encoding=enc, lang=lang, desc="", text=lyric_text))
    if tags.cover:
        id3.add(APIC(encoding=enc, mime=tags.cover_mime or "image/jpeg",
                     type=PictureType.COVER_FRONT, desc="Cover", data=tags.cover))  # fmt: skip
    id3.add(TXXX(encoding=enc, desc=SONG_ID_KEY, text=str(tags.song_id)))
    # 替换原有 ID3v2，并删除 ID3v1
    id3.save(path, v1=0, v2_version=version, v23_sep="/")


def _freeform(value: str) -> list[MP4FreeForm]:
    return [MP4FreeForm(value.encode("utf-8"))]


def _write_mp4(path: Path, tags: TrackTags, lyric_text: str) -> None:
    audio = MP4(path)
    if audio.tags is None:
        audio.add_tags()
    else:
        audio.tags.clear()
    t = audio.tags
    t["\xa9nam"] = [tags.title]
    if tags.artists:
        t["\xa9ART"] = ["/".join(tags.artists)]  # 多值 ©ART 很多播放器只显示第一个
    if tags.album:
        t["\xa9alb"] = [tags.album]
    if tags.album_artists:
        t["aART"] = ["/".join(tags.album_artists)]
    if tags.track:
        t["trkn"] = [(tags.track, tags.track_total)]
    if tags.disc:
        t["disk"] = [(tags.disc, tags.disc_total)]
    if tags.date:
        t["\xa9day"] = [tags.date]
    if tags.composer:
        t["\xa9wrt"] = [tags.composer]
    if lyric_text:
        t["\xa9lyr"] = [lyric_text]
    if tags.cover:
        fmt = MP4Cover.FORMAT_PNG if tags.cover_mime == "image/png" else MP4Cover.FORMAT_JPEG
        t["covr"] = [MP4Cover(tags.cover, imageformat=fmt)]
    if tags.lyricist:
        t["----:com.apple.iTunes:LYRICIST"] = _freeform(tags.lyricist)
    if tags.label:
        t["----:com.apple.iTunes:LABEL"] = _freeform(tags.label)
    t[f"----:com.apple.iTunes:{SONG_ID_KEY}"] = _freeform(str(tags.song_id))
    audio.save()


def read_song_id(path: Path) -> int | None:
    """读出文件里记录的网易云歌曲 ID（用于判断同名文件是否同一首歌）。"""
    try:
        fmt = sniff_format(path)
        if fmt == "flac":
            values = (FLAC(path).tags or {}).get(SONG_ID_KEY) or []
        elif fmt == "mp3":
            frame = ID3(path).get(f"TXXX:{SONG_ID_KEY}")
            values = frame.text if frame else []
        elif fmt == "mp4":
            values = [
                bytes(v).decode()
                for v in (MP4(path).tags or {}).get(f"----:com.apple.iTunes:{SONG_ID_KEY}", [])
            ]
        else:
            return None
        return int(values[0]) if values else None
    except Exception:
        return None
