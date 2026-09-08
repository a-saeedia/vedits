"""Tests for vedits. Run with the bundled Python:
    python -m pytest tests  (if pytest available)
    # or plain:
    C:\\Users\\User\\tools\\litellm\\.venv\\Scripts\\python.exe tests\\run_tests.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from vedits import ops, probe, util, presets, text


def test_parse_timecode():
    assert abs(util.parse_timecode("5") - 5.0) < 1e-6
    assert abs(util.parse_timecode("1:05") - 65.0) < 1e-6
    assert abs(util.parse_timecode("01:02:03.500") - 3723.5) < 1e-6


def test_format_timecode():
    assert util.format_timecode(3723.5) == "01:02:03.500"


def test_presets_exist():
    for name in ("tiktok_1080", "reels_1080", "shorts_1080", "youtube_1080", "square_1080"):
        p = presets.get(name)
        assert p.width > 0 and p.height > 0


def test_escape():
    assert text._escape("a,b:c'd%") == "a\\,b\\:c\\'d\\%"


def test_auto_crop_horizontal_to_vertical():
    # 16:9 source -> 9:16 target: must crop horizontal (left/right stripping).
    f = ops.auto_crop_scale_filter(1920, 1080, 1080, 1920, ops.CROP_CENTER)
    assert "crop=" in f and "scale=1080:1920" in f


def test_auto_crop_vertical_to_horizontal():
    # 9:16 source -> 16:9 target: must crop vertical.
    f = ops.auto_crop_scale_filter(1080, 1920, 1920, 1080, ops.CROP_CENTER)
    assert "crop=" in f and "scale=1920:1080" in f


def test_auto_crop_same_aspect():
    f = ops.auto_crop_scale_filter(1920, 1080, 1280, 720)
    assert "crop=" not in f
    assert "scale=1280:720" in f


def _sample():
    p = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "assets", "sample.mp4")
    assert os.path.isfile(p), "sample.mp4 missing — create with ffmpeg lavfi testsrc"
    return os.path.normpath(p)


def test_probe_and_fit(tmp="out"):
    src = _sample()
    info = probe.probe(src)
    w, h = probe.dimensions(info)
    assert w == 1280 and h == 720
    os.makedirs(tmp, exist_ok=True)
    dst = os.path.join(tmp, "fit_test.mp4")
    preset = presets.get("tiktok_1080")
    ops.fit(src, dst, preset)
    out_info = probe.probe(dst)
    ow, oh = probe.dimensions(out_info)
    assert (ow, oh) == (1080, 1920)
    return dst


def test_reencode_concat(tmp="out"):
    base = _sample()
    os.makedirs(tmp, exist_ok=True)
    a = os.path.join(tmp, "a.mp4"); b = os.path.join(tmp, "b.mp4")
    ops.trim(base, a, start=0, end=1)
    ops.trim(base, b, start=0, end=1)
    c = os.path.join(tmp, "concat.mp4")
    ops.concat([a, b], c)
    assert abs(probe.duration(probe.probe(c)) - 2.0) < 0.5


if __name__ == "__main__":
    failures = 0
    for fn in [test_parse_timecode, test_format_timecode, test_presets_exist,
               test_escape, test_auto_crop_horizontal_to_vertical,
               test_auto_crop_vertical_to_horizontal, test_auto_crop_same_aspect,
               test_probe_and_fit, test_reencode_concat]:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failures += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    sys.exit(1 if failures else 0)
