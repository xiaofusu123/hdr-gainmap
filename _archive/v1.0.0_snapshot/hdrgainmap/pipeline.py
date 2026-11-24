"""一条龙：源图 → 增益图 → Ultra HDR JPEG / 16bit PNG / 10bit HEIC + SDR 模拟预览。"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image

from . import color, gainmap, heic10, png16, ultrahdr

BASE_JPEG_QUALITY = 95
GAIN_JPEG_QUALITY = 92


def _source_dpi(im: Image.Image) -> float:
    """取源图 DPI（缺省 96），用于让输出属性面板与原图一致。"""
    dpi = im.info.get("dpi")
    if isinstance(dpi, tuple) and dpi[0]:
        return float(dpi[0])
    return 96.0


def build(src: str | Path, outdir: str | Path, profile: str = "strong",
          formats: tuple[str, ...] = ("jpg", "png", "heic"), dpi: float | None = None,
          gmax: float | None = None, transfer: str = "PQ", preview: bool = True,
          debug: bool = False) -> dict:
    """生成所有格式，返回产物路径与统计。"""
    src = Path(src)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    formats = tuple("jpg" if f == "jpeg" else f for f in formats)   # jpeg 只是 .jpg 的别名

    rgb, im = gainmap.load_rgb(str(src))
    if dpi is None:
        with Image.open(src) as probe:          # convert() 会丢 info，得在原始对象上读
            dpi = _source_dpi(probe)
    dpi = float(dpi)

    # 0) 只换容器：不碰增益图，原样 8bit sRGB → HEIC（高调素材走这条）
    if set(formats) == {"heic_sdr"}:
        outdir.mkdir(parents=True, exist_ok=True)
        path = outdir / f"{src.stem}_sdr8.heic"
        path.write_bytes(heic10.encode_sdr(im, dpi=dpi))
        return {"src": str(src), "profile": "none", "dpi": dpi, "gmax": 0.0,
                "files": {"heic_sdr": str(path)}, "stats": {"sdr_only": 1.0}}

    cfg = gainmap.fit_width(dict(gainmap.PROFILES[profile]), im.width)
    if gmax:
        cfg["gmax"] = float(gmax)

    # 1) 发光体权重与线性增益
    _, emis = gainmap.emissive(rgb, cfg)
    emis = gainmap.finalize(emis)
    gain = gainmap.gain_of(emis, cfg["gmax"])

    result: dict = {"src": str(src), "profile": profile, "dpi": dpi,
                    "gmax": cfg["gmax"], "files": {},
                    "stats": {"gain_mean": float(gain.mean()),
                              "gain_max": float(gain.max()),
                              "untouched_pct": float(100 * (gain < 1.02).mean()),
                              "gt2x_pct": float(100 * (gain > 2.0).mean())}}

    # 2) SDR 主图（不重编码原图时用；这里源图可能是 PNG，需先落成 JPEG）
    base_buf = io.BytesIO()
    im.save(base_buf, "JPEG", quality=BASE_JPEG_QUALITY, subsampling=0,
            optimize=True, dpi=(dpi, dpi))
    base_jpeg = base_buf.getvalue()
    if debug:
        (outdir / f"{src.stem}_base.jpg").write_bytes(base_jpeg)

    # 3) 增益图 JPEG：半分辨率灰度
    h, w = emis.shape
    gm_img = Image.fromarray((emis * 255.0).astype(np.uint8)).resize((w // 2, h // 2), Image.LANCZOS)
    gm_buf = io.BytesIO()
    gm_img.save(gm_buf, "JPEG", quality=GAIN_JPEG_QUALITY, optimize=True)
    gain_jpeg = gm_buf.getvalue()
    if debug:
        gm_img.save(outdir / f"{src.stem}_gainmap.png")       # 增益图可视化（调试用）

    # 4) Ultra HDR JPEG
    if "jpg" in formats:
        blob = ultrahdr.build(base_jpeg, gain_jpeg, gmax=cfg["gmax"])
        path = outdir / f"{src.stem}_ultrahdr.jpeg"
        path.write_bytes(blob)
        result["files"]["jpg"] = str(path)
        result["stats"]["jpg_bytes"] = len(blob)

    # 5) HDR 线性光：按"解码器看到的那张增益图"重建，保证三种格式逐像素一致
    #    （8bit 量化 + 双三次放大后才是真实落盘的信息）
    decoded_gm = Image.open(io.BytesIO(gain_jpeg)).resize(im.size, Image.BICUBIC)
    g = np.asarray(decoded_gm, dtype=np.float32) / 255.0
    gain_used = (2.0 ** (cfg["gmax"] * g)).astype(np.float32)
    hdr_linear = color.srgb_to_linear(rgb) * gain_used[..., None]
    peak_nits = float(hdr_linear.max() * heic10.SDR_WHITE_NITS)
    result["stats"]["peak_nits"] = peak_nits
    hdr2020 = color.to_bt2020(hdr_linear)

    if "png" in formats or "heic" in formats:
        code = (color.hlg_oetf(hdr2020, heic10.SDR_WHITE_NITS / 1.0) if transfer == "HLG"
                else color.pq_oetf(hdr2020 * heic10.SDR_WHITE_NITS))
        q16 = np.clip(np.round(code * 65535.0), 0, 65535).astype(np.uint16)

        if "png" in formats:
            path = outdir / f"{src.stem}_hdr16.png"
            path.write_bytes(png16.encode(q16, dpi=dpi))
            result["files"]["png"] = str(path)

        if "heic" in formats:
            path = outdir / f"{src.stem}_hdr10_{transfer.lower()}.heic"
            path.write_bytes(heic10.encode(q16, transfer=transfer, bits=10, dpi=dpi))
            result["files"]["heic"] = str(path)

    # 5b) 纯换容器的 8bit SDR HEIC（高调素材用这条，不做 HDR 编码）
    if "heic_sdr" in formats:
        path = outdir / f"{src.stem}_sdr8.heic"
        path.write_bytes(heic10.encode_sdr(im, dpi=dpi))
        result["files"]["heic_sdr"] = str(path)

    # 6) SDR 模拟预览 + 增益数值分布
    if preview:
        prev = gainmap.render_preview(rgb, gain)
        path = outdir / f"{src.stem}_hdrsim.jpg"
        Image.fromarray(prev).save(path, quality=95)
        result["files"]["preview"] = str(path)
        diff = np.abs(prev.astype(np.int16) - (rgb * 255).astype(np.int16)).max(axis=2)
        result["stats"]["changed_gt2_pct"] = float(100 * (diff > 2).mean())
        result["stats"]["changed_gt16_pct"] = float(100 * (diff > 16).mean())

    return result
