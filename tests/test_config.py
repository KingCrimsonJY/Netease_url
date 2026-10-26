import pytest

from config import BASE_DIR, ConfigError, Settings, load_dotenv


def test_defaults():
    s = Settings.from_env({})
    assert s.host == "127.0.0.1" and s.port == 5000
    assert s.cookie_file == BASE_DIR / "cookie.txt"
    assert s.cache_dir == BASE_DIR / "data" / "cache"
    assert s.library_dir is None


def test_values_are_converted():
    s = Settings.from_env({
        "PORT": "8080", "MAX_CACHE_GB": "1.5", "FULLWIDTH_REPLACE": "false",
        "CORS_ORIGINS": "https://a.com, https://b.com", "LIBRARY_DIR": "music",
    })  # fmt: skip
    assert s.port == 8080 and s.max_cache_bytes == int(1.5 * 1024**3)
    assert s.fullwidth_replace is False
    assert s.cors_origins == ("https://a.com", "https://b.com")
    assert s.library_dir == BASE_DIR / "music"


@pytest.mark.parametrize(
    "env",
    [
        {"DEFAULT_LEVEL": "ultra"},
        {"LYRICS_MODE": "sidecar"},
        {"ID3_VERSION": "2"},
        {"PORT": "abc"},
        {"FILENAME_TEMPLATE": "{singer} - {title}"},
        {"MAX_CONCURRENT_DOWNLOADS": "0"},
    ],
)
def test_invalid_values_are_rejected(env):
    with pytest.raises(ConfigError):
        Settings.from_env(env)


def test_dotenv_does_not_override_existing(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text(
        '# 注释\nPORT=9000\nHOST="0.0.0.0"\nexport LOG_LEVEL=DEBUG # 行尾注释\n', encoding="utf-8"
    )
    environ = {"PORT": "7000"}
    load_dotenv(env_file, environ)
    assert environ == {"PORT": "7000", "HOST": "0.0.0.0", "LOG_LEVEL": "DEBUG"}
