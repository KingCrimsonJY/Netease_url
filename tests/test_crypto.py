"""eapi 加密：与已在线验证过的实现保持逐字节一致。"""

from music_api import eapi_encrypt

SONG_URL_V1 = "https://interface3.music.163.com/eapi/song/enhance/player/url/v1"
PAYLOAD = {"ids": [1901371647], "level": "standard", "encodeType": "flac", "header": "{}"}
# 由 2026-10 在线测试通过的原实现生成
EXPECTED = (
    "fa90b329e9614f79e79598f37dc2edb487f00d1bc4c9b24cd57e6c318b9073569338432cd7d98d1a"
    "3626e997a2c5312142e34f414c8ed726b52b8adb1f897be4e4a75ff04771602ce36e4f51b31860d1"
    "c2d5ef0d6a1783dfdcbc3cd6a84e7041ae669ade4c0033aaa3f58bf0c6cb38546ab48597161a18df"
    "0af24ca5c42f15e727554ae9f935b181b54948e3b6c0d1393a05b4093561f89a7c322064877c8680"
    "823ad806afd3af383beb23ea739809a2"
)


def test_encrypt_matches_reference_vector():
    assert eapi_encrypt(SONG_URL_V1, PAYLOAD) == EXPECTED
