"""色彩空间与传递函数：sRGB、PQ（ST 2084）、HLG（BT.2100）、BT.709→BT.2020。"""
from __future__ import annotations

import numpy as np

# PQ 常量（SMPTE ST 2084）
PQ_M1 = 2610.0 / 16384.0
PQ_M2 = 2523.0 / 4096.0 * 128.0
PQ_C1 = 3424.0 / 4096.0
PQ_C2 = 2413.0 / 4096.0 * 32.0
PQ_C3 = 2392.0 / 4096.0 * 32.0

# BT.709 → BT.2020 原色矩阵（线性光）
BT709_TO_2020 = np.array([
    [0.6274, 0.3293, 0.0433],
    [0.0691, 0.9195, 0.0114],
    [0.0164, 0.0880, 0.8956],
], dtype=np.float32)


def srgb_to_linear(x: np.ndarray) -> np.ndarray:
    """sRGB 编码值 [0,1] → 线性光。"""
    x = np.asarray(x, dtype=np.float32)
    return np.where(x <= 0.04045, x / 12.92, ((x + 0.055) / 1.055) ** 2.4).astype(np.float32)


def linear_to_srgb(x: np.ndarray) -> np.ndarray:
    """线性光 → sRGB 编码值，输出裁剪到 [0,1]。"""
    x = np.clip(np.asarray(x, dtype=np.float32), 0.0, 1.0)
    return np.where(x <= 0.0031308, x * 12.92, 1.055 * x ** (1 / 2.4) - 0.055).astype(np.float32)


def pq_oetf(luminance_nits: np.ndarray) -> np.ndarray:
    """绝对亮度（nits）→ PQ 码值 [0,1]。"""
    y = np.clip(np.asarray(luminance_nits, dtype=np.float32) / 10000.0, 0.0, 1.0) ** PQ_M1
    return (((PQ_C1 + PQ_C2 * y) / (1.0 + PQ_C3 * y)) ** PQ_M2).astype(np.float32)


def pq_eotf(code: np.ndarray) -> np.ndarray:
    """PQ 码值 [0,1] → 绝对亮度（nits）。"""
    p = np.clip(np.asarray(code, dtype=np.float64), 0.0, 1.0)
    p = p ** (1.0 / PQ_M2)                     # 先开 1/m2，再解有理式
    ratio = np.clip(p - PQ_C1, 0.0, None) / np.maximum(PQ_C2 - PQ_C3 * p, 1e-12)
    return (10000.0 * ratio ** (1.0 / PQ_M1)).astype(np.float32)


def hlg_oetf(relative_linear: np.ndarray, reference_peak: float = 1000.0) -> np.ndarray:
    """线性（1.0 = SDR 白）→ HLG 信号（reference_peak 归一）。"""
    e = np.clip(np.asarray(relative_linear, dtype=np.float32) * (1.0 / reference_peak), 0.0, 1.0)
    return np.where(
        e <= 1.0 / 12.0,
        np.sqrt(3.0 * e),
        0.17883277 * np.log(np.maximum(12.0 * e - 0.28466892, 1e-6)) + 0.55991073,
    ).astype(np.float32)


def to_bt2020(rgb_linear: np.ndarray) -> np.ndarray:
    """线性 BT.709/Rec.709 原色的 HDR → BT.2020 原色。"""
    return (rgb_linear @ BT709_TO_2020.T).astype(np.float32)
