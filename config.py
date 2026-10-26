"""配置：全部来自环境变量，项目目录下的 .env 文件作为补充（不覆盖已有环境变量）。"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, fields
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent

LYRICS_MODES = ("embed", "lrc", "both", "none")
LYRICS_TRANSLATION_MODES = ("merge", "none")


class ConfigError(ValueError):
    """配置值不合法。"""


def load_dotenv(path: Path, environ: dict[str, str] | None = None) -> None:
    """读取 KEY=VALUE 格式的 .env 文件，只填充尚未设置的变量。"""
    environ = os.environ if environ is None else environ
    if not path.is_file():
        return
    for raw in path.read_text(encoding="utf-8-sig").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip().removeprefix("export ").strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        elif " #" in value:
            value = value.split(" #", 1)[0].rstrip()
        environ.setdefault(key, value)


def _resolve(path: str | Path) -> Path:
    p = Path(path).expanduser()
    return p if p.is_absolute() else (BASE_DIR / p)


@dataclass(frozen=True)
class Settings:
    # 服务
    host: str = "127.0.0.1"
    port: int = 5000
    api_token: str = ""
    cors_origins: tuple[str, ...] = ()
    rate_limit_per_minute: int = 60
    trust_proxy: bool = False  # 部署在反向代理后面时开启，按 X-Forwarded-For 识别客户端 IP
    log_level: str = "INFO"

    # 账号与设备身份
    cookie_file: Path = field(default_factory=lambda: _resolve("cookie.txt"))
    netease_os: str = "pc"
    netease_appver: str = ""  # 留空则使用设备档案里的默认版本
    netease_device_id: str = "pyncm!"

    # 存储与下载
    data_dir: Path = field(default_factory=lambda: _resolve("data"))
    max_cache_gb: float = 20.0
    max_concurrent_downloads: int = 3
    default_level: str = "lossless"
    sync_download_timeout: int = 1800

    # 元数据
    cover_size: int = 1200
    lyrics_mode: str = "embed"
    lyrics_translation: str = "merge"
    id3_version: int = 4

    # 文件名
    filename_template: str = "{artists} - {title}"
    artist_separator: str = ", "
    max_artists_in_filename: int = 3
    fullwidth_replace: bool = True

    # 可选：按友好文件名导出到音乐库目录
    library_dir: Path | None = None

    @property
    def cache_dir(self) -> Path:
        return self.data_dir / "cache"

    @property
    def max_cache_bytes(self) -> int:
        return int(self.max_cache_gb * 1024**3)

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None, *, dotenv: bool = True) -> Settings:
        if environ is None:
            if dotenv:
                load_dotenv(BASE_DIR / ".env")
            environ = os.environ
        values: dict[str, object] = {}
        for f in fields(cls):
            raw = environ.get(f.name.upper())
            if raw is None or raw == "":
                continue
            values[f.name] = _convert(f.name, raw)
        settings = cls(**values)
        settings.validate()
        return settings

    def validate(self) -> None:
        from levels import LEVELS

        if self.default_level not in LEVELS:
            raise ConfigError(f"DEFAULT_LEVEL 不支持: {self.default_level}")
        if self.lyrics_mode not in LYRICS_MODES:
            raise ConfigError(f"LYRICS_MODE 只能是 {', '.join(LYRICS_MODES)}")
        if self.lyrics_translation not in LYRICS_TRANSLATION_MODES:
            raise ConfigError(f"LYRICS_TRANSLATION 只能是 {', '.join(LYRICS_TRANSLATION_MODES)}")
        if self.id3_version not in (3, 4):
            raise ConfigError("ID3_VERSION 只能是 3 或 4")
        if self.cover_size < 0 or self.max_cache_gb < 0:
            raise ConfigError("COVER_SIZE / MAX_CACHE_GB 不能为负数")
        if self.max_concurrent_downloads < 1 or self.max_artists_in_filename < 1:
            raise ConfigError("MAX_CONCURRENT_DOWNLOADS / MAX_ARTISTS_IN_FILENAME 至少为 1")
        validate_template(self.filename_template)


# 文件名模板可用的变量及其示例值
TEMPLATE_SAMPLE = {
    "artists": "歌手A, 歌手B",
    "artist": "歌手A",
    "title": "歌名",
    "album": "专辑",
    "track": 1,
    "disc": 1,
    "year": "2024",
    "level": "lossless",
    "quality_name": "无损音质",
    "id": 1,
}


def validate_template(template: str) -> None:
    try:
        rendered = template.format_map(TEMPLATE_SAMPLE)
    except (KeyError, IndexError, ValueError) as e:
        allowed = ", ".join("{" + k + "}" for k in TEMPLATE_SAMPLE)
        raise ConfigError(f"FILENAME_TEMPLATE 不合法: {template!r}（可用变量: {allowed}）") from e
    if not rendered.strip():
        raise ConfigError("FILENAME_TEMPLATE 渲染结果为空")


def _convert(name: str, value: str) -> object:
    raw = value.strip()
    if name in ("artist_separator", "filename_template"):
        return value  # 这两项允许首尾空格（如分隔符 ", "）
    try:
        if name in (
            "port",
            "rate_limit_per_minute",
            "max_concurrent_downloads",
            "cover_size",
            "id3_version",
            "max_artists_in_filename",
            "sync_download_timeout",
        ):
            return int(raw)
        if name == "max_cache_gb":
            return float(raw)
        if name in ("fullwidth_replace", "trust_proxy"):
            return raw.lower() in ("1", "true", "yes", "on")
        if name == "cors_origins":
            return tuple(o.strip() for o in raw.split(",") if o.strip())
        if name in ("cookie_file", "data_dir", "library_dir"):
            return _resolve(raw)
    except ValueError as e:
        raise ConfigError(f"{name.upper()} 的值不合法: {raw!r}") from e
    return raw
