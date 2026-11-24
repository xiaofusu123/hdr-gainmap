"""一条龙：源图 → 自适应增益 → 各格式的 HDR 母版。

输出格式（不分档位，全部按图自适应）：
  jpeg    Ultra HDR JPEG（主图 + 增益图 + MPF + XMP + ISO 21496-1），安卓原生
  png     16bit PQ/HLG，无损母版
  heic10  10bit HEVC Main10，兼容性最好
  heic12  12bit HEVC Main12，色深最高
  avif    12bit AV1，体积最小

所有编码器默认质量 100；颜色统一走 BT.2020 + PQ（或 HLG），
HEIC/AVIF 写 nclx 9/16(PQ) 或 18(HLG)/9，PNG 写 cICP 9/16/0/1。
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image

from . import color, gainmap, heic10, png16, ultrahdr

FORMATS = ("jpeg", "png", "heic10", "heic12", "avif")
QUALITY = 100
HLG_PEAK_NITS = 1000.0
JPEG_SUFFIXES = {".jpg", ".jpeg", ".jpe", ".jfif"}

_SRGB_ICC: bytes | None = None


def _srgb_icc() -> bytes | None:
    """规范要求 Ultra HDR 主图带 ICC；源图没有时补一份 sRGB（lcms 由 Pillow 自带）。"""
    global _SRGB_ICC
    if _SRGB_ICC is None:
        try:
            from PIL import ImageCms
            _SRGB_ICC = ImageCms.ImageCmsProfile(ImageCms.createProfile("sRGB")).tobytes()
        except Exception:                        # noqa: BLE001  Pillow 精简版可能没 lcms
            _SRGB_ICC = b""
    return _SRGB_ICC or None


def _prepare_base(src: Path, im: Image.Image, dpi: float,
                  quality: int) -> tuple[bytes, bool]:
    """返回 (8bit JPEG 字节, 是否复用了源文件字节)。

    源本来就是 JPEG 且 DPI 没变时直接复用原始字节，SDR 部分逐字节不变；
    否则用给定质量重编码（subsampling=0，避免色边被糊）。
    """
    if src.suffix.lower() in JPEG_SUFFIXES:
        try:
            with Image.open(src) as probe:
                same_dpi = abs(_source_dpi(probe) - dpi) < 0.5
            if same_dpi:
                return src.read_bytes(), True
        except OSError:
            pass
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, subsampling=0, optimize=True, dpi=(dpi, dpi))
    return buf.getvalue(), False


def _source_dpi(im: Image.Image) -> float:
    """取源图 DPI（缺省 96），让输出属性面板与原图一致。"""
    dpi = im.info.get("dpi")
    if isinstance(dpi, tuple) and dpi[0]:
        return float(dpi[0])
    return 96.0


def _hdr_code(hdr_linear: np.ndarray, transfer: str) -> np.ndarray:
    """线性 BT.709 光（SDR 白=203nits 基准）→ BT.2020 + PQ/HLG 的 uint16 码值。

    PQ 用绝对亮度（×203 nits）；HLG 是相对信号，1.0 对应 1000 nits 峰值，
    SDR 白落在 ~0.75，所以要先除以峰值再编码。
    """
    hdr2020 = color.to_bt2020(hdr_linear)
    if transfer == "HLG":
        code = color.hlg_oetf(hdr2020, HLG_PEAK_NITS / heic10.SDR_WHITE_NITS)
    else:
        code = color.pq_oetf(hdr2020 * heic10.SDR_WHITE_NITS)
    return np.clip(np.round(code * 65535.0), 0, 65535).astype(np.uint16)


def build(src: str | Path, outdir: str | Path, formats: tuple[str, ...] = FORMATS,
          quality: int = QUALITY, dpi: float | None = None,
          transfer: str = "PQ", debug: bool = False,
          lift: float | None = None, peak_scale: float | None = None,
          embed_icc: bool = True, gain_fullres: bool = False,
          gain_rgb: bool = False, gain_scale: float = 1.0,
          iso_flags: int = 0x40, write_iso: bool = False,
          invert_gain: bool = False) -> dict:
    """按格式生成 HDR 母版，返回产物路径与统计。

    lift / peak_scale 用于试调：只覆盖其中一个时，就是"只整体抬"或"只压峰值"。
    """
    src = Path(src)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    formats = tuple(formats)

    rgb, im = gainmap.load_rgb(str(src))
    if dpi is None:
        with Image.open(src) as probe:          # convert() 会丢 info，得在原始对象上读
            dpi = _source_dpi(probe)
    # pHYs 存 px/m，换算回 dpi 常带小数尾巴（9449 ppm → 239.98），面板会显示 239
    dpi = float(round(float(dpi), 1))

    # 1) 按图自适应参数 → 发光权重 → 线性增益
    cfg = gainmap.adapt_params(rgb)
    if lift is not None:
        cfg["lift"] = float(lift)
    if peak_scale is not None:
        # gmax 已按 PEAK_SCALE 压过；这里还原后按调用方给的比例重新压
        cfg["gmax"] = round(cfg["gmax"] / gainmap.PEAK_SCALE * float(peak_scale), 3)
    _, emis = gainmap.emissive(rgb, cfg)
    emis = gainmap.finalize(emis)
    gain = gainmap.gain_of(emis, cfg["gmax"], cfg["lift"])

    # 2) 重建 HDR 线性光。增益图不落盘参与，直接用全分辨率权重，
    #    所以 PNG/HEIC/AVIF 三份产物逐像素来自同一份数据。
    hdr_linear = color.srgb_to_linear(rgb) * gain[..., None]
    q16 = _hdr_code(hdr_linear, transfer)

    result: dict = {
        "src": str(src),
        "dpi": dpi,
        "transfer": transfer,
        "gmax": cfg["gmax"],
        "files": {},
        "stats": {
            "gain_mean": float(gain.mean()),
            "gain_max": float(gain.max()),
            "untouched_pct": float(100 * (gain < 1.02).mean()),
            "gt2x_pct": float(100 * (gain > 2.0).mean()),
            "peak_nits": float(hdr_linear.max() * heic10.SDR_WHITE_NITS),
        },
    }

    # 3) 各格式落盘
    stem = src.stem
    if debug:
        Image.fromarray((emis * 255.0).astype(np.uint8)).save(outdir / f"{stem}_gainmap.png")

    if "jpeg" in formats:
        # Ultra HDR：主图 + 半分辨率灰度增益图；源是 JPEG 且 DPI 不变时复用原始字节
        base_jpeg, reused = _prepare_base(src, im, dpi, quality)
        result["stats"]["base_reused"] = bool(reused)
        h, w = emis.shape
        # 增益图要表达的是"最终增益"（含整体抬升与高光压缩），不是发光权重：
        # ISO 解码端按 gain = 2^(GainMapMax * code) 还原，这里把 target gain 反解成码值，
        # 并按实际最大增益写元数据，保证手机与 PNG/HEIC/AVIF 走同一条曲线。
        gmax_meta = float(np.log2(max(float(gain.max()), 1.0)))
        code = np.clip(np.log2(np.maximum(gain, 1e-6)) / max(gmax_meta, 1e-6), 0.0, 1.0)
        # 8bit 增益图 + 大面积平滑渐变会出现等值线状"光圈"（马赫带），
        # 量化前加 ±0.5 LSB 的三角分布抖动把它打散，肉眼不可见但能消除环。
        dither = np.random.default_rng(20261125).random(code.shape, dtype=np.float32) \
            - np.random.default_rng(7).random(code.shape, dtype=np.float32)
        gm_px = np.clip(code * 255.0 * float(gain_scale) + dither, 0, 254.0)
        if invert_gain:
            gm_px = 255.0 - gm_px
        gm = Image.fromarray(gm_px.astype(np.uint8))
        if not gain_fullres:
            gm = gm.resize((max(1, w // 2), max(1, h // 2)), Image.LANCZOS)
        if gain_rgb:
            gm = gm.convert("RGB")
        gm_buf = io.BytesIO()
        gm.save(gm_buf, "JPEG", quality=quality, optimize=True)
        result["gmax_meta"] = gmax_meta
        blob = ultrahdr.build(base_jpeg, gm_buf.getvalue(), gmax=gmax_meta,
                              dpi=dpi, icc=_srgb_icc() if embed_icc else None,
                              iso_flags=iso_flags, write_iso=write_iso)
        path = outdir / f"{stem}_ultrahdr.jpeg"
        path.write_bytes(blob)
        result["files"]["jpeg"] = str(path)

    if "png" in formats:
        path = outdir / f"{stem}_hdr16_{transfer.lower()}.png"
        path.write_bytes(png16.encode(q16, dpi=dpi))
        result["files"]["png"] = str(path)

    for key, bits in (("heic10", 10), ("heic12", 12)):
        if key in formats:
            path = outdir / f"{stem}_hdr{bits}_{transfer.lower()}.heic"
            path.write_bytes(heic10.encode(q16, transfer=transfer, bits=bits,
                                           quality=quality, dpi=dpi))
            result["files"][key] = str(path)

    if "avif" in formats:
        from . import avif as avifmod
        blob, bits = avifmod.encode(q16, transfer=transfer, bits=12,
                                    quality=quality, dpi=dpi)
        path = outdir / f"{stem}_hdr{bits}_{transfer.lower()}.avif"
        path.write_bytes(blob)
        result["files"]["avif"] = str(path)
        result["stats"]["avif_bits"] = bits

    return result
