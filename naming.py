"""文件名：模板渲染 + 跨平台安全处理。

处理顺序：Unicode NFC → 非法字符换成全角（或 _）→ 去控制字符、合并空白 →
按 UTF-8 字节数控制长度（优先减少艺人数量）→ 对"主名+扩展名"整体做 pathvalidate 处理。
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping, Sequence
from typing import Any

from pathvalidate import sanitize_filename

from levels import level_name

FULLWIDTH = str.maketrans(
    {
        "/": "／",
        "\\": "＼",
        ":": "：",
        "*": "＊",
        "?": "？",
        '"': "＂",
        "<": "＜",
        ">": "＞",
        "|": "｜",
    }
)
_ILLEGAL = re.compile(r'[/\\:*?"<>|]')
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_SPACES = re.compile(r"\s+")

MAX_STEM_BYTES = 180  # ext4 单个文件名上限 255 字节，一个汉字占 3 字节


def clean(text: str, *, fullwidth: bool = True) -> str:
    text = unicodedata.normalize("NFC", str(text or ""))
    text = text.translate(FULLWIDTH) if fullwidth else _ILLEGAL.sub("_", text)
    text = _SPACES.sub(" ", text)
    return _CONTROL.sub("", text).strip()


def join_artists(artists: Sequence[str], separator: str = ", ", max_count: int = 3) -> str:
    names = [a for a in artists if a]
    if not names:
        return "未知歌手"
    if len(names) > max_count:
        return separator.join(names[:max_count]) + " 等"
    return separator.join(names)


def truncate_utf8(text: str, max_bytes: int) -> str:
    data = text.encode("utf-8")
    if len(data) <= max_bytes:
        return text
    return data[:max_bytes].decode("utf-8", errors="ignore").rstrip()


def template_fields(info: Mapping[str, Any], artists: str) -> dict[str, Any]:
    names = list(info.get("artists") or [])
    level = info.get("level") or ""
    return {
        "artists": artists,
        "artist": names[0] if names else "未知歌手",
        "title": info.get("title") or "未知歌曲",
        "album": info.get("album") or "",
        "track": int(info.get("track") or 0),
        "disc": int(info.get("disc") or 1),
        "year": info.get("year") or "",
        "level": level,
        "quality_name": level_name(level) if level else "",
        "id": info.get("id") or "",
    }


def build_filename(
    info: Mapping[str, Any],
    ext: str,
    *,
    template: str = "{artists} - {title}",
    separator: str = ", ",
    max_artists: int = 3,
    fullwidth: bool = True,
    max_bytes: int = MAX_STEM_BYTES,
) -> str:
    """info 至少包含 id、title、artists(list)；可选 album、track、disc、year、level。"""
    artists = list(info.get("artists") or [])

    def render(fields: dict[str, Any]) -> str:
        fields = {
            k: clean(v, fullwidth=fullwidth) if isinstance(v, str) else v for k, v in fields.items()
        }
        return clean(template.format_map(fields), fullwidth=fullwidth)

    def fits(text: str) -> bool:
        return len(text.encode("utf-8")) <= max_bytes

    # 超长时依次：减少艺人数量 → 艺人部分最多占三分之一 → 整体截断
    for count in range(max(1, min(max_artists, len(artists) or 1)), 0, -1):
        fields = template_fields(info, join_artists(artists, separator, count))
        stem = render(fields)
        if fits(stem):
            break
    if not fits(stem):
        for key in ("artists", "artist"):
            fields[key] = truncate_utf8(fields[key], max_bytes // 3)
        stem = render(fields)
    stem = truncate_utf8(stem, max_bytes)
    name = sanitize_filename(stem + ext, platform="universal", max_len=255)
    if not name or name == ext or not name.removesuffix(ext).strip(" ."):
        name = f"{info.get('id') or 'unknown'}{ext}"
    return name
