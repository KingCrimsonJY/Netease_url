"""网易云音乐解析服务（Flask）。

启动：python main.py（使用 waitress，单进程多线程）
"""

from __future__ import annotations

import logging
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

from flask import Blueprint, Flask, current_app, jsonify, render_template, request, send_file

from config import Settings
from levels import LEVELS, get_level, level_name
from music_api import NeteaseClient, NeteaseError
from music_downloader import DownloadException, MusicDownloader
from utils import format_size

VERSION = "3.0.0"
log = logging.getLogger("netease")
bp = Blueprint("api", __name__)


# ============ 响应与错误 ============


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, error_code: str = "BAD_REQUEST", data=None):
        super().__init__(message)
        self.message = message
        self.status = status
        self.error_code = error_code
        self.data = data


def ok(data: Any = None, message: str = "success", status: int = 200, **extra):
    body: dict[str, Any] = {"status": status, "success": True, "message": message}
    if data is not None:
        body["data"] = data
    body.update(extra)
    return jsonify(body), status


def fail(message: str, status: int = 400, error_code: str = "BAD_REQUEST", data: Any = None):
    body: dict[str, Any] = {
        "status": status,
        "success": False,
        "message": message,
        "error_code": error_code,
    }
    if data is not None:
        body["data"] = data
    return jsonify(body), status


# 下载/解析错误码 -> HTTP 状态码
ERROR_STATUS = {
    "INVALID_INPUT": 400,
    "TRIAL_ONLY": 403,
    "NEED_VIP_OR_PURCHASE": 403,
    "UNAVAILABLE": 404,
    "NO_URL": 404,
    "NOT_FOUND": 404,
    "QUALITY_DOWNGRADED": 409,
    "UPSTREAM_ERROR": 502,
    "DOWNLOAD_FAILED": 502,
}


@dataclass
class Services:
    settings: Settings
    client: NeteaseClient
    downloader: MusicDownloader


def services() -> Services:
    return current_app.extensions["netease"]


# ============ 参数解析 ============


def params() -> dict[str, Any]:
    """合并 query、表单和 JSON 参数（JSON 优先）。"""
    data: dict[str, Any] = dict(request.args)
    if request.method != "GET":
        data.update(request.form)
        data.update(request.get_json(silent=True) or {})
    return data


def song_id_param(data: dict[str, Any]) -> int:
    raw = str(data.get("ids") or data.get("id") or data.get("url") or "").strip()
    if not raw:
        raise ApiError("必须提供 'ids'、'id' 或 'url' 参数", 400, "INVALID_INPUT")
    m = re.search(r"[?&]id=(\d+)", raw) if "music.163.com" in raw else None
    value = m.group(1) if m else raw
    if not re.fullmatch(r"\d{1,20}", value):
        raise ApiError(f"无法识别的歌曲 ID 或链接: {raw[:80]}", 400, "INVALID_INPUT")
    return int(value)


def level_param(data: dict[str, Any], *keys: str) -> str:
    settings = services().settings
    value = next((str(data[k]) for k in keys if data.get(k)), settings.default_level)
    try:
        return get_level(value).key
    except ValueError as e:
        raise ApiError(str(e), 400, "INVALID_INPUT") from None


def bool_param(data: dict[str, Any], key: str) -> bool:
    return str(data.get(key, "")).lower() in ("1", "true", "yes", "on")


def int_param(data: dict[str, Any], key: str, default: int, lo: int, hi: int | None = None) -> int:
    raw = data.get(key)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        raise ApiError(f"参数 '{key}' 必须是整数", 400, "INVALID_INPUT") from None
    if value < lo or (hi is not None and value > hi):
        rng = f"{lo}~{hi}" if hi is not None else f">= {lo}"
        raise ApiError(f"参数 '{key}' 超出范围（{rng}）", 400, "INVALID_INPUT")
    return value


# ============ 路由 ============


@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/health", methods=["GET"])
def health():
    return ok(
        {"service": "running", "version": VERSION, "timestamp": int(time.time())}, "API服务运行正常"
    )


@bp.route("/song", methods=["GET", "POST"])
@bp.route("/Song_V1", methods=["GET", "POST"])
def song():
    data = params()
    song_id = song_id_param(data)
    level = level_param(data, "level", "quality")
    info_type = data.get("type") or "url"
    if info_type not in ("url", "name", "lyric", "json"):
        raise ApiError("无效的类型参数，支持: url, name, lyric, json", 400, "INVALID_INPUT")
    client = services().client

    if info_type == "name":
        raw = client.song_details_raw([song_id])
        if not raw.get("songs"):
            raise ApiError("未找到这首歌曲", 404, "NOT_FOUND")
        return ok(raw, "获取歌曲信息成功")
    if info_type == "lyric":
        return ok(client.lyric_raw(song_id), "获取歌词成功")

    source = client.song_url(song_id, level)
    if info_type == "url":
        if not source.available:
            raise ApiError(source.error_message, ERROR_STATUS[source.error_code], source.error_code,
                           data=source.to_dict())  # fmt: skip
        if bool_param(data, "strict") and source.downgraded:
            raise ApiError(
                f"实际音质为{level_name(source.level)}，与请求的{level_name(level)}不一致",
                409, "QUALITY_DOWNGRADED", data=source.to_dict(),
            )  # fmt: skip
        return ok(source_payload(source), "获取歌曲URL成功")

    # type=json：前端单曲解析用的合并信息
    detail = client.song_detail(song_id)
    lyrics = client.lyrics(song_id)
    al = detail.get("al") or {}
    privilege = detail.get("privilege") or {}
    payload = {
        "id": song_id,
        "name": detail.get("name", ""),
        "ar_name": ", ".join(a.get("name", "") for a in detail.get("ar") or []),
        "al_name": al.get("name", ""),
        "pic": al.get("picUrl", ""),
        "lyric": lyrics.lrc,
        "tlyric": lyrics.tlyric,
        "max_level": privilege.get("plLevel")
        if privilege.get("plLevel") not in (None, "none")
        else None,
        **source_payload(source),
        "size": format_size(source.size) if source.available else "获取失败",
        "url": source.url or "",
    }
    return ok(payload, "获取歌曲信息成功")


def source_payload(source) -> dict[str, Any]:
    return {
        "id": source.song_id,
        "url": source.url,
        "level": source.level,
        "requested_level": source.requested_level,
        "downgraded": source.downgraded,
        "quality_name": level_name(source.level),
        "available": source.available,
        "error_code": source.error_code,
        "error_message": source.error_message,
        "size": source.size,
        "size_formatted": format_size(source.size),
        "type": source.type,
        "bitrate": source.bitrate,
        "sample_rate": source.sample_rate,
        "md5": source.md5,
        "expires_in": max(0, int(source.expires_at - time.time())),
    }


@bp.route("/search", methods=["GET", "POST"])
@bp.route("/Search", methods=["GET", "POST"])
def search():
    data = params()
    keyword = str(data.get("keyword") or data.get("keywords") or data.get("q") or "").strip()
    if not keyword:
        raise ApiError("参数 'keyword' 不能为空", 400, "INVALID_INPUT")
    limit = int_param(data, "limit", 30, 1, 100)
    offset = int_param(data, "offset", 0, 0)
    search_type = int_param(data, "type", 1, 1)
    try:
        result = services().client.search(keyword, type=search_type, limit=limit, offset=offset)
    except ValueError as e:
        raise ApiError(str(e), 400, "INVALID_INPUT") from None
    items = result["items"]
    if search_type == 1:
        for item in items:
            item["artist_string"] = item["artists"]
    return ok(
        items, "搜索完成", total=result["total"], offset=offset, limit=limit, type=search_type
    )


@bp.route("/playlist", methods=["GET", "POST"])
@bp.route("/Playlist", methods=["GET", "POST"])
def playlist():
    data = params()
    playlist_id = song_id_param({"id": data.get("id") or data.get("url")})
    offset = int_param(data, "offset", 0, 0)
    limit = int_param(data, "limit", 0, 0) or None
    result = services().client.playlist(playlist_id, offset=offset, limit=limit)
    return ok({"status": "success", "playlist": result}, "获取歌单详情成功")


@bp.route("/album", methods=["GET", "POST"])
@bp.route("/Album", methods=["GET", "POST"])
def album():
    data = params()
    album_id = song_id_param({"id": data.get("id") or data.get("url")})
    return ok({"status": 200, "album": services().client.album(album_id)}, "获取专辑详情成功")


@bp.route("/download", methods=["GET", "POST"])
@bp.route("/Download", methods=["GET", "POST"])
def download():
    data = params()
    song_id = song_id_param(data)
    level = level_param(data, "quality", "level")
    return_format = data.get("format") or "file"
    if return_format not in ("file", "json"):
        raise ApiError("返回格式只支持 'file' 或 'json'", 400, "INVALID_INPUT")
    try:
        result = services().downloader.download_music_file(song_id, level)
    except DownloadException as e:
        raise ApiError(f"下载失败: {e}", 502, "DOWNLOAD_FAILED") from e
    if not result.success:
        raise ApiError(f"下载失败: {result.error_message}", 502, "DOWNLOAD_FAILED")
    info = result.music_info
    path = Path(result.file_path)
    if return_format == "json":
        return ok(
            {
                "music_id": song_id,
                "name": info.name,
                "artist": info.artists,
                "album": info.album,
                "quality": info.quality,
                "requested_quality": level,
                "quality_name": level_name(info.quality),
                "file_type": info.file_type,
                "file_size": path.stat().st_size,
                "file_size_formatted": format_size(path.stat().st_size),
                "filename": path.name,
                "duration": info.duration,
            },
            "下载完成",
        )
    response = send_file(path, as_attachment=True, download_name=path.name, conditional=True)
    response.headers["X-Download-Filename"] = quote(path.name, safe="")
    return response


@bp.route("/api/info", methods=["GET"])
def api_info():
    settings = services().settings
    return ok(
        {
            "name": "网易云音乐API服务",
            "version": VERSION,
            "endpoints": {
                "/health": "GET - 健康检查",
                "/song": "GET/POST - 歌曲信息（type=url|name|lyric|json）",
                "/search": "GET/POST - 搜索（type=1|10|100|1000）",
                "/playlist": "GET/POST - 歌单详情",
                "/album": "GET/POST - 专辑详情",
                "/download": "GET/POST - 下载（format=file|json）",
                "/api/info": "GET - 服务信息",
            },
            "supported_qualities": [
                {"key": lv.key, "name": lv.name, "description": lv.description}
                for lv in LEVELS.values()
            ],  # fmt: skip
            "default_level": settings.default_level,
        },
        "API信息获取成功",
    )


# ============ 应用工厂 ============


def register_error_handlers(app: Flask) -> None:
    @app.errorhandler(ApiError)
    def _api_error(e: ApiError):
        return fail(e.message, e.status, e.error_code, e.data)

    @app.errorhandler(NeteaseError)
    def _netease_error(e: NeteaseError):
        log.warning("网易云接口错误: %s (code=%s)", e.message, e.code)
        return fail(e.message, 502, "UPSTREAM_ERROR")

    @app.errorhandler(404)
    def _not_found(e):
        return fail("请求的资源不存在", 404, "NOT_FOUND")

    @app.errorhandler(405)
    def _method_not_allowed(e):
        return fail("不支持的请求方法", 405, "METHOD_NOT_ALLOWED")

    @app.errorhandler(Exception)
    def _internal(e: Exception):
        log.exception("未处理的异常")
        return fail("服务器内部错误", 500, "INTERNAL_ERROR")


def setup_logging(settings: Settings) -> None:
    logger = logging.getLogger("netease")
    if logger.handlers:
        return
    logger.setLevel(settings.log_level.upper())
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    logger.addHandler(handler)


def create_app(
    settings: Settings | None = None,
    *,
    client: NeteaseClient | None = None,
    downloader: MusicDownloader | None = None,
) -> Flask:
    settings = settings or Settings.from_env()
    setup_logging(settings)
    client = client or NeteaseClient.from_settings(settings)
    if downloader is None:
        downloader = MusicDownloader(download_dir=str(settings.data_dir / "downloads"))
        downloader.api = client
    app = Flask(__name__)
    app.json.ensure_ascii = False
    app.extensions["netease"] = Services(settings, client, downloader)
    app.register_blueprint(bp)
    register_error_handlers(app)

    @app.before_request
    def _log_request():
        log.info("%s %s - %s", request.method, request.path, request.remote_addr)

    return app


def main() -> None:
    settings = Settings.from_env()
    app = create_app(settings)
    log.info("服务地址: http://%s:%s", settings.host, settings.port)
    try:
        from waitress import serve
    except ImportError:  # pragma: no cover
        app.run(host=settings.host, port=settings.port, threaded=True)
        return
    serve(app, host=settings.host, port=settings.port, threads=16)


if __name__ == "__main__":
    sys.exit(main())
