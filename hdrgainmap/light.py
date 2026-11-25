"""光的传播 / 衍射 / 漫反射近似。

为什么需要这一层：插画里的"发光"不只是像素亮，而是**光从光源出发、照亮了周围**。
只按亮度分位判断，会把光球旁边的地面和光球本身当成两回事；这里补一个
辐照度场（irradiance），让光从高光源向外扩散、随距离衰减，再乘上表面反射率，
从而得到"被这束光照到的区域"——远离光源的表面依然不该有增益。

模型（2D 图像的近似，不做几何重建）：

    S(x)  = 光源强度          —— 高亮像素（高于 p_hi 分位）是光源
    E(x)  = Σ_k w_k · G_{σ_k} * S   —— 多尺度扩散：σ 逐级加倍、权重按幂律衰减，
                                    于是核心是高斯、尾部近似 1/r^p，用来模拟
                                    漫反射/散射的"辉光"（衍射条纹在 2D 里也是这个形状）
    R(x)  = E(x) · (a + (1-a)·Y(x))  —— 漫反射：同样的照度，浅色表面反射得多
    irr(x)= smoothstep(t0, t1, R)

全部在降采样图上做：光照场本身就是低频量，1/8 分辨率足够，且大图也不慢。
"""

from __future__ import annotations

import numpy as np

from .gainmap import _gauss  # 同一个盒式高斯，避免两份实现漂移


def _downsample(y: np.ndarray, max_side: int = 900) -> tuple[np.ndarray, int]:
    """按整数倍块平均降采样（块平均 = 抗锯齿，比抽点稳）。"""
    h, w = y.shape
    step = max(1, int(round(max(h, w) / max_side)))
    if step <= 1:
        return y, 1
    hh, ww = h // step * step, w // step * step
    blk = y[:hh, :ww].reshape(hh // step, step, ww // step, step).mean(axis=(1, 3))
    return blk.astype(np.float32), step


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


def irradiance(y: np.ndarray, *, p_hi: float = 96.0, a_refl: float = 0.25,
               scales: int = 4, sigma_frac: float = 0.06,
               tail_power: float = 0.75) -> np.ndarray:
    """由亮度图估计"这张图里被光照射到的程度"，返回 [0,1] 的平滑场。"""
    ys, _ = _downsample(y)
    hi = float(np.percentile(ys, p_hi))
    # 光源：高出 p_hi 的部分；减去基线再归一，避免整体亮度影响
    src = np.clip((ys - hi) / max(1e-3, float(ys.max()) - hi), 0.0, 1.0) ** 1.5
    if float(src.max()) <= 0:
        return np.zeros(y.shape, dtype=np.float32)

    base = max(6.0, min(ys.shape) * sigma_frac)
    acc = np.zeros_like(ys)
    wsum = 0.0
    for k in range(scales):
        sig = base * (2.0 ** k)
        # 权重按 σ^-(1+tail_power)：核心（小 σ）占比高，尾部衰减像 1/r^p 的散射光
        w = sig ** (-1.0 - tail_power)
        acc += w * _gauss(src, int(round(sig)))
        wsum += w
    field = acc / max(wsum, 1e-6)

    # 漫反射：同一照度下，浅色表面反射得多（暗表面吸收）
    field = field * (a_refl + (1.0 - a_refl) * ys)
    field = field / max(float(np.percentile(field, 99.5)), 1e-6)
    field = np.clip(field, 0.0, 1.0)
    return _upsample(field, y.shape)
