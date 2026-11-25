"""单元测试：图像预处理、分块编码、原子写盘、面积惩罚。

这些用例不依赖外部素材，全部用合成图，几秒内跑完。
"""
from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hdrgainmap import gainmap                       # noqa: E402
from hdrgainmap.pipeline import _hdr_code, _write_atomic, build  # noqa: E402


def test_orientation_applied(tmp_path: Path) -> None:
    """EXIF orientation=6 必须在读图时转正；转过的图不能再复用源字节。"""
    path = tmp_path / "o.jpg"
    im = Image.new("RGB", (64, 32), (200, 100, 50))
    exif = Image.Exif()
    exif[0x0112] = 6
    im.save(path, exif=exif)

    rgb, _, meta = gainmap.prepare_image(path)
    assert meta["orientation"] == 6
    assert meta["reusable_source"] is False
    assert rgb.shape[:2] == (64, 32)              # 64x32 → 旋转后 32x64


def test_alpha_flatten_bg(tmp_path: Path) -> None:
    """透明区域按指定底色合成；默认黑底，可切换白底。"""
    path = tmp_path / "a.png"
    Image.new("RGBA", (8, 8), (255, 0, 0, 0)).save(path)

    rgb, _, meta = gainmap.prepare_image(path, alpha_bg=(255, 255, 255))
    assert meta["had_alpha"] is True
    assert meta["reusable_source"] is False
    assert np.allclose(rgb, 1.0)

    black, _, _ = gainmap.prepare_image(path)
    assert np.allclose(black, 0.0)


def test_icc_to_srgb(tmp_path: Path) -> None:
    """非 sRGB 的 ICC（LAB）应转到 sRGB；LAB 的 a 轴不能被误判成 alpha。"""
    from PIL import ImageCms

    p = tmp_path / "lab.tif"
    rng = np.random.default_rng(0)
    arr = (rng.random((32, 32, 3)) * 255).astype(np.uint8)
    srgb = ImageCms.createProfile("sRGB")
    lab = ImageCms.createProfile("LAB")
    lab_im = ImageCms.profileToProfile(Image.fromarray(arr, "RGB"), srgb, lab,
                                       outputMode="LAB")
    lab_im.save(p, icc_profile=ImageCms.ImageCmsProfile(lab).tobytes())

    rgb, _, meta = gainmap.prepare_image(p)
    assert meta["had_alpha"] is False
    assert meta["icc_converted"] is True
    assert rgb.shape == (32, 32, 3)
    assert np.abs(rgb * 255 - arr).mean() < 4.0


def test_jpeg_reusable_flag(tmp_path: Path) -> None:
    """普通 JPEG（无方向/alpha/ICC 改动）才允许复用原始字节。"""
    plain = tmp_path / "plain.jpg"
    Image.new("RGB", (16, 16), (10, 20, 30)).save(plain)
    assert gainmap.prepare_image(plain)[2]["reusable_source"] is True

    png = tmp_path / "x.png"
    Image.new("RGB", (16, 16), (10, 20, 30)).save(png)
    assert gainmap.prepare_image(png)[2]["reusable_source"] is False


def test_hdr_code_tiles_match() -> None:
    """按行分块编码必须与整图编码逐码值一致。"""
    rng = np.random.default_rng(3)
    rgb = rng.random((9, 7, 3), dtype=np.float32)
    gain = 1.0 + rng.random((9, 7), dtype=np.float32)
    q_tiled, peak_tiled = _hdr_code(rgb, gain, "PQ", tile_rows=2)
    q_full, peak_full = _hdr_code(rgb, gain, "PQ", tile_rows=1000)
    assert np.array_equal(q_tiled, q_full)
    assert abs(peak_tiled - peak_full) < 1e-3

    q_hlg, _ = _hdr_code(rgb, gain, "HLG", tile_rows=3)
    assert q_hlg.shape == rgb.shape


def test_large_flat_region_penalised() -> None:
    """同样亮度下，大片平亮区（白墙）的增益应低于小光点（灯）。"""
    def mean_gain(side: int) -> float:
        size = 200
        img = np.full((size, size, 3), 0.02, np.float32)
        c = size // 2 - side // 2
        img[c:c + side, c:c + side] = 0.98
        cfg = gainmap.adapt_params(img)
        _, emis = gainmap.emissive(img, cfg)
        emis = gainmap.finalize(emis)
        g = gainmap.gain_of(emis, cfg["gmax"])
        return float(g[c:c + side, c:c + side].mean())

    small, large = mean_gain(12), mean_gain(160)
    assert small > large * 1.1, (small, large)


def test_gain_monotonic() -> None:
    """增益随发光权重单调不减。"""
    emis = np.linspace(0.0, 1.0, 33, dtype=np.float32)
    gain = gainmap.gain_of(emis, 1.2)
    assert np.all(np.diff(gain) >= 0)


def test_atomic_write(tmp_path: Path) -> None:
    """原子写：内容落盘，且不留下临时文件。"""
    path = tmp_path / "x.bin"
    _write_atomic(path, b"abc")
    assert path.read_bytes() == b"abc"
    assert not list(tmp_path.glob("*.tmp"))


def test_build_reuses_source_jpeg(tmp_path: Path) -> None:
    """纯 JPEG 源且 DPI 不变时，SDR 主图应直接复用原始字节。"""
    src = tmp_path / "a.jpg"
    Image.new("RGB", (32, 32), (40, 60, 80)).save(src, dpi=(96, 96))
    res = build(src, tmp_path / "out", formats=("jpeg",))
    assert res["ok"]
    assert res["stats"]["base_reused"] is True


def test_build_argument_validation(tmp_path: Path) -> None:
    """API 入口先校验参数，避免半途失败留下半套产物。"""
    bad = ({"formats": ("bmp",)}, {"quality": 0}, {"transfer": "SRGB"}, {"dpi": 0})
    for kwargs in bad:
        with pytest.raises(ValueError):
            build(tmp_path / "x.png", tmp_path / "out", **kwargs)


def test_debug_outputs_gainmaps(tmp_path: Path) -> None:
    """debug=True 输出发光场、全分辨率增益图，以及 JPEG 内嵌的那张增益图。"""
    src = tmp_path / "light.png"
    size = 96
    img = np.full((size, size, 3), 20, np.uint8)
    img[30:66, 30:66] = 240
    Image.fromarray(img, "RGB").save(src)

    res = build(src, tmp_path / "out", formats=("jpeg",), debug=True)
    assert res["ok"]
    out = tmp_path / "out"
    gain = Image.open(out / "light_gainmap.png")
    emis = Image.open(out / "light_emis.png")
    embedded = Image.open(out / "light_gainmap_embedded.png")
    assert gain.mode == "L" and gain.size == (size, size)
    assert emis.size == (size, size)
    assert embedded.size == (size // 2, size // 2)
    assert np.asarray(gain).max() > 0
