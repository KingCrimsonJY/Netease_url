"""音质等级的唯一定义：顺序、中文名、请求参数。"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class Level:
    key: str
    name: str
    description: str
    encode_type: str = "flac"
    immerse_type: str | None = None
    # 需要覆盖的设备身份（只有 vivid 需要用 Android 身份请求）
    force_device: dict[str, str] = field(default_factory=dict)


_LEVELS = (
    Level("standard", "标准音质", "MP3 128kbps"),
    Level("higher", "较高音质", "MP3 192kbps"),
    Level("exhigh", "极高音质", "MP3 320kbps"),
    Level("lossless", "无损音质", "FLAC 无损"),
    Level("hires", "Hi-Res音质", "FLAC 24bit 高采样率"),
    Level("jyeffect", "高清臻音", "FLAC 高清"),
    Level("sky", "沉浸环绕声", "FLAC 5.1 声道", immerse_type="c51"),
    Level("dolby", "杜比全景声", "Dolby Atmos (M4A)", encode_type="mp4"),
    Level(
        "vivid",
        "臻音全景声",
        "Audio Vivid (M4A)",
        encode_type="mp3",
        force_device={"os": "android", "appver": "9.5.61"},
    ),
    Level("jymaster", "超清母带", "FLAC 24bit/192kHz"),
)

LEVELS: dict[str, Level] = {lv.key: lv for lv in _LEVELS}
LEVEL_KEYS: tuple[str, ...] = tuple(LEVELS)


def get_level(key: str) -> Level:
    try:
        return LEVELS[key]
    except KeyError:
        raise ValueError(f"不支持的音质: {key}，可选: {', '.join(LEVEL_KEYS)}") from None


def level_name(key: str | None) -> str:
    if not key:
        return "未知"
    lv = LEVELS.get(key)
    return lv.name if lv else key
