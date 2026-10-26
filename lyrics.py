"""歌词处理：还原 JSON 元数据行、解析作词作曲、合并翻译。"""

from __future__ import annotations

import json
import re

# 行首的一个或多个时间标签，如 [01:02.34][01:30.00]
_LEADING_TAGS = re.compile(r"^((?:\[\d{1,3}:\d{1,2}(?:[.:]\d{1,3})?\])+)(.*)$")
_TAG = re.compile(r"\[(\d{1,3}):(\d{1,2})(?:[.:](\d{1,3}))?\]")
_CREDIT = re.compile(r"^(作词|作曲|词|曲)\s*[:：]\s*(.+?)\s*$")
_EMPTY_TRANSLATIONS = {"", "/", "//"}


def _tag_ms(match: re.Match) -> int:
    minutes, seconds, frac = match.group(1), match.group(2), match.group(3) or "0"
    return int(minutes) * 60000 + int(seconds) * 1000 + int(frac.ljust(3, "0")[:3])


def _format_ms(ms: int) -> str:
    return f"[{ms // 60000:02d}:{ms // 1000 % 60:02d}.{ms % 1000:03d}]"


def _split(line: str) -> tuple[list[re.Match], str]:
    m = _LEADING_TAGS.match(line.strip())
    if not m:
        return [], line.strip()
    return list(_TAG.finditer(m.group(1))), m.group(2).strip()


def normalize(lrc: str) -> str:
    """把新版接口的 JSON 元数据行（{"t":0,"c":[{"tx":"作词: "},...]}）还原成普通 LRC 行。"""
    out = []
    for line in (lrc or "").splitlines():
        s = line.strip()
        if s.startswith("{") and s.endswith("}"):
            try:
                obj = json.loads(s)
                text = "".join(part.get("tx", "") for part in obj.get("c") or [])
                out.append(f"{_format_ms(int(obj.get('t') or 0))}{text}")
            except (ValueError, TypeError, AttributeError):
                continue
        else:
            out.append(line.rstrip())
    return "\n".join(out).strip()


def credits(lrc: str) -> dict[str, str]:
    """从歌词开头的制作信息里取出作词、作曲。"""
    result: dict[str, str] = {}
    for line in (lrc or "").splitlines():
        _, text = _split(line)
        m = _CREDIT.match(text)
        if not m:
            continue
        key = "lyricist" if m.group(1) in ("作词", "词") else "composer"
        result.setdefault(key, m.group(2))
    return result


def merge_translation(lrc: str, tlyric: str) -> str:
    """在每行原文下面插入相同时间戳的译文（多数播放器会把两行一起显示）。"""
    translations: dict[int, str] = {}
    for line in (tlyric or "").splitlines():
        tags, text = _split(line)
        if text in _EMPTY_TRANSLATIONS:
            continue
        for tag in tags:
            translations[_tag_ms(tag)] = text
    out = []
    for line in (lrc or "").splitlines():
        out.append(line)
        tags, text = _split(line)
        if not text:
            continue
        for tag in tags:
            translated = translations.get(_tag_ms(tag))
            if translated and translated != text:
                out.append(f"{tag.group(0)}{translated}")
    return "\n".join(out)


def build(lrc: str, tlyric: str = "", *, translation: str = "merge") -> str:
    text = normalize(lrc)
    if translation == "merge" and tlyric:
        text = merge_translation(text, normalize(tlyric))
    return text
