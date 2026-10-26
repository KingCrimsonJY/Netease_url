"""解析各种输入：纯 ID、网易云链接（多种格式）、分享文本、163cn.tv 短链接。

短链接只会请求白名单内的域名，并且手动跟随跳转、最终必须落在 music.163.com，避免 SSRF。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qs, urljoin, urlsplit

import requests

KIND_NAMES = {"song": "歌曲", "playlist": "歌单", "album": "专辑", "artist": "歌手"}
SHORT_HOSTS = frozenset({"163cn.tv", "163cn.link"})
_URL = re.compile(
    r"(?:https?://)?(?:[\w-]+\.)*(?:music\.163\.com|163cn\.tv|163cn\.link)"
    r"[^\s，。！？、；“”‘’\"'<>()（）]*",
    re.IGNORECASE,
)
_ID = re.compile(r"\d{1,20}")


class LinkError(ValueError):
    """输入无法识别为网易云的 ID 或链接。"""


@dataclass(frozen=True)
class Target:
    kind: str
    id: int


def is_netease_host(host: str | None) -> bool:
    host = (host or "").lower()
    return host == "music.163.com" or host.endswith(".music.163.com")


def from_netease_url(url: str) -> Target | None:
    """支持 ?id=、#/x?id=、/m/x?id=、/x/123 等格式。"""
    parts = urlsplit(url)
    if not is_netease_host(parts.hostname):
        return None
    path, query = parts.path, parts.query
    if parts.fragment.startswith("/"):
        frag = urlsplit(parts.fragment)
        path, query = frag.path, frag.query
    segments = [s for s in path.split("/") if s]
    kind = song_id = None
    for i, seg in enumerate(segments):
        if seg in KIND_NAMES:
            kind = seg
            if i + 1 < len(segments) and _ID.fullmatch(segments[i + 1]):
                song_id = segments[i + 1]
    query_id = (parse_qs(query).get("id") or [None])[0]
    if query_id and _ID.fullmatch(query_id):
        song_id = query_id
    if kind and song_id:
        return Target(kind, int(song_id))
    return None


def resolve_short_link(
    url: str, *, session: requests.Session | None = None, max_hops: int = 3, timeout: float = 5
) -> str:
    current = url
    http = session or requests
    for _ in range(max_hops):
        parts = urlsplit(current)
        if (
            parts.scheme not in ("http", "https")
            or (parts.hostname or "").lower() not in SHORT_HOSTS
        ):
            break
        try:
            resp = http.get(current, allow_redirects=False, timeout=timeout)
        except requests.RequestException as e:
            raise LinkError("短链接无法访问") from e
        location = resp.headers.get("Location")
        if resp.status_code not in (301, 302, 303, 307, 308) or not location:
            raise LinkError("短链接无法解析")
        current = urljoin(current, location)
    if not is_netease_host(urlsplit(current).hostname):
        raise LinkError("短链接指向的不是网易云音乐")
    return current


def parse(text: object, expect: str | None = None, *, resolver=resolve_short_link) -> Target:
    """把用户输入解析成 (类型, ID)。纯数字按 expect（默认歌曲）处理。"""
    raw = str(text or "").strip()
    if not raw:
        raise LinkError("请输入 ID 或链接")
    if _ID.fullmatch(raw):
        return Target(expect or "song", int(raw))
    for match in _URL.finditer(raw):
        url = match.group(0)
        if not url.lower().startswith(("http://", "https://")):
            url = "https://" + url
        if (urlsplit(url).hostname or "").lower() in SHORT_HOSTS:
            url = resolver(url)
        target = from_netease_url(url)
        if target is None:
            continue
        if expect and target.kind != expect:
            raise LinkError(f"这是{KIND_NAMES[target.kind]}链接，这里需要{KIND_NAMES[expect]}")
        return target
    raise LinkError(f"无法识别的 ID 或链接: {raw[:60]}")
