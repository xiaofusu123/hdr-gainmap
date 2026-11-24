"""启发式增益图：把"看起来在发光的东西"挑出来，只给它们亮度余量。

背景：8bit SDR 图的高光大多已经撞到 255，没有真实细节可恢复。
     AI 反色调映射（ITM）在二次元平涂上会糊块、线稿发光；
     这里改用几何/局部对比度判据，只抬光柱、星芒、水面反光、眼神光这类"发光体"。

判据：亮度够高（bright）且局部比周围亮（peak）；flat 给出平缓底的兜底权重，
     halo 把核心向外扩一圈做 bloom，避免硬边。
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from .color import srgb_to_linear, linear_to_srgb

# 4× 放大的插画常常上亿像素，Pillow 会当成"解压炸弹"报警；这里是我们自己的输入，直接放开
Image.MAX_IMAGE_PIXELS = None

# 四套预设：strong 实机验证过观感最好（默认）/ lights 只点光源 / soft 温和 / night 夜景偏亮
PROFILES: dict[str, dict] = {
    "strong": dict(b0=0.30, b1=0.80, pr=25, p0=1.45, p1=2.90, flat=0.15, halo=0.50, hr=11, gmax=2.6),
    # detail：高调素材专用，按局部结构选点（蕾丝/发丝/窗框），平坦亮面不动
    "detail": dict(b0=0.35, b1=0.90, pr=25, p0=1.50, p1=3.00, flat=0.00, halo=0.30, hr=7, gmax=1.6,
                   dr=6, d0=0.045, d1=0.20),
    "lights": dict(b0=0.45, b1=0.90, pr=25, p0=1.60, p1=3.20, flat=0.00, halo=0.40, hr=9, gmax=2.0),
    "soft": dict(b0=0.10, b1=0.52, pr=30, p0=1.25, p1=2.80, flat=0.35, halo=0.55, hr=12, gmax=2.0),
    "night": dict(b0=0.06, b1=0.44, pr=30, p0=1.15, p1=2.40, flat=0.55, halo=0.62, hr=14, gmax=2.0),
}


REF_WIDTH = 3240     # 预设的半径按这个宽度调出来的，放大后要等比放大半径


def fit_width(cfg: dict, width: int) -> dict:
    """半径（pr / hr）随图像宽度等比放大，保证放大后光晕相对大小不变。"""
    k = max(1.0, width / REF_WIDTH)
    out = dict(cfg)
    out["pr"] = max(1, round(cfg["pr"] * k))
    out["hr"] = max(1, round(cfg["hr"] * k))
    return out


def load_rgb(path: str) -> tuple[np.ndarray, Image.Image]:
    """读图 → (float32 [0,1] 的 sRGB 数组, RGB 图)；RGBA 贴到黑底。"""
    im = Image.open(path)
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGB", im.size, (0, 0, 0))
        bg.paste(im, mask=im.split()[3])
        im = bg
    else:
        im = im.convert("RGB")
    return np.asarray(im, dtype=np.float32) / 255.0, im


def _box(a: np.ndarray, r: int) -> np.ndarray:
    """均值模糊（可分离，累加和实现），边界复制。"""
    if r < 1:
        return a
    p = np.pad(a, r, mode="edge")
    k = 2 * r + 1

    c = np.cumsum(p, axis=0, dtype=np.float64)
    c = np.vstack([np.zeros((1, c.shape[1])), c])
    rows = (c[k:] - c[:-k]) / k

    c = np.cumsum(rows, axis=1, dtype=np.float64)
    c = np.hstack([np.zeros((c.shape[0], 1)), c])
    return ((c[:, k:] - c[:, :-k]) / k).astype(np.float32)


def _gauss(a: np.ndarray, r: int) -> np.ndarray:
    """三次均值模糊 ≈ 高斯（够用且比真高斯快）。"""
    for _ in range(3):
        a = _box(a, r)
    return a


def _smoothstep(e0: float, e1: float, x: np.ndarray) -> np.ndarray:
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return (t * t * (3.0 - 2.0 * t)).astype(np.float32)


def emissive(rgb: np.ndarray, cfg: dict) -> tuple[np.ndarray, np.ndarray]:
    """返回 (亮度 Y, 发光权重 emis)，均在 [0,1]。"""
    lin = srgb_to_linear(rgb)
    y = 0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]

    bright = _smoothstep(cfg["b0"], cfg["b1"], y)                 # 绝对亮度门槛
    if cfg.get("d0") is not None:                                 # 细节掩膜档：只认有结构的像素
        base = _gauss(y, cfg["dr"])
        detail = np.abs(y - base) / (base + 0.02)
        emis = bright * _smoothstep(cfg["d0"], cfg["d1"], detail)
    else:
        peak = y / (_gauss(y, cfg["pr"]) + 1e-4)                  # 相对邻域的突起程度
        peak_w = _smoothstep(cfg["p0"], cfg["p1"], peak)
        emis = bright * (cfg["flat"] + (1.0 - cfg["flat"]) * peak_w)  # 平底兜底 + 突起加权
    emis = cfg["halo"] * _gauss(emis, cfg["hr"]) + (1.0 - cfg["halo"]) * emis
    return y, np.clip(emis, 0.0, 1.0).astype(np.float32)


def finalize(emis: np.ndarray) -> np.ndarray:
    """软拐点归一：让最亮的那 0.1% 恰好用到满量程，避免整片顶到上限。"""
    p = float(np.percentile(emis, 99.9)) or 1.0
    em = emis * (1.0 + emis / (p * p)) / (1.0 + emis)
    return _gauss(np.clip(em, 0.0, 1.0), 2)


def gain_of(emis: np.ndarray, gmax: float) -> np.ndarray:
    """发光权重 → 线性增益倍数（gmax 为最大档数，2.0 = +2 EV = 4×）。"""
    return (2.0 ** (gmax * emis)).astype(np.float32)


def render_preview(rgb: np.ndarray, gain: np.ndarray, knee: float = 0.14, k: float = 2.5) -> np.ndarray:
    """把 HDR 结果压回 SDR 展示：1.0 以下原样，以上保留结构不要死白。"""
    x = srgb_to_linear(rgb) * gain[..., None]
    y = np.where(x <= 1.0, x, 1.0 - knee * np.exp(-(x - 1.0) * k))
    return (linear_to_srgb(y) * 255.0).round().astype(np.uint8)
