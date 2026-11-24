"""自测：合成一张小图，跑通四种格式并做往返与合规校验（不需要外部素材）。

用法：python tests/test_roundtrip.py
"""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import numpy as np
from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from hdrgainmap import avif, color, gainmap, heic10, png16, ultrahdr, validate  # noqa: E402
from hdrgainmap.pipeline import build                                   # noqa: E402


def make_source(path: Path, bright: bool = False) -> None:
    """256x128 暗底 + 一个高光点 + 一点渐变，模拟"平涂 + 发光体"。"""
    w, h = 256, 128
    yy, xx = np.mgrid[0:h, 0:w]
    base = 0.10 + 0.25 * (xx / w)
    img = np.dstack([base * 0.6, base * 0.8, base * 1.0])
    glow = np.exp(-(((xx - 190) ** 2 + (yy - 40) ** 2) / (2 * 9.0 ** 2)))
    img += glow[..., None] * np.array([1.0, 0.95, 0.8])
    if bright:
        img[:, w // 3:] = 0.98
    Image.fromarray((np.clip(img, 0, 1) * 255).astype(np.uint8)).save(path)


def check_pq_roundtrip() -> None:
    """PQ 编解码必须互为逆运算（曾因漏掉 1/m2 次幂导致整幅变黑）。"""
    v = np.array([1.0, 10.0, 100.0, 203.0, 1000.0, 4000.0], dtype=np.float32)
    r = color.pq_eotf(color.pq_oetf(v))
    assert np.allclose(r, v, rtol=1e-4), (v, r)


def check_adaptive_params() -> None:
    """自适应：阈值跟着这张图的亮度分位走，不是固定常数。"""
    dark = np.zeros((64, 64, 3), np.float32)
    dark[20:40, 20:40] = 0.98                    # 暗底 + 一小块高光
    bright = np.full((64, 64, 3), 0.85, np.float32)   # 整片都亮
    a = gainmap.adapt_params(dark)
    b = gainmap.adapt_params(bright)
    assert a["gmax"] > b["gmax"] > 1.0, (a["gmax"], b["gmax"])
    assert 1 <= a["hr"] <= 3 and 1 <= b["hr"] <= 3, (a["hr"], b["hr"])
    assert a["hr"] >= b["hr"], (a["hr"], b["hr"])        # 暗场景泛光半径更大
    # 门槛跟着这张图自己的 p99 走：b0 = clip(p99 - 0.05)
    for rgb, cfg in ((dark, a), (bright, b)):
        p99 = float(np.percentile(gainmap.luma(rgb), 99))
        want = round(min(0.95, max(0.55, p99 - 0.05)), 3)
        assert abs(cfg["b0"] - want) < 1e-6, (p99, cfg["b0"])
        assert cfg["b1"] > cfg["b0"]


def main() -> int:
    check_pq_roundtrip()
    check_adaptive_params()

    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        src = tmp / "src.png"
        make_source(src)

        res = build(src, tmp / "out")
        files = res["files"]
        assert set(files) == {"jpeg", "png", "heic10", "heic12", "avif"}, files

        for kind, path in files.items():
            rep = validate.validate_file(path)
            assert rep.ok, f"{kind}\n" + rep.format()

        # Ultra HDR：MPF 两条 + 主图 XMP + 增益图 XMP/ISO
        jblob = Path(files["jpeg"]).read_bytes()
        jrep = validate.validate_file(files["jpeg"])
        assert jrep.kind == "ultrahdr", jrep.format()
        info = ultrahdr.parse(jblob)
        assert len(info["entries"]) == 2, info
        assert info["entries"][0]["attr"] & 0x0FFFFFFF == 0x030000
        assert info["entries"][1]["size"] == info["gain_len"]
        assert jblob[info["gain_offset"]:info["gain_offset"] + 2] == b"\xff\xd8"
        assert info["hdrgm"]["Version"] == "1.0", info["hdrgm"]
        assert jblob.find(b"MPF\x00") < jblob.find(b"\xff\xda"), "元数据被插进了扫描数据"

        # PNG：16bit、cICP 9/16/0/1，两条解码路径逐像素一致
        png = Path(files["png"]).read_bytes()
        slow = png16.decode(png, fast=False)
        fast = png16.decode(png, fast=True)
        assert slow.dtype == np.dtype(">u2") and slow.shape[2] == 3, slow.shape
        assert np.array_equal(slow, fast), "ffmpeg 与 NumPy 解码结果不一致"

        # HEIC 10 / 12：位深、BT.2020 + PQ、矩阵系数必须是 9
        for kind, bits in (("heic10", 10), ("heic12", 12)):
            arr, meta = heic10.decode(Path(files[kind]).read_bytes())
            nclx = {k: getattr(v, "value", v) for k, v in meta["nclx"].items()}
            assert meta["bit_depth"] == bits, meta
            assert nclx["transfer_characteristics"] == 16, nclx
            assert nclx["color_primaries"] == 9, nclx
            assert nclx["matrix_coefficients"] == 9, nclx
            assert arr.max() > 0

        # AVIF：12bit 4:4:4 + BT.2020/PQ（ffmpeg 不可用时跳过）
        if avif.available():
            ab = Path(files["avif"]).read_bytes()
            nclx = avif.read_nclx(ab)
            assert nclx["primaries"] == 9 and nclx["transfer"] == 16 and nclx["matrix"] == 9, nclx
            assert max(avif.pixi_bits(ab)) == 12, avif.pixi_bits(ab)
            assert avif.decode(ab).shape[:2] == (128, 256)
            print("AVIF 12bit OK", len(ab), "B")

        # 增益只落在光点上
        assert 0.5 < res["stats"]["untouched_pct"] <= 100.0, res["stats"]
        assert res["stats"]["peak_nits"] > 300, res["stats"]

        # 质量参数可控：q60 的体积应明显小于 q100
        small = build(src, tmp / "q60", formats=("heic10",), quality=60)
        big = build(src, tmp / "q100", formats=("heic10",), quality=100)
        assert (Path(small["files"]["heic10"]).stat().st_size
                < Path(big["files"]["heic10"]).stat().st_size)

        print("OK", {Path(v).name: Path(v).stat().st_size for v in files.values()})
        print("stats:", res["stats"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
