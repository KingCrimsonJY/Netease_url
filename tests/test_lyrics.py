import lyrics

LRC = """[00:00.000] 作词 : 唐恬
[00:00.463] 作曲 : 钱雷
[00:00.926] 编曲 : 钱雷
[00:12.50]Hello
[00:15.5]World
[00:20.000]"""

TLYRIC = """[by:译者]
[00:12.500]你好
[00:15.500]//
"""


def test_credits():
    assert lyrics.credits(LRC) == {"lyricist": "唐恬", "composer": "钱雷"}


def test_merge_translation_matches_normalized_timestamps():
    merged = lyrics.merge_translation(LRC, TLYRIC).splitlines()
    i = merged.index("[00:12.50]Hello")
    assert merged[i + 1] == "[00:12.50]你好"
    j = merged.index("[00:15.5]World")
    assert merged[j + 1] == "[00:20.000]"  # 译文 "//" 视为空


def test_normalize_json_metadata_lines():
    raw = '{"t":0,"c":[{"tx":"作词: "},{"tx":"唐恬","li":"x"}]}\n[00:01.00]歌词'
    assert lyrics.normalize(raw) == "[00:00.000]作词: 唐恬\n[00:01.00]歌词"
    assert lyrics.credits(lyrics.normalize(raw)) == {"lyricist": "唐恬"}


def test_build_without_translation():
    assert lyrics.build(LRC, TLYRIC, translation="none") == LRC
