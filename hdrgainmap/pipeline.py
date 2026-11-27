"""一条龙：源图 → 光场 → 分通道增益 → 各格式的 HDR 母版。

jpeg Ultra HDR（MPF + XMP + ISO 21496-1）、png 16bit PQ、
heic10/12 HEVC Main10/12、avif 12bit AV1、jxl 24bit JPEG XL。
统一 BT.2020 + PQ；HDR 按行分块编码，增益图全分辨率 RGB、量化前抖动。
"""
from __future__ import annotations

import io
import os
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import numpy as np
from PIL import Image

from . import gainmap, gpu, heic10, jxl, lut, png16, ultrahdr
from . import light

FORMATS = ("jpeg", "png", "heic10", "heic12", "avif", "jxl")
QUALITY = 100
JPEG_SUFFIXES = {".jpg", ".jpeg", ".jpe", ".jfif"}
SDR_WHITE_NITS = heic10.SDR_WHITE_NITS
TRANSFER = "PQ"

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
    """返回 (8bit JPEG 字节, 是否复用源文件字节)。复用条件：源为 JPEG 且像素/DPI 未变。"""
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


def _channel_terms(rgb: np.ndarray, emis: np.ndarray, gmax: float,
                   weights: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """返回 (线性 RGB, 每通道 log2 增益)。tint 按像素色相微调，再按亮度归一。

    幂运算全部查 ``lut``：与 GPU 共用同一张表，两边结果对齐到码值。
    """
    lin = lut.srgb_to_linear(rgb)
    lum = np.maximum(np.float32(0.2126) * lin[..., 0]
                     + np.float32(0.7152) * lin[..., 1]
                     + np.float32(0.0722) * lin[..., 2], np.float32(1e-4))
    w = lut.tint(lin / lum[..., None]) * weights.astype(np.float32)
    w /= (np.float32(0.2126) * w[..., 0] + np.float32(0.7152) * w[..., 1]
          + np.float32(0.0722) * w[..., 2])[..., None]
    return lin, (np.float32(gmax) * emis[..., None] * w).astype(np.float32)


def _hdr_band(rgb: np.ndarray, emis: np.ndarray, gmax: float,
              weights: np.ndarray) -> tuple[np.ndarray, float]:
    """一块 RGB + emis → BT.2020 PQ 码值（0..1），返回峰值 nits。"""
    lin, expo = _channel_terms(rgb, emis, gmax, weights)
    hdr = lut.to_bt2020(lin * lut.exp2(expo)) * np.float32(SDR_WHITE_NITS)
    return np.clip(lut.pq(hdr), 0.0, 1.0).astype(np.float32), float(hdr.max())


def _hdr_code(rgb: np.ndarray, emis: np.ndarray, gmax: float, weights: np.ndarray,
              *, tile_rows: int = 2048, use_gpu: bool = False) -> tuple[np.ndarray, float]:
    """BT.709 线性光 → BT.2020 PQ 的 uint16 码值；GPU 可用时整张一次算完。"""
    if use_gpu and gpu.available():
        return gpu.encode_pq(rgb, emis, gmax, weights)
    h = rgb.shape[0]
    out = np.empty(rgb.shape, dtype=np.uint16)
    peak = 0.0
    for y0 in range(0, h, tile_rows):
        y1 = min(h, y0 + tile_rows)
        code, band_peak = _hdr_band(rgb[y0:y1], emis[y0:y1], gmax, weights)
        peak = max(peak, band_peak)
        out[y0:y1] = np.clip(np.round(code * 65535.0), 0, 65535).astype(np.uint16)
    return out, peak


def _write_pfm(rgb: np.ndarray, emis: np.ndarray, gmax: float, weights: np.ndarray,
               path: Path, *, tile_rows: int = 2048) -> None:
    """写 PFM 给 cjxl：float32 能无损装下 24bit 码值；行序自下而上。"""
    h, w, _ = rgb.shape
    with open(path, "wb") as fh:
        fh.write(f"PF\n{w} {h}\n-1.0\n".encode("ascii"))
        for y1 in range(h, 0, -tile_rows):
            y0 = max(0, y1 - tile_rows)
            code, _ = _hdr_band(rgb[y0:y1], emis[y0:y1], gmax, weights)
            fh.write(np.ascontiguousarray(code[::-1]).astype("<f4").tobytes())


def _save_gain_debug(gain: np.ndarray, path: Path, gmax_meta: float,
                     *, tile_rows: int = 2048) -> None:
    """把全分辨率增益写成 8bit 可视化 PNG（log2 域，与内嵌增益图同一套归一化）。"""
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
          debug: bool = False,
          lift: float | None = None, peak_scale: float | None = None,
          embed_icc: bool = True, iso_flags: int = 0x40,
          alpha_bg: tuple[int, int, int] = (0, 0, 0),
          tile_rows: int = 2048, use_gpu: bool = False, jobs: int = 1,
          log=None) -> dict:
    """按格式生成 HDR 母版，返回文件、统计与耗时。use_gpu/jobs 控制加速与并行。"""
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
    if dpi is not None and float(dpi) <= 0:
        raise ValueError(f"dpi 必须为正数，收到 {dpi}")

    if use_gpu:                                  # 与读图/光场并行建管线
        threading.Thread(target=gpu.warm, daemon=True).start()

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

    # 1) 光场分析一次，自适应参数 / 发光权重 / 光色共用
    notify("分析光场 …")
    y = gainmap.luma(rgb)
    scene = light.analyze(rgb, y)
    cfg = gainmap.adapt_params(rgb, y=y, scene=scene)
    if lift is not None:
        cfg["lift"] = float(lift)
    if peak_scale is not None:
        cfg["gmax"] = round(cfg["gmax"] / gainmap.PEAK_SCALE * float(peak_scale), 3)
    _, emis = gainmap.emissive(rgb, cfg, y=y, scene=scene)
    del y
    emis = gainmap.finalize(emis)
    gain = gainmap.gain_of(emis, cfg["gmax"], cfg["lift"])
    w_light = gainmap.channel_weights(rgb, scene=scene)
    t0 = mark("gain", t0)

    # 2) 重建 HDR 线性光并编码。PNG/HEIC/AVIF 三份产物逐像素来自同一份 q16。
    notify("编码 BT.2020 + PQ …")
    q16, peak_nits = _hdr_code(rgb, emis, cfg["gmax"], w_light,
                               tile_rows=tile_rows, use_gpu=use_gpu)
    t0 = mark("hdr", t0)

    result: dict = {
        "src": str(src),
        "dpi": dpi,
        "transfer": TRANSFER,
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
        "gpu": gpu.info() if (use_gpu and gpu.available()) else None,
    }

    # 3) 先把 JPEG 增益图算完（两遍扫全图），再让各编码器并行落盘
    stem = src.stem
    if debug:
        # 发光权重是中间场，最终增益才是编码进增益图的数据，两者分开落盘。
        Image.fromarray((emis * 255.0).astype(np.uint8)).save(outdir / f"{stem}_emis.png")
        gmax_debug = float(np.log2(max(float(gain.max()), 1.0)))
        _save_gain_debug(gain, outdir / f"{stem}_gainmap.png", gmax_debug)
    del gain
    lock = threading.Lock()

    def fail(key: str, exc: Exception) -> None:
        with lock:
            result["errors"][key] = f"{type(exc).__name__}: {exc}"
        notify(f"{key} 失败：{exc}")

    def run(key: str, fn) -> None:
        t0 = time.perf_counter()
        try:
            path = fn()
        except Exception as exc:                     # noqa: BLE001
            fail(key, exc)
            return
        with lock:
            result["files"][key] = str(path)
            timings[key] = round(time.perf_counter() - t0, 2)

    gm_blob: bytes | None = None
    if "jpeg" in formats:
        notify("准备增益图 …")
        t0 = time.perf_counter()
        h, w = emis.shape
        on_gpu = use_gpu and gpu.available()
        # 两遍：先求每通道最大对数增益，再按通道归一化写码值（全分辨率 RGB）
        expo_max = np.zeros(3, dtype=np.float64)
        for y0 in range(0, h, tile_rows):
            y1 = min(h, y0 + tile_rows)
            band = emis[y0:y1]
            if on_gpu:
                _, em = gpu.expo(rgb[y0:y1], band, cfg["gmax"], w_light)
            else:
                _, ex = _channel_terms(rgb[y0:y1], band, cfg["gmax"], w_light)
                em = ex.reshape(-1, 3).max(axis=0)
            expo_max = np.maximum(expo_max, em)
        expo_max = np.maximum(expo_max, 1e-4)

        # 8bit 增益图 + 平滑渐变的等值线圈用 ±0.5 LSB 三角抖动打散
        rng_a = np.random.default_rng(20261125)
        rng_b = np.random.default_rng(7)
        gm_px = np.empty((h, w, 3), dtype=np.uint8)
        for y0 in range(0, h, tile_rows):
            y1 = min(h, y0 + tile_rows)
            band = emis[y0:y1]
            if on_gpu:
                expo = gpu.expo(rgb[y0:y1], band, cfg["gmax"], w_light)[0]
            else:
                expo = _channel_terms(rgb[y0:y1], band, cfg["gmax"], w_light)[1]
            code = np.clip(expo / expo_max, 0.0, 1.0) * 255.0
            code += (rng_a.random(code.shape, dtype=np.float32)
                     - rng_b.random(code.shape, dtype=np.float32))
            gm_px[y0:y1] = np.clip(code, 0.0, 254.0).astype(np.uint8)
        gm = Image.fromarray(gm_px, "RGB")
        del gm_px
        if debug:
            gm.save(outdir / f"{stem}_gainmap_embedded.png")
        gm_buf = io.BytesIO()
        gm.save(gm_buf, "JPEG", quality=quality, subsampling=0, optimize=True)
        gm_blob = gm_buf.getvalue()
        gmax_meta = float(expo_max.max())
        gmax_channels = tuple(float(v) for v in expo_max)
        result["gmax_meta"] = gmax_meta
        result["gmax_channels"] = gmax_channels
        timings["gainmap"] = round(time.perf_counter() - t0, 2)

    # 各任务只读共享数组，互不写同一文件；AVIF 线程数随后按并行度回填
    tasks = {}
    avif_threads = 0

    if "jpeg" in formats:
        def do_jpeg() -> Path:
            blob = ultrahdr.build(base_jpeg, gm_blob, gmax=gmax_meta,
                                  gmax_channels=gmax_channels, dpi=dpi,
                                  icc=_srgb_icc() if embed_icc else None,
                                  iso_flags=iso_flags)
            path = outdir / f"{stem}_ultrahdr.jpeg"
            _write_atomic(path, blob)
            return path

        tasks["jpeg"] = do_jpeg

    if "png" in formats:
        def do_png() -> Path:
            path = outdir / f"{stem}_hdr16_pq.png"
            _write_atomic(path, png16.encode(q16, dpi=dpi, peak_nits=peak_nits,
                                             jobs=jobs))
            return path

        tasks["png"] = do_png

    for key, bits in (("heic10", 10), ("heic12", 12)):
        if key not in formats:
            continue

        def do_heic(bits=bits) -> Path:
            path = outdir / f"{stem}_hdr{bits}_pq.heic"
            _write_atomic(path, heic10.encode(q16, bits=bits, quality=quality, dpi=dpi))
            return path

        tasks[key] = do_heic

    if "avif" in formats:
        def do_avif() -> Path:
            from . import avif as avifmod
            blob, bits = avifmod.encode(q16, bits=12, quality=quality, dpi=dpi,
                                        threads=avif_threads)
            path = outdir / f"{stem}_hdr{bits}_pq.avif"
            _write_atomic(path, blob)
            with lock:
                result["stats"]["avif_bits"] = bits
            return path

        tasks["avif"] = do_avif

    if "jxl" in formats:
        def do_jxl() -> Path:
            fd, pfm_name = tempfile.mkstemp(prefix=f".{stem}.", suffix=".pfm",
                                            dir=str(outdir))
            os.close(fd)
            pfm_path = Path(pfm_name)
            tmp_jxl = outdir / f".{stem}.pq.tmp.jxl"
            try:
                _write_pfm(rgb, emis, cfg["gmax"], w_light, pfm_path, tile_rows=tile_rows)
                jxl.encode(pfm_path, tmp_jxl, "PQ", dpi=dpi)
                path = outdir / f"{stem}_hdr24_pq.jxl"
                os.replace(tmp_jxl, path)
                return path
            finally:
                for leftover in (pfm_path, tmp_jxl):
                    try:
                        leftover.unlink()
                    except FileNotFoundError:
                        pass

        tasks["jxl"] = do_jxl

    workers = max(1, min(int(jobs), len(tasks))) if tasks else 1
    avif_threads = max(1, (os.cpu_count() or 4) // workers)
    result["workers"] = workers
    notify(f"编码 {len(tasks)} 个格式（{workers} 路并行）…")
    if workers == 1:
        for key, fn in tasks.items():
            run(key, fn)
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(run, key, fn) for key, fn in tasks.items()]
            for fut in as_completed(futures):
                fut.result()
    del rgb, emis, q16

    result["stats"]["base_reused"] = bool(base_reused)
    timings["total"] = round(time.perf_counter() - t_start, 2)
    result["ok"] = not result["errors"]
    notify(f"完成，用时 {timings['total']:.1f}s")
    return result
