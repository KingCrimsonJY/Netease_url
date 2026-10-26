"""网易云音乐接口客户端。

所有请求都经过 NeteaseClient：
- cookie = 设备档案默认值（os=pc、appver 等）+ 用户 cookie（用户的值优先）。
  appver 只有放在 cookie 里才会被服务端采用；缺少它时高于无损的音质会被静默降级。
- eapi 接口自动加密，payload 里的 header 字段由合并后的 cookie 生成。
- 网络错误、429 和 5xx 自动重试。
"""

from __future__ import annotations

import json
import random
import re
import threading
import time
import urllib.parse
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from hashlib import md5
from http.cookiejar import DefaultCookiePolicy
from pathlib import Path
from typing import Any

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from levels import get_level, level_name
from utils import TTLCache, atomic_write_text

EAPI_BASE = "https://interface3.music.163.com/eapi"
API_BASE = "https://interface3.music.163.com/api"
WEB_BASE = "https://music.163.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Safari/537.36 Chrome/91.0.4472.164 NeteaseMusicDesktop/2.10.2.200154"
)
REFERER = "https://music.163.com/"
EAPI_KEY = b"e82ckenh8dichen8"

# 与 NeteaseCloudMusicApi Enhanced 的设备档案保持一致
DEVICE_PROFILES: dict[str, dict[str, str]] = {
    "pc": {
        "os": "pc",
        "appver": "3.1.17.204416",
        "osver": "Microsoft-Windows-10-Professional-build-19045-64bit",
        "channel": "netease",
    },
    "android": {
        "os": "android",
        "appver": "8.20.20.231215173437",
        "osver": "14",
        "channel": "xiaomi",
    },
}

# url 接口返回的 type -> 文件扩展名（MP4 容器的纯音频按惯例用 .m4a）
EXT_BY_TYPE = {
    "mp3": ".mp3",
    "flac": ".flac",
    "mp4": ".m4a",
    "m4a": ".m4a",
    "av3a": ".m4a",
    "eac3": ".m4a",
    "ec3": ".m4a",
    "ogg": ".ogg",
    "opus": ".opus",
}

URL_ERRORS = {
    "TRIAL_ONLY": "只能获取试听片段（需要会员或购买）",
    "NEED_VIP_OR_PURCHASE": "无权获取：需要会员或购买，或该歌曲不提供此音质",
    "UNAVAILABLE": "歌曲已下架或暂无版权",
    "NO_URL": "未获取到播放链接",
}


class NeteaseError(Exception):
    """网易云接口调用失败（网络错误、HTTP 错误或业务 code 非 200）。"""

    def __init__(self, message: str, code: int | None = None):
        super().__init__(message)
        self.message = message
        self.code = code


APIException = NeteaseError  # 兼容旧名称


# ============ eapi 加密 ============


def eapi_encrypt(url: str, payload: Mapping[str, Any]) -> str:
    """eapi 参数加密：AES-128-ECB(path-36cd479b6b5-json-36cd479b6b5-md5)。"""
    path = urllib.parse.urlparse(url).path.replace("/eapi/", "/api/")
    text = json.dumps(payload)
    digest = md5(f"nobody{path}use{text}md5forencrypt".encode()).hexdigest()
    plain = f"{path}-36cd479b6b5-{text}-36cd479b6b5-{digest}".encode()
    padder = padding.PKCS7(128).padder()
    padded = padder.update(plain) + padder.finalize()
    encryptor = Cipher(algorithms.AES(EAPI_KEY), modes.ECB()).encryptor()
    return (encryptor.update(padded) + encryptor.finalize()).hex()


# ============ Cookie ============


def parse_cookie_string(cookie_string: str | None) -> dict[str, str]:
    """把 ``k1=v1; k2=v2``（可跨行，可带 ``Cookie:`` 前缀）解析为 dict。"""
    cookies: dict[str, str] = {}
    if not cookie_string:
        return cookies
    text = cookie_string.strip()
    if text.lower().startswith("cookie:"):
        text = text[7:]
    for pair in re.split(r"[;\r\n]", text):
        if "=" not in pair:
            continue
        key, value = pair.split("=", 1)
        key, value = key.strip(), value.strip()
        if key and value:
            cookies[key] = value
    return cookies


def load_cookies(path: str | Path = "cookie.txt") -> dict[str, str]:
    """读取 cookie 文件；文件不存在或读取失败时返回空 dict。"""
    try:
        return parse_cookie_string(Path(path).read_text(encoding="utf-8-sig"))
    except OSError:
        return {}


class CookieStore:
    """cookie 文件的读写。按 mtime 缓存，修改文件后无需重启即可生效。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = threading.Lock()
        self._stamp: tuple[int, int] | None = None
        self._cookies: dict[str, str] = {}

    def get(self) -> dict[str, str]:
        try:
            st = self.path.stat()
        except OSError:
            return {}
        stamp = (st.st_mtime_ns, st.st_size)
        with self._lock:
            if stamp != self._stamp:
                self._cookies = load_cookies(self.path)
                self._stamp = stamp
            return dict(self._cookies)

    def save(self, cookies: Mapping[str, str]) -> None:
        atomic_write_text(self.path, "; ".join(f"{k}={v}" for k, v in cookies.items()) + "\n")

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


# ============ 数据结构 ============


@dataclass
class TrackSource:
    """一次 url 请求的结果：请求的音质、实际得到的音质和文件信息。"""

    song_id: int
    requested_level: str
    level: str | None
    url: str | None
    type: str | None
    ext: str | None
    size: int
    md5: str | None
    bitrate: int
    sample_rate: int
    duration_ms: int
    expires_at: float
    is_trial: bool
    code: int

    @classmethod
    def from_api(cls, item: Mapping[str, Any], requested_level: str) -> TrackSource:
        type_ = (item.get("type") or "").lower() or None
        return cls(
            song_id=int(item.get("id") or 0),
            requested_level=requested_level,
            level=item.get("level") or None,
            url=item.get("url") or None,
            type=type_,
            ext=EXT_BY_TYPE.get(type_ or ""),
            size=int(item.get("size") or 0),
            md5=(item.get("md5") or "").lower() or None,
            bitrate=int(item.get("br") or 0),
            sample_rate=int(item.get("sr") or 0),
            duration_ms=int(item.get("time") or 0),
            expires_at=time.time() + int(item.get("expi") or 1200),
            is_trial=bool(item.get("freeTrialInfo")),
            code=int(item.get("code") or 0),
        )

    @property
    def available(self) -> bool:
        return bool(self.url) and not self.is_trial

    @property
    def downgraded(self) -> bool:
        return self.available and self.level != self.requested_level

    @property
    def error_code(self) -> str | None:
        if self.available:
            return None
        if self.is_trial:
            return "TRIAL_ONLY"
        if self.code == -110:
            return "NEED_VIP_OR_PURCHASE"
        if self.code == 404:
            return "UNAVAILABLE"
        return "NO_URL"

    @property
    def error_message(self) -> str | None:
        code = self.error_code
        return URL_ERRORS[code] if code else None

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d.update(
            available=self.available,
            downgraded=self.downgraded,
            quality_name=level_name(self.level),
            requested_quality_name=level_name(self.requested_level),
            error_code=self.error_code,
            error_message=self.error_message,
            expires_in=max(0, int(self.expires_at - time.time())),
        )
        d.pop("expires_at")
        return d


@dataclass
class Lyrics:
    lrc: str = ""
    tlyric: str = ""
    romalrc: str = ""
    no_lyrics: bool = False


def _artist_names(items: list[Mapping[str, Any]] | None) -> list[str]:
    return [a.get("name") or "" for a in items or [] if a.get("name")]


def song_brief(song: Mapping[str, Any]) -> dict[str, Any]:
    """列表展示用的歌曲摘要（字段名与旧版前端兼容）。"""
    al = song.get("al") or song.get("album") or {}
    artists = _artist_names(song.get("ar") or song.get("artists"))
    return {
        "id": song.get("id"),
        "name": song.get("name") or "",
        "artists": "/".join(artists),
        "artist_list": artists,
        "album": al.get("name") or "",
        "album_id": al.get("id"),
        "picUrl": al.get("picUrl") or "",
        "duration": song.get("dt") or song.get("duration") or 0,
    }


def _as_int(value: Any, default: int = 0) -> int:
    m = re.search(r"\d+", str(value or ""))
    return int(m.group()) if m else default


# ============ 客户端 ============


class NeteaseClient:
    def __init__(
        self,
        cookies: CookieStore | Mapping[str, str] | None = None,
        *,
        os_name: str = "pc",
        appver: str = "",
        device_id: str = "pyncm!",
        timeout: tuple[float, float] = (5, 30),
    ):
        if isinstance(cookies, CookieStore):
            self._cookie_source = cookies.get
        else:
            fixed = dict(cookies or {})
            self._cookie_source = lambda: dict(fixed)
        profile = dict(DEVICE_PROFILES.get(os_name, DEVICE_PROFILES["pc"]))
        if appver:
            profile["appver"] = appver
        profile["deviceId"] = device_id
        self.device = profile
        self.timeout = timeout
        self.session = requests.Session()
        # cookie 由我们显式拼接；不让响应的 Set-Cookie 污染共享的 session
        self.session.cookies.set_policy(DefaultCookiePolicy(allowed_domains=[]))
        retry = Retry(
            total=3,
            backoff_factor=0.5,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=None,
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=32)
        self.session.mount("https://", adapter)
        self.session.mount("http://", adapter)
        self._album_cache = TTLCache(ttl=600, maxsize=256)
        self._playlist_cache = TTLCache(ttl=300, maxsize=32)

    @classmethod
    def from_settings(cls, settings) -> NeteaseClient:
        return cls(
            CookieStore(settings.cookie_file),
            os_name=settings.netease_os,
            appver=settings.netease_appver,
            device_id=settings.netease_device_id,
        )

    # ----------- 底层请求 -----------

    def user_cookies(self) -> dict[str, str]:
        return self._cookie_source()

    def cookies(self, force: Mapping[str, str] | None = None) -> dict[str, str]:
        """设备默认值 < 用户 cookie < 强制覆盖（如 vivid 的 Android 身份）。"""
        merged = {**self.device, **self.user_cookies()}
        if force:
            merged.update(force)
        return merged

    def _send(
        self, method: str, url: str, cookies: Mapping[str, str], **kwargs
    ) -> requests.Response:
        headers = {
            "User-Agent": USER_AGENT,
            "Referer": REFERER,
            "Cookie": "; ".join(f"{k}={v}" for k, v in cookies.items()),
        }
        try:
            resp = self.session.request(
                method, url, headers=headers, timeout=self.timeout, **kwargs
            )
        except requests.RequestException as e:
            raise NeteaseError(f"请求网易云接口失败: {e.__class__.__name__}") from e
        if resp.status_code != 200:
            raise NeteaseError(f"网易云接口返回 HTTP {resp.status_code}", resp.status_code)
        return resp

    @staticmethod
    def _json(resp: requests.Response, *, check: bool = True) -> dict[str, Any]:
        try:
            data = resp.json()
        except ValueError as e:
            raise NeteaseError("网易云接口返回的不是 JSON") from e
        if check and data.get("code") != 200:
            message = data.get("message") or data.get("msg") or "未知错误"
            raise NeteaseError(f"网易云接口错误: {message}", data.get("code"))
        return data

    def _eapi_header(self, cookies: Mapping[str, str]) -> dict[str, str]:
        header = {
            "osver": cookies.get("osver", ""),
            "deviceId": cookies.get("deviceId", ""),
            "os": cookies.get("os", ""),
            "appver": cookies.get("appver", ""),
            "versioncode": cookies.get("versioncode", "140"),
            "mobilename": cookies.get("mobilename", ""),
            "buildver": cookies.get("buildver", str(int(time.time()))),
            "resolution": cookies.get("resolution", "1920x1080"),
            "__csrf": cookies.get("__csrf", ""),
            "channel": cookies.get("channel", ""),
            "requestId": f"{int(time.time() * 1000)}_{random.randint(0, 999):04d}",
        }
        for key in ("MUSIC_U", "MUSIC_A"):
            if cookies.get(key):
                header[key] = cookies[key]
        return header

    def eapi_response(
        self,
        path: str,
        payload: Mapping[str, Any],
        *,
        force_device: Mapping[str, str] | None = None,
    ) -> requests.Response:
        cookies = self.cookies(force_device)
        url = EAPI_BASE + path
        body = dict(payload)
        body["header"] = json.dumps(self._eapi_header(cookies))
        return self._send("POST", url, cookies, data={"params": eapi_encrypt(url, body)})

    def eapi(self, path: str, payload: Mapping[str, Any], **kwargs) -> dict[str, Any]:
        return self._json(self.eapi_response(path, payload, **kwargs))

    def api(
        self, url: str, data: Mapping[str, Any] | None = None, *, check: bool = True
    ) -> dict[str, Any]:
        return self._json(self._send("POST", url, self.cookies(), data=data or {}), check=check)

    def api_get(self, url: str, *, check: bool = True) -> dict[str, Any]:
        return self._json(self._send("GET", url, self.cookies()), check=check)

    # ----------- 歌曲 -----------

    def song_url_raw(self, song_id: int, level: str) -> dict[str, Any]:
        lv = get_level(level)
        payload: dict[str, Any] = {
            "ids": [int(song_id)],
            "level": lv.key,
            "encodeType": lv.encode_type,
        }
        if lv.immerse_type:
            payload["immerseType"] = lv.immerse_type
        return self.eapi(
            "/song/enhance/player/url/v1", payload, force_device=lv.force_device or None
        )

    def song_url(self, song_id: int, level: str) -> TrackSource:
        data = self.song_url_raw(song_id, level).get("data") or []
        if not data:
            return TrackSource.from_api({"id": song_id, "code": 404}, level)
        return TrackSource.from_api(data[0], level)

    def song_details_raw(self, ids: list[int]) -> dict[str, Any]:
        c = json.dumps([{"id": int(i)} for i in ids])
        return self.api(f"{API_BASE}/v3/song/detail", {"c": c})

    def song_details(
        self, ids: list[int], *, batch: int = 500, workers: int = 4
    ) -> list[dict[str, Any]]:
        """批量获取歌曲详情，按传入顺序返回（已下架等无详情的歌曲会被跳过）。"""
        ids = [int(i) for i in ids]
        chunks = [ids[i : i + batch] for i in range(0, len(ids), batch)]
        if len(chunks) <= 1:
            results = [self.song_details_raw(c) for c in chunks]
        else:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                results = list(ex.map(self.song_details_raw, chunks))
        by_id: dict[int, dict[str, Any]] = {}
        for res in results:
            privileges = {p.get("id"): p for p in res.get("privileges") or []}
            for song in res.get("songs") or []:
                song = dict(song)
                song["privilege"] = privileges.get(song.get("id"), {})
                by_id[song["id"]] = song
        return [by_id[i] for i in ids if i in by_id]

    def song_detail(self, song_id: int) -> dict[str, Any]:
        songs = self.song_details([song_id])
        if not songs:
            raise NeteaseError("未找到这首歌曲", 404)
        return songs[0]

    def lyric_raw(self, song_id: int) -> dict[str, Any]:
        data = {"id": int(song_id), "cp": "false", "tv": "0", "lv": "0", "rv": "0", "kv": "0",
                "yv": "0", "ytv": "0", "yrv": "0"}  # fmt: skip
        return self.api(f"{API_BASE}/song/lyric", data)

    def lyrics(self, song_id: int) -> Lyrics:
        raw = self.lyric_raw(song_id)

        def text(key: str) -> str:
            return ((raw.get(key) or {}).get("lyric") or "").strip()

        no_lyrics = bool(raw.get("nolyric") or raw.get("uncollected") or raw.get("pureMusic"))
        return Lyrics(text("lrc"), text("tlyric"), text("romalrc"), no_lyrics)

    # ----------- 搜索 / 歌单 / 专辑 -----------

    SEARCH_TYPES = {1: "songs", 10: "albums", 100: "artists", 1000: "playlists"}

    def search(
        self, keyword: str, *, type: int = 1, limit: int = 30, offset: int = 0
    ) -> dict[str, Any]:
        if type not in self.SEARCH_TYPES:
            raise ValueError(
                f"不支持的搜索类型: {type}，可选: 1 单曲 / 10 专辑 / 100 歌手 / 1000 歌单"
            )
        raw = self.api(f"{WEB_BASE}/api/cloudsearch/pc",
                       {"s": keyword, "type": type, "limit": limit, "offset": offset})  # fmt: skip
        result = raw.get("result") or {}
        key = self.SEARCH_TYPES[type]
        items = result.get(key) or []
        if type == 1:
            items = [song_brief(s) for s in items]
        elif type == 10:
            items = [
                {
                    "id": a.get("id"),
                    "name": a.get("name"),
                    "picUrl": a.get("picUrl"),
                    "artist": (a.get("artist") or {}).get("name", ""),
                    "size": a.get("size"),
                    "publishTime": a.get("publishTime"),
                }
                for a in items
            ]
        elif type == 100:
            items = [
                {"id": a.get("id"), "name": a.get("name"), "picUrl": a.get("picUrl"),
                 "albumSize": a.get("albumSize"), "musicSize": a.get("musicSize")}
                for a in items
            ]  # fmt: skip
        else:
            items = [
                {
                    "id": p.get("id"),
                    "name": p.get("name"),
                    "coverImgUrl": p.get("coverImgUrl"),
                    "trackCount": p.get("trackCount"),
                    "creator": (p.get("creator") or {}).get("nickname", ""),
                }
                for p in items
            ]
        total_key = {1: "songCount", 10: "albumCount", 100: "artistCount", 1000: "playlistCount"}[
            type
        ]
        return {"type": type, "total": int(result.get(total_key) or 0), "items": items}

    def playlist(
        self, playlist_id: int, *, offset: int = 0, limit: int | None = None
    ) -> dict[str, Any]:
        # 歌单详情缓存 5 分钟，分页浏览大歌单时不必每页都重新获取 trackIds
        raw = self._playlist_cache.get_or_set(
            int(playlist_id),
            lambda: self.api(
                f"{WEB_BASE}/api/v6/playlist/detail", {"id": int(playlist_id), "n": 100000, "s": 8}
            ),
        )
        pl = raw.get("playlist") or {}
        track_ids = [t["id"] for t in pl.get("trackIds") or []]
        end = None if limit is None else offset + limit
        page_ids = track_ids[offset:end]
        tracks = [song_brief(s) for s in self.song_details(page_ids)] if page_ids else []
        return {
            "id": pl.get("id"),
            "name": pl.get("name"),
            "coverImgUrl": pl.get("coverImgUrl"),
            "creator": (pl.get("creator") or {}).get("nickname", ""),
            "trackCount": pl.get("trackCount"),
            "description": pl.get("description") or "",
            "total": len(track_ids),
            "offset": offset,
            "limit": limit,
            "unavailable": len(page_ids) - len(tracks),
            "tracks": tracks,
        }

    def album_raw(self, album_id: int) -> dict[str, Any]:
        return self._album_cache.get_or_set(
            int(album_id), lambda: self.api_get(f"{WEB_BASE}/api/v1/album/{int(album_id)}")
        )

    def album(self, album_id: int) -> dict[str, Any]:
        raw = self.album_raw(album_id)
        al = raw.get("album") or {}
        artists = _artist_names(al.get("artists")) or _artist_names([al.get("artist") or {}])
        return {
            "id": al.get("id"),
            "name": al.get("name"),
            "coverImgUrl": al.get("picUrl") or "",
            "artist": "/".join(artists),
            "artists": artists,
            "publishTime": al.get("publishTime"),
            "company": al.get("company") or "",
            "description": al.get("description") or "",
            "size": al.get("size"),
            "songs": [song_brief(s) for s in raw.get("songs") or []],
        }

    def album_tag_info(self, album_id: int) -> dict[str, Any]:
        """写标签需要的专辑信息：专辑艺人、曲目总数、碟数、发行时间、唱片公司。"""
        raw = self.album_raw(album_id)
        al = raw.get("album") or {}
        songs = raw.get("songs") or []
        discs = {_as_int(s.get("cd"), 1) for s in songs} or {1}
        return {
            "name": al.get("name") or "",
            "artists": _artist_names(al.get("artists")) or _artist_names([al.get("artist") or {}]),
            "track_total": int(al.get("size") or len(songs) or 0),
            "disc_total": max(discs),
            "publish_time": al.get("publishTime") or 0,
            "company": al.get("company") or "",
        }

    # ----------- 账号 -----------

    def account(self) -> dict[str, Any]:
        raw = self.api(f"{WEB_BASE}/api/nuser/account/get")
        account = raw.get("account") or {}
        profile = raw.get("profile") or {}
        return {
            "logged_in": bool(account.get("id")) and bool(profile),
            "user_id": account.get("id"),
            "nickname": profile.get("nickname"),
            "vip_type": account.get("vipType"),
        }

    def vip_info(self) -> dict[str, Any]:
        raw = self.api(f"{WEB_BASE}/api/music-vip-membership/client/vip/info")
        data = raw.get("data") or {}
        now_ms = time.time() * 1000

        def expire(key: str) -> int | None:
            item = data.get(key) or {}
            ts = item.get("expireTime") or 0
            return ts if item.get("vipCode") and ts > now_ms else None

        return {
            "svip_expire": expire("redplus"),
            "vip_expire": expire("associator"),
            "music_package_expire": expire("musicPackage"),
            "red_vip_level": data.get("redVipLevel"),
        }

    # ----------- 二维码登录 -----------

    def qr_unikey(self) -> str:
        data = self.eapi("/login/qrcode/unikey", {"type": 1})
        key = data.get("unikey")
        if not key:
            raise NeteaseError("生成二维码失败：未返回 unikey")
        return key

    def qr_check(self, key: str) -> dict[str, Any]:
        """返回 {code, message, cookies}；code 800 过期、801 待扫码、802 待确认、803 成功。"""
        resp = self.eapi_response("/login/qrcode/client/login", {"key": key, "type": 1})
        data = self._json(resp, check=False)
        code = data.get("code")
        cookies = {c.name: c.value for c in resp.cookies if c.value} if code == 803 else {}
        return {
            "code": code,
            "message": data.get("message") or "",
            "nickname": data.get("nickname"),
            "cookies": cookies,
        }


def qr_login_url(key: str) -> str:
    return f"https://music.163.com/login?codekey={key}"


# ============ 兼容旧版函数接口 ============


def url_v1(song_id: int, level: str, cookies: Mapping[str, str]) -> dict[str, Any]:
    return NeteaseClient(cookies).song_url_raw(song_id, level)


def name_v1(song_id: int, cookies: Mapping[str, str] | None = None) -> dict[str, Any]:
    return NeteaseClient(cookies).song_details_raw([song_id])


def lyric_v1(song_id: int, cookies: Mapping[str, str]) -> dict[str, Any]:
    return NeteaseClient(cookies).lyric_raw(song_id)


def search_music(
    keywords: str, cookies: Mapping[str, str], limit: int = 10
) -> list[dict[str, Any]]:
    return NeteaseClient(cookies).search(keywords, limit=limit)["items"]


def playlist_detail(playlist_id: int, cookies: Mapping[str, str]) -> dict[str, Any]:
    return NeteaseClient(cookies).playlist(playlist_id)


def album_detail(album_id: int, cookies: Mapping[str, str]) -> dict[str, Any]:
    return NeteaseClient(cookies).album(album_id)
