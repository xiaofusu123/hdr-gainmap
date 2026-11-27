"""共享查找表：sRGB 解码、tint、exp2、PQ。

同一张表由 CPU 与 GPU 以同一段插值代码采样，逐像素结果可对齐到 1 个码值以内；
GPU 侧只做乘加，不再调 pow。
"""
from __future__ import annotations

import numpy as np

# 每张表的段数（结点数 = 段数 + 1）
N_SRGB = 4096
N_TINT = 2048
N_EXP2 = 4096
N_PQ = 4096

TINT_MAX = 2.0        # lin/lum 的采样上限；超出部分按端点值
EXP2_MAX = 4.0        # log2 增益的采样上限；1.45*1.25 也到不了这里


def _knots(n: int, hi: float) -> np.ndarray:
    return np.linspace(0.0, hi, n + 1, dtype=np.float64)


def _srgb_table() -> np.ndarray:
    v = _knots(N_SRGB, 1.0)
    lin = np.where(v <= 0.04045, v / 12.92,
                   np.power((v + 0.055) / 1.055, 2.4))
    return lin.astype(np.float32)


def _tint_table() -> np.ndarray:
    r = _knots(N_TINT, TINT_MAX)
    return np.clip(np.power(np.maximum(r, 0.0), 0.35), 0.85, 1.15).astype(np.float32)


def _exp2_table() -> np.ndarray:
    e = _knots(N_EXP2, EXP2_MAX)
    return np.power(2.0, e).astype(np.float32)


def _pq_table() -> np.ndarray:
    # 按 nits^(1/4) 均匀取点：近黑处 PQ 斜率极大，均匀取点误差压不住
    u = _knots(N_PQ, 1.0)
    y = np.clip((u ** 4), 0.0, 1.0) ** (2610.0 / 16384.0)
    n = ((3424.0 / 4096.0 + (2413.0 / 4096.0 * 32.0) * y)
         / (1.0 + (2392.0 / 4096.0 * 32.0) * y))
    return np.clip(np.power(n, 2523.0 / 4096.0 * 128.0), 0.0, 1.0).astype(np.float32)


SRGB = _srgb_table()
TINT = _tint_table()
EXP2 = _exp2_table()
PQ = _pq_table()

_STEP = {
    "srgb": np.float32(N_SRGB),
    "tint": np.float32(N_TINT / TINT_MAX),
    "exp2": np.float32(N_EXP2 / EXP2_MAX),
    "pq": np.float32(N_PQ),
}


def _sample(table: np.ndarray, t: np.ndarray) -> np.ndarray:
    """线性插值；t 已乘过步长，索引夹到 [0, N-1]。"""
    i = np.minimum(np.floor(t), np.float32(len(table) - 2)).astype(np.int32)
    f = t - i.astype(np.float32)
    return table[i] + f * (table[i + 1] - table[i])


def srgb_to_linear(v):
    """0..1 的 sRGB 编码值 → 线性光。"""
    return _sample(SRGB, np.clip(v, 0.0, 1.0).astype(np.float32) * _STEP["srgb"])


def tint(ratio):
    """lin/lum 的 0.35 次幂，夹到 [0.85, 1.15]。"""
    r = np.clip(np.asarray(ratio, dtype=np.float32), 0.0, TINT_MAX)
    return _sample(TINT, r * _STEP["tint"])


def exp2(expo):
    """2^x，用于把 log2 增益变回线性倍数。"""
    e = np.clip(np.asarray(expo, dtype=np.float32), 0.0, EXP2_MAX)
    return _sample(EXP2, e * _STEP["exp2"])


def pq(nits):
    """nits → PQ 编码值；索引域是 (nits/10000)^(1/4)。"""
    u = np.sqrt(np.sqrt(np.clip(np.asarray(nits, dtype=np.float32), 0.0, 10000.0) / 10000.0))
    return _sample(PQ, u * _STEP["pq"])


BT709_LUMA = (0.2126, 0.7152, 0.0722)
TO_2020 = (
    (0.627404, 0.329283, 0.043313),
    (0.069097, 0.919540, 0.011362),
    (0.016391, 0.088013, 0.895595),
)


def to_bt2020(lin: np.ndarray) -> np.ndarray:
    """逐元素写开，乘法顺序与着色器一致（不依赖 BLAS）。"""
    r, g, b = lin[..., 0], lin[..., 1], lin[..., 2]
    out = np.empty_like(lin, dtype=np.float32)
    for i, (a, c, d) in enumerate(TO_2020):
        out[..., i] = r * np.float32(a) + g * np.float32(c) + b * np.float32(d)
    return out
