"""自适应增益图：只抬"发光体"，阈值按每张图自己的光影分布走。

判据
----
`bright`  绝对亮度门槛：只认接近白的像素（头发、皮肤、衣料本体不抬）；
`peak`    相对邻域的突起程度：局部比周围亮才算"光"，平涂的亮面不算；
`halo`    把核心向外扩一圈做 bloom，避免硬边；
`finalize` 软拐点归一：让最亮的 0.1% 恰好用满量程。

为什么阈值要自适应
------------------
固定阈值在不同曝光的图上必然出错：夜景里 0.6 已是很亮的高光，白天的白墙
也是 0.9。参照局部适应亮度做阈值（Chapiro, "Perceptually Adaptive Real-Time
Tone Mapping", 2023：适应亮度取金字塔更高层，阈值随适应亮度缩放），这里用
图像自身的亮度分位数代替：99 分位往下一点作为门槛，99.9 分位作为饱和点。
这样暗图阈值自然低、亮图阈值自然高，不需要按素材类型分档。
"""
from __future__ import annotations

import numpy as np
from PIL import Image

from .color import srgb_to_linear, linear_to_srgb

# 4× 放大的插画常常上亿像素，Pillow 会当成"解压炸弹"报警；这里是我们自己的输入，直接放开
Image.MAX_IMAGE_PIXELS = None

PEAK_SCALE = 0.70   # 峰值相对旧版的收敛比例（最亮的点不再顶太高）
GMAX_CAP = 1.45      # 提升上限（EV）：实测超过 ~1.6 EV 时手机端高光会出现黑斑

# 判据的固定部分：门槛 b0/b1 与强度 gmax 由 adapt_params() 按图计算
BASE_CFG: dict = {
    "pr": 25, "pr2": 0, "q0": 1.06, "q1": 1.35,
    "spread": 0.0, "spread_r": 0,   # 大光团的柔光扩散（0 = 关闭）
    "pr3": 0, "q3": (1.03, 1.6),   # 中尺度：逆光/边缘光这类条带状受光       # 求局部均值的半径（像素）
    "p0": 1.45,     # peak 的起始权重
    "p1": 2.90,     # peak 的饱和点（局部亮 2.9× 视为纯光）
    "flat": 0.05,   # 平底兜底权重：纯色亮面留一点余量，避免死平
    "halo": 0.16,   # 泛光占比
    "hr": 3,        # 泛光半径（像素）
    "gmax": 2.2,    # 最大提升档数（EV）
    "lift": 1.0,    # 整体抬升（线性倍数）：默认不抬；需要时由调用方覆盖
}


def load_rgb(path: str) -> tuple[np.ndarray, Image.Image]:
    """读图 → (float32 [0,1] 的 sRGB 数组, RGB 图)；带 alpha 的贴黑底。"""
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
    """Hermite 平滑阶跃，e0→e1 之间平滑过渡。"""
    if e1 <= e0:
        return (x >= e0).astype(np.float32)
    t = np.clip((x - e0) / (e1 - e0), 0.0, 1.0)
    return (t * t * (3.0 - 2.0 * t)).astype(np.float32)


def luma(rgb: np.ndarray) -> np.ndarray:
    """sRGB → 线性 BT.709 亮度 Y。"""
    lin = srgb_to_linear(rgb)
    return (0.2126 * lin[..., 0] + 0.7152 * lin[..., 1] + 0.0722 * lin[..., 2]).astype(np.float32)


def adapt_params(rgb: np.ndarray) -> dict:
    """按这张图自己的亮度分布算出判据参数。

    t ∈ [0,1] 表示整体曝光：0 = 夜景（大面积深色），1 = 高调（整片都亮）。
    b0/b1 取亮度分位：只有最亮的那 1% 附近开始抬，99.9 分位饱和，
    因此白发、白墙、高光皮肤不会被整片推白。
    """
    y = luma(rgb)
    p50, p99, p999 = (float(np.percentile(y, q)) for q in (50, 99, 99.9))
    t = float(np.clip(p50 / 0.60, 0.0, 1.0))

    cfg = dict(BASE_CFG)
    cfg["b0"] = round(min(0.95, max(0.55, p99 - 0.05)), 3)
    cfg["b1"] = round(min(1.0, max(cfg["b0"] + 0.02, p999 + 0.004)), 3)
    cfg["halo"] = round(0.16 - 0.06 * t, 3)      # 泛光占比：暗 0.16 → 亮 0.10（只做柔化，不铺底）
    cfg["hr"] = max(1, round(3 - 2 * t))         # 泛光半径：暗 3px → 亮 1px
    # 峰值收敛：最亮的点（中心光斑）压到旧版的一部分；不再做整体抬升
    cfg["gmax"] = round(min((2.2 - 0.6 * t) * PEAK_SCALE, GMAX_CAP), 3)
    cfg["pr2"] = max(25, min(y.shape[0], y.shape[1]) // 10)
    cfg["pr3"] = max(12, min(y.shape[0], y.shape[1]) // 60)
    # 受光区域抬多少：高调图少抬（本来就亮），低调图多抬；细碎高光另算
    cfg["base_lit"] = round(0.78 - 0.16 * t, 3)
    cfg["base_spark"] = 0.45
    cfg["base_irr"] = 0.45        # 光传播/漫反射场权重
    cfg["base_amb"] = 0.10        # 全局光照/环境光底噪（暗部也会有一点点）
    cfg["i0"], cfg["i1"] = 0.35, 0.85
    cfg["lift"] = 1.0                          # 不再整体抬升：只抬"被照到的地方"
    return cfg


def emissive(rgb: np.ndarray, cfg: dict) -> tuple[np.ndarray, np.ndarray]:
    """返回 (亮度 Y, 发光权重 emis)，均在 [0,1]。

    判据分两层，都用大尺度平滑量，避免"同样亮的两块地方一块亮一块不亮"：
      1) 受光区域：用 ~1/25 图宽的邻域均值估计这张图的光照分布，取它的
         55–97 分位做平滑阈值 —— 灯光照到的地方整片抬起，暗部不动；
         因为判据本身是低频的，相邻像素不会逐块跳变。
      2) 细碎高光：星芒、亮点、小光斑这类"比周围明显突起"的像素额外加一层。
    """
    y = luma(rgb)
    h, w = y.shape
    r_illum = max(8, min(h, w) // 25)
    illum = _gauss(y, r_illum)
    # 阈值区间要足够宽：窄区间会让平滑渐变上出现"等值线圈"，白布上就是一圈明显的光圈
    lo, hi = (float(np.percentile(illum, q)) for q in (38, 99))
    lit = _smoothstep(lo, max(hi, lo + 1e-3), illum)          # 被照到的整片区域
    peak = y / (_gauss(y, cfg["pr"]) + 1e-4)
    spark = _smoothstep(cfg["p0"], cfg["p1"], peak)           # 细碎高光

    # 3) 光的传播/衍射/漫反射：光从高光源向外扩散、随距离衰减，再乘表面反射率。
    #    这一层让"光球照到的地方"也拿到增益，而不是只有光源本身亮。
    from .light import irradiance
    irr = _smoothstep(cfg["i0"], cfg["i1"], irradiance(y))

    # 4) 全局光照 + 环境光遮蔽：远离光源的暗部也吃一点环境光，但凹缝/被遮挡处更少
    r_small = max(4, min(h, w) // 60)
    r_big = max(12, min(h, w) // 20)
    near = _gauss(y, r_small)
    far = _gauss(y, r_big)
    ao = np.clip((near + 0.02) / (far + 0.02), 0.45, 1.0) ** 0.9   # 遮蔽近似：局部比大范围暗 = 凹缝
    env = _smoothstep(0.02, 0.45, far) * ao
    amb = cfg["base_amb"] * env

    # 纯白高光（镜面/白布最亮处）要整片吃增益：只靠 relative peak 会在亮斑边缘
    # 形成一圈"光环"（核心比值≈1 不触发、边缘触发），补一个绝对亮度项把中心填上。
    emis = (cfg["base_lit"] * lit + cfg["base_irr"] * irr
            + cfg["base_spark"] * spark + amb)
    emis = cfg["halo"] * _gauss(emis, cfg["hr"]) + (1.0 - cfg["halo"]) * emis
    return y, np.clip(emis, 0.0, 1.0).astype(np.float32)


def finalize(emis: np.ndarray) -> np.ndarray:
    """平滑饱和到 (0,1)：用指数逼近而不是硬截断。

    硬截断会在亮部内部造出一块"顶到 1.0 的平台"，平台与渐变的交界就是肉眼可见的
    等值线光圈（白布上那一圈）。指数饱和处处连续可导，最亮处也只是趋近 1，
    不会再出现平台边界。
    """
    p = float(np.percentile(emis, 99.9)) or 1.0
    em = 1.0 - np.exp(-emis / max(p, 1e-6) * 1.6)
    return _gauss(em, 2)


def gain_of(emis: np.ndarray, gmax: float, lift: float = 1.0) -> np.ndarray:
    """发光权重 → 线性增益倍数。

    lift 是整体抬升（所有像素都乘），gmax 只作用于发光权重：
    中间调（皮肤、衣料）整体略亮，而最亮的点相对收敛。
    """
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
