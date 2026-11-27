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
from hdrgainmap import ultrahdr, validate            # noqa: E402
from hdrgainmap.pipeline import (                     # noqa: E402
    _channel_terms, _hdr_code, _write_atomic, build)


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
    emis = rng.random((9, 7), dtype=np.float32)
    w = np.array([1.08, 1.0, 0.90], dtype=np.float32)
    q_tiled, peak_tiled = _hdr_code(rgb, emis, 1.2, w, tile_rows=2)
    q_full, peak_full = _hdr_code(rgb, emis, 1.2, w, tile_rows=1000)
    assert np.array_equal(q_tiled, q_full)
    assert abs(peak_tiled - peak_full) < 1e-3
    assert q_tiled.shape == rgb.shape


def test_channel_terms_keep_luminance() -> None:
    """分通道权重按亮度归一：加权亮度增益仍等于 2^(gmax·emis)。"""
    rng = np.random.default_rng(11)
    rgb = rng.random((6, 5, 3), dtype=np.float32)
    emis = rng.random((6, 5), dtype=np.float32)
    w = np.array([1.08, 1.0, 0.90], dtype=np.float32)
    _, expo = _channel_terms(rgb, emis, 1.2, w)
    lum_gain = (0.2126 * np.exp2(expo[..., 0]) + 0.7152 * np.exp2(expo[..., 1])
                + 0.0722 * np.exp2(expo[..., 2]))
    assert np.allclose(lum_gain, np.exp2(1.2 * emis), rtol=0.01)


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
    bad = ({"formats": ("bmp",)}, {"quality": 0}, {"dpi": 0})
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
    assert embedded.mode == "RGB" and embedded.size == (size, size)
    assert np.asarray(gain).max() > 0


def test_iso_payload_spec_layout() -> None:
    """ISO 21496-1 载荷必须是大端；字节与参考实现逐位一致。"""
    payload = ultrahdr._iso_metadata(0.89387586, 0.015625)
    expected = bytes.fromhex(
        "0000000040" "0000000005f5e100" "0553f24205f5e100"
        "0000000005f5e100" "0553f24205f5e100" "0000000100000001"
        "0017d78405f5e100" "0017d78405f5e100")
    assert payload == expected

    parsed = validate.parse_iso(ultrahdr.ISO_NS + payload)
    assert parsed is not None
    assert parsed["alt_headroom"] == pytest.approx(0.89387586)
    assert parsed["min0"] == pytest.approx(0.0)
    assert parsed["max0"] == pytest.approx(0.89387586)
    assert parsed["gamma0"] == pytest.approx(1.0)
    assert parsed["base_offset0"] == pytest.approx(0.015625)


def test_iso_three_channel_layout() -> None:
    """三通道载荷 141 字节：5 头 + 2 组头度 + 3×40，flags 0xC0。"""
    payload = ultrahdr._iso_metadata_channels((0.9, 1.2, 0.7), 0.015625)
    assert len(payload) == 141
    assert payload[4] == 0xC0

    parsed = validate.parse_iso(ultrahdr.ISO_NS + payload)
    assert parsed is not None and parsed["channels"] == 3
    assert parsed["alt_headroom"] == pytest.approx(1.2)
    for i, ch in enumerate((0.9, 1.2, 0.7)):
        assert parsed[f"max{i}"] == pytest.approx(ch)
        assert parsed[f"base_offset{i}"] == pytest.approx(0.015625)


def test_ultrahdr_reconstruction_matches_png(tmp_path: Path) -> None:
    """JPEG 三通道增益图重建的 HDR 应与 PNG 母版一致（8bit 量化误差内）。"""
    import io

    from hdrgainmap import png16
    from hdrgainmap.color import pq_eotf, srgb_to_linear, to_bt2020

    src = tmp_path / "src.png"
    img = np.full((80, 120, 3), 24, np.uint8)
    img[20:60, 40:90] = 235
    img[10:25, 95:110] = (250, 180, 90)
    Image.fromarray(img, "RGB").save(src)

    res = build(src, tmp_path / "out", formats=("jpeg", "png"))
    assert res["ok"], res["errors"]
    blob = Path(res["files"]["jpeg"]).read_bytes()
    info = ultrahdr.parse(blob)
    assert info.get("gmax_channels")
    gm = np.asarray(Image.open(io.BytesIO(ultrahdr.read_gainmap(blob)))
                    .convert("RGB"), np.float32) / 255.0
    sdr = np.asarray(Image.open(io.BytesIO(blob[:info["primary_len"]]))
                     .convert("RGB"), np.float32) / 255.0
    rec = to_bt2020(ultrahdr.reconstruct(srgb_to_linear(sdr), gm, info["gmax_channels"]))

    code = png16.decode(Path(res["files"]["png"]).read_bytes()).astype(np.float32) / 65535.0
    ref = pq_eotf(code) / 203.0
    mask = ref > 0.05
    rel = np.abs(rec[mask] - ref[mask]) / ref[mask]
    assert float(np.median(rel)) < 0.05, float(np.median(rel))


def test_build_writes_iso_by_default(tmp_path: Path) -> None:
    """Ultra HDR 1.1 默认同时写 XMP 与 ISO；主图存根、增益图完整段。"""
    src = tmp_path / "light.png"
    img = np.full((64, 64, 3), 20, np.uint8)
    img[20:44, 20:44] = 240
    Image.fromarray(img, "RGB").save(src)

    res = build(src, tmp_path / "out", formats=("jpeg",))
    assert res["ok"]
    path = tmp_path / "out" / "light_ultrahdr.jpeg"
    blob = path.read_bytes()
    info = ultrahdr.parse(blob)

    primary_iso = info["iso_primary"]
    assert primary_iso is not None
    assert primary_iso[len(ultrahdr.ISO_NS):] == b"\x00\x00\x00\x00"   # 版本存根

    gain_iso = validate.parse_iso(info["iso_gainmap"])
    assert gain_iso is not None and not gain_iso["stub"]
    assert gain_iso["channels"] == 3
    assert gain_iso["alt_headroom"] == pytest.approx(info["gmax"])
    for i, ch in enumerate(res["gmax_channels"]):
        assert gain_iso[f"max{i}"] == pytest.approx(ch)

    rep = validate.validate_file(path)
    assert rep.ok, rep.errors


def test_png_parallel_filter_matches_serial() -> None:
    """并行滤波按块独立，字节流必须与逐行串行完全一致。"""
    from hdrgainmap import png16

    rng = np.random.default_rng(7)
    arr = rng.integers(0, 65536, (160, 220, 3), dtype=np.uint16)
    raw = arr.astype(">u2").tobytes()
    assert png16._filter(raw, 220, 160, 1) == png16._filter(raw, 220, 160, 4)


def test_parallel_build_matches_serial(tmp_path: Path) -> None:
    """多路并行编码与串行结果逐字节一致。"""
    src = tmp_path / "s.png"
    img = np.full((96, 128, 3), 18, np.uint8)
    img[30:70, 40:96] = 240
    Image.fromarray(img, "RGB").save(src)

    a = build(src, tmp_path / "a", formats=("jpeg", "png", "heic10"), jobs=1)
    b = build(src, tmp_path / "b", formats=("jpeg", "png", "heic10"), jobs=3)
    assert a["ok"] and b["ok"], (a["errors"], b["errors"])
    for key in ("jpeg", "png", "heic10"):
        assert Path(a["files"][key]).read_bytes() == Path(b["files"][key]).read_bytes(), key


def test_gpu_matches_cpu() -> None:
    """GPU 查表与 CPU 查表：码值最多差 1 个 16bit LSB。"""
    from hdrgainmap import gpu

    if not gpu.available():
        pytest.skip("没有可用的 WebGPU 适配器")

    rng = np.random.default_rng(11)
    rgb = (rng.random((72, 96, 3), dtype=np.float32) ** 2).astype(np.float32)
    emis = (rng.random((72, 96), dtype=np.float32) ** 2).astype(np.float32)
    weights = np.array([1.02, 0.98, 1.05], np.float32)
    ref, ref_peak = _hdr_code(rgb, emis, 1.2, weights, use_gpu=False)
    got, peak = _hdr_code(rgb, emis, 1.2, weights, use_gpu=True)
    diff = np.abs(got.astype(np.int32) - ref.astype(np.int32))
    assert diff.max() <= 1
    assert float((diff > 0).mean()) < 0.01
    assert peak == pytest.approx(ref_peak, rel=1e-6)
    log2_ref = _channel_terms(rgb, emis, 1.2, weights)[1]
    log2_got, _ = gpu.expo(rgb, emis, 1.2, weights)
    assert np.abs(log2_ref - log2_got).max() < 1e-5


def test_lut_matches_exact_math() -> None:
    """查表近似：sRGB 解码、exp2、tint、PQ 都要贴在解析值上。"""
    from hdrgainmap import lut

    v = np.linspace(0.0, 1.0, 4001, dtype=np.float32)
    exact = np.where(v <= 0.04045, v / 12.92,
                     ((v + 0.055) / 1.055) ** 2.4).astype(np.float32)
    assert np.abs(lut.srgb_to_linear(v) - exact).max() < 1e-6

    e = np.linspace(0.0, 2.0, 4001, dtype=np.float32)
    assert np.abs(lut.exp2(e) - np.exp2(e)).max() < 1e-4

    r = np.linspace(0.0, 2.0, 4001, dtype=np.float32)
    want = np.clip(np.maximum(r, 0.0) ** 0.35, 0.85, 1.15)
    assert np.abs(lut.tint(r) - want).max() < 2e-3

    nits = np.linspace(0.0, 1000.0, 4001, dtype=np.float64)
    y = np.clip(nits / 10000.0, 0, 1) ** (2610 / 16384)
    ref = np.clip(((3424 / 4096 + (2413 / 4096 * 32) * y)
                   / (1 + (2392 / 4096 * 32) * y)) ** (2523 / 4096 * 128), 0, 1)
    got = lut.pq(nits.astype(np.float32)).astype(np.float64)
    assert np.abs(got - ref).max() * 65535 < 0.05


def test_gpu_field_matches_cpu() -> None:
    """GPU 光场：模糊、导向滤波与五层判据都要和 CPU 版逐点对齐。"""
    from hdrgainmap import gainmap, gpufield, light

    if not gpufield.available():
        pytest.skip("没有可用的 WebGPU 适配器")

    rng = np.random.default_rng(5)
    y = (rng.random((150, 210), np.float32) ** 2).astype(np.float32)
    for r in (1, 4, 15):
        assert np.abs(gainmap._gauss(y, r) - gpufield.gauss(y, r)).max() < 2e-5

    ref = light.guided_filter(y, gainmap._gauss(y, 8), 6)
    got = gpufield.guided(y, gpufield.gauss(y, 8), 6)
    assert np.abs(ref - got).max() < 5e-5

    rgb = np.repeat(y[..., None], 3, axis=2)
    scene = light.analyze(rgb, y)
    cfg = gainmap.adapt_params(rgb, y, scene)
    _, ref_emis = gainmap.emissive(rgb, cfg, y, scene)
    ref_fin = gainmap.finalize(ref_emis)
    _, gpu_emis = gpufield.emissive(y, cfg, scene.irradiance, scene.ao)
    gpu_fin = gpufield.finalize(gpu_emis)
    assert np.abs(ref_emis - gpu_emis).max() < 2e-3
    assert np.abs(ref_fin - gpu_fin).max() < 5e-4

    g1 = gainmap.gain_of(ref_fin, cfg["gmax"])
    g2 = gainmap.gain_of(gpu_fin, cfg["gmax"])
    mx = max(float(g1.max()), float(g2.max()))
    c1 = np.log2(np.maximum(g1, 1e-6)) / np.log2(mx)
    c2 = np.log2(np.maximum(g2, 1e-6)) / np.log2(mx)
    assert np.abs(np.floor(np.clip(c1, 0, 1) * 255)
                  - np.floor(np.clip(c2, 0, 1) * 255)).max() <= 1
