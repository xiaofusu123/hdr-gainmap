"""一条龙：源图 → 自适应增益 → 各格式的 HDR 母版。

输出格式（不分档位，全部按图自适应）：
  jpeg    Ultra HDR JPEG（主图 + 增益图 + MPF + XMP + ISO 21496-1），安卓原生
  png     16bit PQ/HLG，无损母版
  heic10  10bit HEVC Main10，兼容性最好
  heic12  12bit HEVC Main12，色深最高
  avif    12bit AV1，体积最小

所有编码器默认质量 100；颜色统一走 BT.2020 + PQ（或 HLG），
HEIC/AVIF 写 nclx 9/16(PQ) 或 18(HLG)/9，PNG 写 cICP 9/16/0/1。

大图内存策略：BT.2020 + PQ/HLG 编码按行分块，增益图在降采样后的尺寸上
量化并抖动，避免为亿像素图同时保留多份 float32 全分辨率副本。
"""
from __future__ import annotations

import io
import os
import tempfile
import time
from pathlib import Path

import numpy as np
from PIL import Image

from . import color, gainmap, heic10, png16, ultrahdr

FORMATS = ("jpeg", "png", "heic10", "heic12", "avif")
QUALITY = 100
HLG_PEAK_NITS = 1000.0
JPEG_SUFFIXES = {".jpg", ".jpeg", ".jpe", ".jfif"}
SDR_WHITE_NITS = heic10.SDR_WHITE_NITS

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


def _write_atomic(path: Path, blob: bytes) -> None:
    """先写同目录临时文件再原子替换，避免中断留下半截产物。"""
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(blob)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def _prepare_base(src: Path, im: Image.Image, dpi: float, quality: int,
                  reusable: bool) -> tuple[bytes, bool]:
    """返回 (8bit JPEG 字节, 是否复用了源文件字节)。

    只有源本来就是 JPEG、且像素没被方向/alpha/ICC 改动过、DPI 也没变时才复用
    原始字节，SDR 部分逐字节不变；否则用给定质量重编码（subsampling=0，避免色边被糊）。
    """
    if reusable and src.suffix.lower() in JPEG_SUFFIXES:
        try:
            with Image.open(src) as probe:
                if abs(_source_dpi(probe) - dpi) < 0.5:
                    return src.read_bytes(), True
        except OSError:
            pass
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality, subsampling=0, optimize=True, dpi=(dpi, dpi))
    return buf.getvalue(), False


def _source_dpi(im: Image.Image) -> float:
    """取源图 DPI（缺省 96），让输出属性面板与原图一致。"""
    dpi = im.info.get("dpi")
    if isinstance(dpi, (tuple, list)) and dpi and dpi[0]:
        try:
            value = float(dpi[0])
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return 96.0


def _hdr_code(rgb: np.ndarray, gain: np.ndarray, transfer: str,
              *, tile_rows: int = 2048) -> tuple[np.ndarray, float]:
    """线性 BT.709 光（SDR 白=203nits 基准）→ BT.2020 + PQ/HLG 的 uint16 码值。

    PQ 用绝对亮度（×203 nits）；HLG 是相对信号，1.0 对应 1000 nits 峰值，
    SDR 白落在 ~0.75，所以要先除以峰值再编码。
    返回 (码值, 峰值 nits)；按行分块，峰值内存只与 tile_rows 有关。
    """
    h, w, _ = rgb.shape
    out = np.empty((h, w, 3), dtype=np.uint16)
    peak = 0.0
    for y0 in range(0, h, tile_rows):
        y1 = min(h, y0 + tile_rows)
        lin = color.srgb_to_linear(rgb[y0:y1]) * gain[y0:y1, :, None]
        peak = max(peak, float(lin.max()))
        hdr2020 = color.to_bt2020(lin)
        if transfer == "HLG":
            code = color.hlg_oetf(hdr2020, HLG_PEAK_NITS / SDR_WHITE_NITS)
        else:
            code = color.pq_oetf(hdr2020 * SDR_WHITE_NITS)
        out[y0:y1] = np.clip(np.round(code * 65535.0), 0, 65535).astype(np.uint16)
    return out, peak * SDR_WHITE_NITS


def _save_gain_debug(gain: np.ndarray, path: Path, gmax_meta: float,
                     *, tile_rows: int = 2048) -> None:
    """把全分辨率增益写成 8bit 可视化 PNG（log2 域，0..255 对应 0..gmax_meta）。

    分块处理，避免为调试图再分配一份全尺寸 float32；码值与 JPEG 内嵌增益图
    采用同一套 log2 归一化，可以直接对照，区别只是这里不降采样、不加抖动。
    """
    h, w = gain.shape
    out = np.empty((h, w), dtype=np.uint8)
    denom = max(float(gmax_meta), 1e-6)
    for y0 in range(0, h, tile_rows):
        y1 = min(h, y0 + tile_rows)
        code = np.log2(np.maximum(gain[y0:y1], 1e-6)) / denom
        out[y0:y1] = np.round(np.clip(code, 0.0, 1.0) * 255.0).astype(np.uint8)
    Image.fromarray(out).save(path)


def build(src: str | Path, outdir: str | Path, formats: tuple[str, ...] = FORMATS,
          quality: int = QUALITY, dpi: float | None = None,
          transfer: str = "PQ", debug: bool = False,
          lift: float | None = None, peak_scale: float | None = None,
          embed_icc: bool = True, gain_fullres: bool = False,
          gain_rgb: bool = False, gain_scale: float = 1.0,
          iso_flags: int = 0x40,
          invert_gain: bool = False, alpha_bg: tuple[int, int, int] = (0, 0, 0),
          tile_rows: int = 2048, log=None) -> dict:
    """按格式生成 HDR 母版，返回产物路径与统计。

    lift / peak_scale 用于试调：只覆盖其中一个时，就是"只整体抬"或"只压峰值"。
    alpha_bg 是透明图合成时的底色；log 是可选的进度回调（接收一行文本）。
    Ultra HDR 按 1.1 同时写 XMP 与 ISO 21496-1；单个格式失败不会中断其它格式，
    错误收在 result["errors"] 里。
    """
    t_start = time.perf_counter()
    timings: dict[str, float] = {}

    def mark(name: str, t0: float) -> float:
        timings[name] = round(time.perf_counter() - t0, 2)
        return time.perf_counter()

    def notify(msg: str) -> None:
        if log is not None:
            log(msg)

    src = Path(src)
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    formats = tuple(formats)
    unknown = set(formats) - set(FORMATS)
    if unknown:
        raise ValueError(f"未知格式：{', '.join(sorted(unknown))}；可选 {', '.join(FORMATS)}")
    if not 1 <= int(quality) <= 100:
        raise ValueError(f"quality 必须在 1–100，收到 {quality}")
    transfer = transfer.upper()
    if transfer not in ("PQ", "HLG"):
        raise ValueError(f"transfer 必须是 PQ 或 HLG，收到 {transfer}")
    if dpi is not None and float(dpi) <= 0:
        raise ValueError(f"dpi 必须为正数，收到 {dpi}")

    t0 = time.perf_counter()
    notify(f"读取 {src.name} …")
    rgb, im, meta = gainmap.prepare_image(src, alpha_bg=alpha_bg)
    if dpi is None:
        dpi = meta["dpi"]
    # pHYs 存 px/m，换算回 dpi 常带小数尾巴（9449 ppm → 239.98），面板会显示 239
    dpi = float(round(float(dpi), 1))
    t0 = mark("load", t0)

    # 先做 SDR 主图（Ultra HDR 需要），之后就能释放 PIL 图
    base_jpeg: bytes | None = None
    base_reused = False
    if "jpeg" in formats:
        notify("准备 SDR 主图 …")
        base_jpeg, base_reused = _prepare_base(src, im, dpi, quality,
                                               meta["reusable_source"])
        t0 = mark("base", t0)
    del im

    # 1) 按图自适应参数 → 发光权重 → 线性增益
    notify("分析光影分布 …")
    y = gainmap.luma(rgb)
    cfg = gainmap.adapt_params(rgb, y=y)
    if lift is not None:
        cfg["lift"] = float(lift)
    if peak_scale is not None:
        # gmax 已按 PEAK_SCALE 压过；这里还原后按调用方给的比例重新压
        cfg["gmax"] = round(cfg["gmax"] / gainmap.PEAK_SCALE * float(peak_scale), 3)
    _, emis = gainmap.emissive(rgb, cfg, y=y)
    del y
    emis = gainmap.finalize(emis)
    gain = gainmap.gain_of(emis, cfg["gmax"], cfg["lift"])
    t0 = mark("gain", t0)

    # 2) 重建 HDR 线性光并编码。增益图不落盘参与，PNG/HEIC/AVIF 三份产物
    #    逐像素来自同一份 q16。
    notify("编码 BT.2020 + " + transfer + " …")
    q16, peak_nits = _hdr_code(rgb, gain, transfer, tile_rows=tile_rows)
    t0 = mark("hdr", t0)

    result: dict = {
        "src": str(src),
        "dpi": dpi,
        "transfer": transfer,
        "gmax": cfg["gmax"],
        "files": {},
        "errors": {},
        "stats": {
            "gain_mean": float(gain.mean()),
            "gain_max": float(gain.max()),
            "untouched_pct": float(100 * (gain < 1.02).mean()),
            "gt2x_pct": float(100 * (gain > 2.0).mean()),
            "peak_nits": float(peak_nits),
        },
        "timings": timings,
    }

    # 3) 各格式落盘；某个编码器失败只记录，不影响其它格式
    stem = src.stem
    if debug:
        # 发光权重是中间场，最终增益才是编码进增益图的数据，两者分开落盘。
        Image.fromarray((emis * 255.0).astype(np.uint8)).save(outdir / f"{stem}_emis.png")
        gmax_debug = float(np.log2(max(float(gain.max()), 1.0)))
        _save_gain_debug(gain, outdir / f"{stem}_gainmap.png", gmax_debug)
    del rgb

    def fail(key: str, exc: Exception) -> None:
        result["errors"][key] = f"{type(exc).__name__}: {exc}"
        notify(f"{key} 失败：{exc}")

    if "jpeg" in formats:
        try:
            notify("编码 Ultra HDR JPEG …")
            # 增益图要表达的是"最终增益"（含整体抬升与高光压缩），不是发光权重：
            # ISO 解码端按 gain = 2^(GainMapMax * code) 还原，这里把 target gain 反解成码值，
            # 并按实际最大增益写元数据，保证手机与 PNG/HEIC/AVIF 走同一条曲线。
            gmax_meta = float(np.log2(max(float(gain.max()), 1.0)))
            h, w = gain.shape
            # 质量优先：先在 log 域按全分辨率取码，再用 LANCZOS 降到半分辨率。
            # 如果先对增益做双线性降采样，高光边缘的码值会被插值磨平，半分辨率增益图会发糊。
            code = np.clip(np.log2(np.maximum(gain, 1e-6)) / max(gmax_meta, 1e-6), 0.0, 1.0)
            if not gain_fullres:
                code = np.asarray(
                    Image.fromarray(code, mode="F").resize(
                        (max(1, w // 2), max(1, h // 2)), Image.LANCZOS),
                    dtype=np.float32)
            # 8bit 增益图 + 大面积平滑渐变会出现等值线状"光圈"（马赫带），
            # 量化前加 ±0.5 LSB 的三角分布抖动把它打散，肉眼不可见但能消除环。
            rng_a = np.random.default_rng(20261125)
            rng_b = np.random.default_rng(7)
            dither = (rng_a.random(code.shape, dtype=np.float32)
                      - rng_b.random(code.shape, dtype=np.float32))
            gm_px = np.clip(code * 255.0 * float(gain_scale) + dither, 0, 254.0)
            del code, dither
            if invert_gain:
                gm_px = 255.0 - gm_px
            gm = Image.fromarray(gm_px.astype(np.uint8))
            del gm_px
            if debug:
                # 与 JPEG 内嵌的那张增益图逐像素一致（含缩放与抖动），便于对照。
                gm.save(outdir / f"{stem}_gainmap_embedded.png")
            if gain_rgb:
                gm = gm.convert("RGB")
            gm_buf = io.BytesIO()
            gm.save(gm_buf, "JPEG", quality=quality, optimize=True)
            result["gmax_meta"] = gmax_meta
            blob = ultrahdr.build(base_jpeg, gm_buf.getvalue(), gmax=gmax_meta,
                                  dpi=dpi, icc=_srgb_icc() if embed_icc else None,
                                  iso_flags=iso_flags)
            path = outdir / f"{stem}_ultrahdr.jpeg"
            _write_atomic(path, blob)
            result["files"]["jpeg"] = str(path)
            result["stats"]["base_reused"] = bool(base_reused)
            t0 = mark("jpeg", t0)
        except Exception as exc:                 # noqa: BLE001
            fail("jpeg", exc)
    del gain

    if "png" in formats:
        try:
            notify("编码 16bit PNG …")
            path = outdir / f"{stem}_hdr16_{transfer.lower()}.png"
            _write_atomic(path, png16.encode(q16, dpi=dpi, transfer=transfer))
            result["files"]["png"] = str(path)
            t0 = mark("png", t0)
        except Exception as exc:                 # noqa: BLE001
            fail("png", exc)

    for key, bits in (("heic10", 10), ("heic12", 12)):
        if key not in formats:
            continue
        try:
            notify(f"编码 {bits}bit HEIC …")
            path = outdir / f"{stem}_hdr{bits}_{transfer.lower()}.heic"
            _write_atomic(path, heic10.encode(q16, transfer=transfer, bits=bits,
                                              quality=quality, dpi=dpi))
            result["files"][key] = str(path)
            t0 = mark(key, t0)
        except Exception as exc:                 # noqa: BLE001
            fail(key, exc)

    if "avif" in formats:
        try:
            from . import avif as avifmod
            notify("编码 12bit AVIF …")
            blob, bits = avifmod.encode(q16, transfer=transfer, bits=12,
                                        quality=quality, dpi=dpi)
            path = outdir / f"{stem}_hdr{bits}_{transfer.lower()}.avif"
            _write_atomic(path, blob)
            result["files"]["avif"] = str(path)
            result["stats"]["avif_bits"] = bits
            t0 = mark("avif", t0)
        except Exception as exc:                 # noqa: BLE001
            fail("avif", exc)

    timings["total"] = round(time.perf_counter() - t_start, 2)
    result["ok"] = not result["errors"]
    notify(f"完成，用时 {timings['total']:.1f}s")
    return result
