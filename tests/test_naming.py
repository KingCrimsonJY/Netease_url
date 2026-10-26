import pytest

from naming import build_filename, join_artists

# 测试报告第三节中的真实例子
CASES = [
    ("What is Love?", ["TWICE"], ".mp3", "TWICE - What is Love？.mp3"),
    ("T.N.T.", ["AC/DC"], ".mp3", "AC／DC - T.N.T..mp3"),
    (
        "想你就写信 (Live)",
        ["周杰伦", "李硕", "张鑫"],
        ".flac",
        "周杰伦, 李硕, 张鑫 - 想你就写信 (Live).flac",
    ),
    (
        '"(In My) Solitude" (Arr. Terry for Jazz Ensemble)',
        ["Joyce DiDonato", "Charlie Porter", "Chuck Israels", "Craig Terry", "Jimmy Madison"],
        ".mp3",
        "Joyce DiDonato, Charlie Porter, Chuck Israels 等 - ＂(In My) Solitude＂ (Arr. Terry for Jazz Ensemble).mp3",
    ),
    ("Why.", ["阮豆"], ".mp3", "阮豆 - Why..mp3"),
    ("...", ["王OK"], ".mp3", "王OK - ....mp3"),
    ("🎵", ["Ravagre"], ".mp3", "Ravagre - 🎵.mp3"),
    ("Why?!", ["张国荣"], ".mp3", "张国荣 - Why？!.mp3"),
    ("a\tb\nc", ["x"], ".mp3", "x - a b c.mp3"),
    ("", [], ".mp3", "未知歌手 - 未知歌曲.mp3"),
]


@pytest.mark.parametrize(("title", "artists", "ext", "expected"), CASES)
def test_report_examples(title, artists, ext, expected):
    assert build_filename({"id": 1, "title": title, "artists": artists}, ext) == expected


def test_custom_template_and_separator():
    info = {
        "id": 3,
        "title": "想你就写信 (Live)",
        "artists": ["周杰伦", "李硕", "张鑫"],
        "track": 3,
    }
    name = build_filename(info, ".mp3", template="{track:02d}. {title}-{artists}", separator="、")
    assert name == "03. 想你就写信 (Live)-周杰伦、李硕、张鑫.mp3"


def test_underscore_mode():
    info = {"id": 1, "title": "Re:Zero?", "artists": ["A/B"]}
    assert build_filename(info, ".flac", fullwidth=False) == "A_B - Re_Zero_.flac"


def test_reserved_windows_names():
    assert (
        build_filename({"id": 1, "title": "CON", "artists": []}, ".mp3", template="{title}")
        == "CON_.mp3"
    )


def test_long_names_keep_title_and_fit_byte_limit():
    info = {"id": 9, "title": "长" * 200, "artists": ["歌手" * 50, "B"]}
    name = build_filename(info, ".flac")
    assert len(name.encode("utf-8")) <= 185
    assert " - 长" in name and name.endswith(".flac")


def test_level_fields():
    info = {"id": 5, "title": "x", "artists": ["y"], "level": "jymaster"}
    assert (
        build_filename(info, ".flac", template="{title} [{quality_name}] {id}")
        == "x [超清母带] 5.flac"
    )


def test_join_artists():
    assert join_artists([]) == "未知歌手"
    assert join_artists(["a", "b", "c", "d"], " & ", 2) == "a & b 等"
