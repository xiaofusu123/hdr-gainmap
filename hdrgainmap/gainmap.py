"""增益图：光场 → 发光权重 emis → 每通道增益。

判据：lit（受光）、spark（细碎高光）、irr（辐照度）、ao（遮蔽）、
white（绝对白）、area（大面积平亮惩罚）；阈值取亮度分位数（Chapiro 2023）。
``channel_weights()`` 从光色给 R/G/B 权重，按 BT.709 亮度归一。
"""
from __future__ import annotations

import io
from pathlib import Path

import numpy as np
from PIL import Image, ImageCms, ImageOps

from .color import linear_to_srgb, srgb_to_linear

# 4× 放大的插画常常上亿像素，Pillow 会当成"解压炸弹"报警；这里是我们自己的输入，直接放开
Image.MAX_IMAGE_PIXELS = None

PEAK_SCALE = 0.70   # 峰值收敛：gmax 只取 2.2 EV 的 70%，避免最亮处过冲
GMAX_CAP = 1.45      # 提升上限（EV）：实测超过 ~1.6 EV 时手机端高光会出现黑斑

_JPEG_SUFFIXES = {".jpg", ".jpeg", ".jpe", ".jfif"}

# 判据的固定部分：受光阈值（illum 分位）与强度 gmax 由 adapt_params()/emissive() 按图计算
BASE_CFG: dict = {
    "pr": 25,        # 局部突起判据的半径（像素）
    "p0": 1.45,      # peak 的起始权重
    "p1": 2.90,      # peak 的饱和点（局部亮 2.9× 视为纯光）
    "halo": 0.16,    # 泛光占比
    "hr": 3,         # 泛光半径（像素）
    "gmax": 2.2,     # 最大提升档数（EV）
    "lift": 1.0,     # 整体抬升（线性倍数）：默认不抬；需要时由调用方覆盖
    # 大面积平亮区惩罚：area 越大、spark 越弱，惩罚越强，避免白墙/天空整片发亮
    "area_pen": 0.55,
    "area_lo": 0.35,
    "area_hi": 0.85,
    "base_white": 0.18,
}


def _dpi_of(im: Image.Image) -> float:
    """源图 DPI（缺省 96），让输出属性面板与原图一致。"""
    dpi = im.info.get("dpi")
    if isinstance(dpi, (tuple, list)) and dpi and dpi[0]:
        try:
            value = float(dpi[0])
            if value > 0:
                return value
        except (TypeError, ValueError):
            pass
    return 96.0


def prepare_image(path: str | Path, *, alpha_bg: tuple[int, int, int] = (0, 0, 0),
                  auto_orient: bool = True, convert_icc: bool = True
                  ) -> tuple[np.ndarray, Image.Image, dict]:
    """读图 → (sRGB float32 数组, 处理后图像, 元信息)。

    顺序：EXIF 转正 → alpha 合成 → ICC 转 sRGB。``reusable_source`` 为真才允许
    复用源 JPEG 字节（像素未被改动）。
    """
    src = Path(path)
    im = Image.open(path)
    dpi = _dpi_of(im)
    try:
        orientation = int(im.getexif().get(0x0112, 1) or 1)
    except Exception:                            # noqa: BLE001  坏 EXIF 不该让整张图失败
        orientation = 1
    if auto_orient and orientation != 1:
        im = ImageOps.exif_transpose(im) or im

    # 注意不能只看 "A" in bands：LAB 的 a 轴通道也是 'A'，会被误判成 alpha。
    had_alpha = (im.mode in ("RGBA", "LA", "PA", "RGBa", "La")
                 or (im.mode == "P" and "transparency" in im.info))
    icc = im.info.get("icc_profile")
    icc_converted = False

    def _to_srgb(image: Image.Image) -> Image.Image:
        """有 ICC 且不是 sRGB 时转 sRGB；失败保持原样（宁可偏色，也不要读不出图）。"""
        nonlocal icc_converted
        if not (convert_icc and icc):
            return image
        try:
            src_prof = ImageCms.ImageCmsProfile(io.BytesIO(icc))
            desc = (ImageCms.getProfileDescription(src_prof) or "").lower()
            if "srgb" in desc:
                return image
            if image.mode not in ("RGB", "CMYK", "LAB"):
                image = image.convert("RGB")
            out = ImageCms.profileToProfile(
                image, src_prof, ImageCms.createProfile("sRGB"),
                outputMode="RGB", renderingIntent=0)
            icc_converted = True
            return out
        except Exception:                        # noqa: BLE001  lcms 缺失/坏 profile
            return image

    if had_alpha or im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, tuple(int(v) for v in alpha_bg))
        bg.paste(im, mask=im.getchannel("A"))
        im = _to_srgb(bg)
    else:
        im = _to_srgb(im)
    if im.mode != "RGB":
        im = im.convert("RGB")

    reusable = (src.suffix.lower() in _JPEG_SUFFIXES and not had_alpha
                and not icc_converted and orientation == 1)
    meta = {
        "dpi": dpi,
        "orientation": orientation,
        "had_alpha": had_alpha,
        "icc_converted": icc_converted,
        "reusable_source": reusable,
    }
    return np.asarray(im, dtype=np.float32) / 255.0, im, meta


def load_rgb(path: str | Path, **kwargs) -> tuple[np.ndarray, Image.Image]:
    """兼容旧接口：读图 → (sRGB 数组, RGB 图)。新代码请用 prepare_image()。"""
    rgb, im, _ = prepare_image(path, **kwargs)
    return rgb, im


def _box(a: np.ndarray, r: int) -> np.ndarray:
    """均值模糊（可分离累加和，边界复制）。float32 足够：17.8MP 误差约 4e-5。"""
    if r < 1:
        return a
    p = np.pad(a, r, mode="edge")
    k = 2 * r + 1

    c = np.cumsum(p, axis=0, dtype=np.float32)
    rows = np.empty((c.shape[0] - k + 1, c.shape[1]), dtype=np.float32)
    rows[0] = c[k - 1] / k
    rows[1:] = (c[k:] - c[:-k]) / k

    c = np.cumsum(rows, axis=1, dtype=np.float32)
    out = np.empty((c.shape[0], c.shape[1] - k + 1), dtype=np.float32)
    out[:, 0] = c[:, k - 1] / k
    out[:, 1:] = (c[:, k:] - c[:, :-k]) / k
    return out


def _gauss(a: np.ndarray, r: int) -> np.ndarray:
    """三次均值模糊 ≈ 高斯（够用且比真高斯快）。"""
    for _ in range(3):
        a = _box(a, r)
    return a


def _smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    """Hermite 平滑阶跃，e0→e1 之间平滑过渡。"""
    if e1 <= e0:
        return (x >= e0).astype(np.float32)
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return (t * t * (3.0 - 2.0 * t)).astype(np.float32)


def luma(rgb: np.ndarray) -> np.ndarray:
    """sRGB → 线性 BT.709 亮度 Y。"""
    lin = srgb_to_linear(rgb)
    return (0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]).astype(np.float32)


def adapt_params(rgb: np.ndarray, y: np.ndarray | None = None,
                 scene=None) -> dict:
    """按这张图自己的亮度分布算判据参数（兼容入口）。

    实际计算在 light.adapt()：与光场共用一份场景统计。这里保留旧签名，内部转调。
    """
    from .light import adapt, analyze
    if y is None:
        y = luma(rgb)
    if scene is None:
        scene = analyze(rgb, y)
    return adapt(y, scene)


def emissive(rgb: np.ndarray, cfg: dict,
             y: np.ndarray | None = None, scene=None) -> tuple[np.ndarray, np.ndarray]:
    """返回 (亮度 Y, 发光权重 emis)，均在 [0,1]。

    五层判据：受光区域（导引滤波保边）、细碎高光、光场辐照度、大面积平亮惩罚、
    绝对白。主要用大尺度平滑量，避免"同样亮的两块地方一块亮一块不亮"。
    """
    from .light import analyze, guided_filter

    if y is None:
        y = luma(rgb)
    if scene is None:
        scene = analyze(rgb, y)
    h, w = y.shape
    r_illum = max(8, min(h, w) // 25)
    # 以亮度为引导保边平滑（纯高斯会糊掉光斑边缘）
    illum = guided_filter(y, _gauss(y, r_illum), max(1, r_illum // 2), eps=1e-3)
    # 阈值区间要够宽，否则渐变处出现等值线圈
    lo, hi = (float(np.percentile(illum, q)) for q in (38, 99))
    lit = _smoothstep(lo, max(hi, lo + 1e-3), illum)          # 被照到的整片区域
    peak = y / (_gauss(y, cfg["pr"]) + 1e-3)
    spark = _smoothstep(cfg["p0"], cfg["p1"], peak)           # 细碎高光

    # 面积惩罚：低频面积大 = 白墙/天空；spark 强的点光源只罚 30%
    r_area = max(8, min(h, w) // 8)
    area = _gauss(lit, r_area)
    penalty = (cfg["area_pen"] * _smoothstep(cfg["area_lo"], cfg["area_hi"], area)
               * (1.0 - 0.7 * spark))
    lit = lit * (1.0 - penalty)

    # 光传播：定向光束 + 扩散 + 空间透视
    irr = _smoothstep(cfg["i0"], cfg["i1"], scene.irradiance)

    # 环境光：凹缝处更少（AO 来自光场模块）
    r_big = max(12, min(h, w) // 20)
    far = _gauss(y, r_big)
    env = _smoothstep(0.02, 0.45, far) * np.clip(scene.ao, 0.45, 1.0)
    amb = cfg["base_amb"] * env

    # 纯白高光补绝对判据，填掉亮斑中心不触发相对峰值判据的空档
    white = _smoothstep(0.92, 1.0, y) * (1.0 - penalty)
    emis = (cfg["base_lit"] * lit + cfg["base_irr"] * irr
            + cfg["base_spark"] * spark + cfg["base_white"] * white + amb)
    emis = cfg["halo"] * _gauss(emis, cfg["hr"]) + (1.0 - cfg["halo"]) * emis
    return y, np.clip(emis, 0.0, 1.0).astype(np.float32)


def channel_weights(rgb: np.ndarray, y: np.ndarray | None = None,
                    scene=None) -> np.ndarray:
    """光源颜色 → R/G/B 权重，按 BT.709 亮度归一（白光 = 1,1,1）。"""
    from .light import analyze
    if scene is None:
        if y is None:
            y = luma(rgb)
        scene = analyze(rgb, y)
    lc = np.clip(np.asarray(scene.light_color, dtype=np.float32), 0.25, 4.0)
    w = np.clip(lc ** 0.6, 0.80, 1.25)
    lum = float(0.2126 * w[0] + 0.7152 * w[1] + 0.0722 * w[2])
    return (w / max(lum, 1e-6)).astype(np.float32)


def finalize(emis: np.ndarray) -> np.ndarray:
    """指数饱和到 (0,1)，避免硬截断在亮部造出平台与等值线光圈。"""
    p = float(np.percentile(emis, 99.9)) or 1.0
    em = 1.0 - np.exp(-emis / max(p, 1e-6) * 1.6)
    return _gauss(em, 2)


def gain_of(emis: np.ndarray, gmax: float, lift: float = 1.0) -> np.ndarray:
    """发光权重 → 线性增益；lift 整体抬升，gmax 只作用于发光权重。"""
    # 高光增益要扣掉整体抬升的部分：整体已经抬了 lift 倍，高光只需再补 (b - lift + 1)，
    # 这样整体变亮的同时，高光与中调的差距反而被压缩（用户要的"减少差距"）。
    b = 2.0 ** (gmax * emis)
    extra = np.maximum(1.0, b - (lift - 1.0))
    return (lift * extra).astype(np.float32)


def render_preview(rgb: np.ndarray, gain: np.ndarray, knee: float = 0.14, k: float = 2.5) -> np.ndarray:
    """把 HDR 结果压回 SDR 查看：1.0 以下原样，以上保留结构不要死白。"""
    x = srgb_to_linear(rgb) * gain[..., None]
    y = np.where(x <= 1.0, x, 1.0 - knee * np.exp(-(x - 1.0) * k))
    return (linear_to_srgb(y) * 255.0).round().astype(np.uint8)
