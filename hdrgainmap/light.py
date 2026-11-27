"""光场分析：光源 → 光线步进 → 辐照度 → 空间透视，在降采样图上完成。

从亮度场反推光源（>p97）、8 方向衰减光束、多尺度扩散与空气透视；
``adapt()`` 与它共用同一份场景统计。
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .gainmap import _box, _gauss, _smoothstep

# 8 个传播方向（dy, dx）；步进顺序无关，最后按方向平均
_DIRECTIONS = ((1, 0), (-1, 0), (0, 1), (0, -1),
               (1, 1), (1, -1), (-1, 1), (-1, -1))

# 光场参数：集中在一处，方便文档与调参对照
RAY_DECAY = 0.88        # 每步的介质透过率（与 τ 相乘）
RAY_STEPS = 40          # 单方向最大步数（降采样图上约覆盖半幅）
RAY_MIX = 0.32          # 定向光束在总照度里的占比，其余给各向同性扩散
AERIAL_BETA = 1.1       # 空间透视消光系数：depth=1 处衰减到 e^-1.1 ≈ 0.33
AERIAL_SKY = 0.06       # 大气光混合量（远处雾感），只做很轻的抬升


@dataclass
class Scene:
    """一张图的光场与场景统计，供增益判据复用（避免重复模糊）。"""

    y: np.ndarray                       # 原分辨率亮度
    irradiance: np.ndarray              # 原分辨率辐照度 [0,1]
    ao: np.ndarray                      # 原分辨率环境光遮蔽 [0.45,1]
    depth: np.ndarray                   # 原分辨率深度代理 [0,1]
    light_color: tuple[float, float, float]  # 线性 RGB 光色（亮度归一）
    sources: np.ndarray = field(default_factory=lambda: np.zeros((0, 0,), np.float32))
    rays: np.ndarray = field(default_factory=lambda: np.zeros((0, 0,), np.float32))
    stats: dict = field(default_factory=dict)


def _downsample(y: np.ndarray, max_side: int = 640) -> tuple[np.ndarray, int]:
    """按整数倍块平均降采样（块平均 = 抗锯齿，比抽点稳）。"""
    h, w = y.shape
    step = max(1, int(round(max(h, w) / max_side)))
    if step <= 1:
        return y.astype(np.float32, copy=False), 1
    hh, ww = h // step * step, w // step * step
    blk = y[:hh, :ww].reshape(hh // step, step, ww // step, step).mean(axis=(1, 3))
    return blk.astype(np.float32), step


def _downsample_rgb(rgb: np.ndarray, step: int) -> np.ndarray:
    """与亮度同一网格的 RGB 降采样；step=1 时直接返回。"""
    if step <= 1:
        return rgb.astype(np.float32, copy=False)
    h, w, _ = rgb.shape
    hh, ww = h // step * step, w // step * step
    blk = rgb[:hh, :ww].reshape(hh // step, step, ww // step, step, 3).mean(axis=(1, 3))
    return blk.astype(np.float32)


def _upsample(y: np.ndarray, shape: tuple[int, int]) -> np.ndarray:
    """双线性放大（只用于低频场，够用且快）。"""
    h, w = shape
    ys = (np.arange(h, dtype=np.float32) + 0.5) * y.shape[0] / h - 0.5
    xs = (np.arange(w, dtype=np.float32) + 0.5) * y.shape[1] / w - 0.5
    y0 = np.clip(np.floor(ys), 0, y.shape[0] - 1).astype(np.int32)
    x0 = np.clip(np.floor(xs), 0, y.shape[1] - 1).astype(np.int32)
    y1 = np.clip(y0 + 1, 0, y.shape[0] - 1)
    x1 = np.clip(x0 + 1, 0, y.shape[1] - 1)
    fy = np.clip(ys - y0, 0, 1)[:, None].astype(np.float32)
    fx = np.clip(xs - x0, 0, 1)[None, :].astype(np.float32)
    # 先沿 y 插值成 (h, w_low)，再沿 x 插值：峰值内存从 2×O(h·w) 降到 O(h·w_low)，
    # 4× 放大的亿像素图不会再因为这一步多占几百 MB。
    tmp = y[y0] * (1.0 - fy) + y[y1] * fy
    return (tmp[:, x0] * (1.0 - fx) + tmp[:, x1] * fx).astype(np.float32)


def guided_filter(guide: np.ndarray, src: np.ndarray, r: int,
                  eps: float = 1e-3) -> np.ndarray:
    """自引导滤波（He 2010）：保边平滑，O(1) 半径。eps 越大越平滑。"""
    if r < 1:
        return src.astype(np.float32, copy=False)
    mean_i = _box(guide, r)
    mean_p = _box(src, r)
    corr_i = _box(guide * guide, r)
    corr_ip = _box(guide * src, r)
    var_i = np.maximum(corr_i - mean_i * mean_i, 0.0)
    cov_ip = corr_ip - mean_i * mean_p
    a = cov_ip / (var_i + eps)
    b = mean_p - a * mean_i
    return (_box(a, r) * guide + _box(b, r)).astype(np.float32)


def _shift(a: np.ndarray, dy: int, dx: int) -> np.ndarray:
    """按 (dy,dx) 平移并在越界处补 0；不用 np.roll（会首尾相接形成假光带）。"""
    out = a
    if dy:
        out = np.zeros_like(a)
        if dy > 0:
            out[dy:] = a[:-dy]
        else:
            out[:dy] = a[-dy:]
    if dx:
        src = out
        out = np.zeros_like(src)
        if dx > 0:
            out[:, dx:] = src[:, :-dx]
        else:
            out[:, :dx] = src[:, -dx:]
    return out


def _ray_march(src: np.ndarray, tau: np.ndarray,
               steps: int = RAY_STEPS) -> np.ndarray:
    """定向光线步进：8 方向各自递推 ``F ← S + k·shift(F)·τ``，再取平均。"""
    acc = np.zeros_like(src)
    for dy, dx in _DIRECTIONS:
        field = np.zeros_like(src)
        for _ in range(steps):
            field = src + RAY_DECAY * _shift(field, dy, dx) * tau
        acc += field
    acc /= float(len(_DIRECTIONS))
    top = float(np.percentile(acc, 99.5))
    if top <= 1e-6:
        return np.zeros_like(src)
    return np.clip(acc / top, 0.0, 1.0).astype(np.float32)


def _depth_proxy(ys: np.ndarray) -> np.ndarray:
    """深度代理：越靠上、局部对比越低 → 越远（半是构图，半是空气透视）。"""
    h, w = ys.shape
    vertical = (np.arange(h, dtype=np.float32) / max(h - 1, 1))[:, None]
    r = max(2, min(h, w) // 24)
    mu = _box(ys, r)
    var = np.maximum(_box(ys * ys, r) - mu * mu, 0.0)
    contrast = np.sqrt(var)
    scale = float(np.percentile(contrast, 90)) + 1e-6
    haze = 1.0 - np.clip(contrast / scale, 0.0, 1.0)
    depth = np.clip(0.55 * vertical + 0.45 * haze, 0.0, 1.0)
    return _gauss(depth, max(1, r // 2))


def analyze(rgb: np.ndarray, y: np.ndarray | None = None,
            *, max_side: int = 640) -> Scene:
    """分析一张图：光源、光束、辐照度、AO、深度与光色。"""
    from .gainmap import luma as _luma
    if y is None:
        y = _luma(rgb)
    ys, step = _downsample(y, max_side)
    h, w = ys.shape

    # 光源：高于 p97 的部分归一化
    hi = float(np.percentile(ys, 97.0))
    span = max(1e-3, float(ys.max()) - hi)
    src = np.clip((ys - hi) / span, 0.0, 1.0) ** 1.4
    has_light = float(src.max()) > 1e-6

    # 反射率：浅色表面反光更多
    tau = 0.45 + 0.55 * ys

    # 3) 定向光束 + 各向同性扩散
    rays = _ray_march(src, tau) if has_light else np.zeros_like(ys)
    diffuse = np.zeros_like(ys)
    if has_light:
        base = max(6.0, min(h, w) * 0.055)
        wsum = 0.0
        for k in range(4):
            sig = base * (2.0 ** k)
            wk = sig ** (-1.75)
            diffuse += wk * _gauss(src, int(round(sig)))
            wsum += wk
        diffuse /= max(wsum, 1e-6)
    field = (1.0 - RAY_MIX) * diffuse + RAY_MIX * rays

    # 4) 空间透视：远景衰减 + 极少量大气光
    depth = _depth_proxy(ys)
    atten = np.exp(-AERIAL_BETA * depth)
    field = field * atten + AERIAL_SKY * (1.0 - atten) * float(np.percentile(ys, 90))
    field *= 0.25 + 0.75 * ys
    field /= max(float(np.percentile(field, 99.5)), 1e-6)
    irr = np.clip(field, 0.0, 1.0).astype(np.float32)

    # 5) AO：局部/大范围亮度，凹缝 <1
    r_small = max(4, min(h, w) // 12)
    r_big = max(r_small * 3, min(h, w) // 4)
    near = guided_filter(ys, _gauss(ys, r_small), max(1, r_small // 2))
    far = _gauss(ys, r_big)
    ao = np.clip((near + 0.02) / (far + 0.02), 0.45, 1.0) ** 0.9

    # 6) 光色：光源像素的线性 RGB 均值，按亮度归一
    color = (1.0, 1.0, 1.0)
    if has_light:
        lin = _downsample_rgb(np.clip(rgb, 0.0, 1.0), step)
        lin = lin ** 2.2       # γ2.2 近似足够估色偏
        wgt = src[..., None]
        tot = float(wgt.sum())
        if tot > 1e-6:
            mean = (lin * wgt).sum(axis=(0, 1)) / tot
            lum = float(0.2126 * mean[0] + 0.7152 * mean[1] + 0.0722 * mean[2])
            if lum > 1e-6:
                color = tuple(float(c / lum) for c in mean)

    stats = {
        "source_pct": float(100.0 * (src > 0.05).mean()),
        "mean_depth": float(depth.mean()),
        "ray_energy": float(rays.mean()),
    }
    return Scene(
        y=y,
        irradiance=_upsample(irr, y.shape),
        ao=_upsample(ao, y.shape),
        depth=_upsample(depth, y.shape),
        light_color=color,          # type: ignore[arg-type]
        sources=_upsample(src, y.shape),
        rays=_upsample(rays, y.shape),
        stats=stats,
    )


def adapt(y: np.ndarray, scene: Scene | None = None) -> dict:
    """按中位亮度（曝光）与光源覆盖率算 gmax / 泛光 / 受光权重。"""
    p50 = float(np.percentile(y, 50))
    t = float(np.clip(p50 / 0.60, 0.0, 1.0))

    # 光源太多（整片烟花）时略收峰值，避免 gmax 顶满
    cover = 0.0
    if scene is not None:
        cover = float(np.clip(scene.stats.get("source_pct", 0.0) / 12.0, 0.0, 1.0))

    cfg = {
        "pr": 25,
        "p0": 1.45,
        "p1": 2.90,
        "halo": round(0.16 - 0.06 * t, 3),
        "hr": max(1, round(3 - 2 * t)),
        "gmax": round(min((2.2 - 0.6 * t) * 0.70 * (1.0 - 0.10 * cover), 1.45), 3),
        "lift": 1.0,
        "base_lit": round(0.78 - 0.16 * t, 3),
        "base_spark": 0.45,
        "base_irr": 0.45,
        "base_amb": 0.10,
        "i0": 0.35,
        "i1": 0.85,
        "area_pen": 0.55,
        "area_lo": 0.35,
        "area_hi": 0.85,
        "base_white": 0.18,
    }
    return cfg


def irradiance(y: np.ndarray, *, rgb: np.ndarray | None = None) -> np.ndarray:
    """兼容旧接口：只返回辐照度场。"""
    if rgb is None:
        rgb = np.repeat(y[..., None], 3, axis=2)
    return analyze(rgb, y).irradiance
