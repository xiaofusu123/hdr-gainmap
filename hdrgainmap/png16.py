"""16bit PQ 的 HDR PNG：IHDR 16bit RGB + cICP(9,16,0,1) + mDCV/cLLI + pHYs。

PNG 只有 8/16bit；16bit PQ 在 0–203 nits 约有 38055 个码值，精度足够。
mDCV 描述 BT.2020 / 1000 nits 母版，cLLI 写实际峰值；缺 cICP 时查看器按 sRGB 解释。
"""
from __future__ import annotations

import struct
import zlib

import numpy as np

BPP = 6          # 3 通道 × 2 字节

# BT.2020 原色与 D65（单位 0.00002，mDCV 规定的定点）
_MDCV_PRIMARIES = (35400, 14600, 8500, 39850, 6550, 2300, 15635, 16450)
_MDCV_MAX_NITS = 1000.0
_MDCV_MIN_NITS = 0.005


def _chunk(tag: bytes, data: bytes) -> bytes:
    return (struct.pack(">I", len(data)) + tag + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))


def _shift(arr: np.ndarray, n: int) -> np.ndarray:
    """整体右移 n 字节，左侧补 0（滤波的"左邻/左上邻"）。"""
    out = np.zeros_like(arr)
    out[n:] = arr[:-n]
    return out


def _paeth(a: np.ndarray, b: np.ndarray, c: np.ndarray) -> np.ndarray:
    p = a.astype(np.int16) + b.astype(np.int16) - c.astype(np.int16)
    pa, pb, pc = np.abs(p - a), np.abs(p - b), np.abs(p - c)
    return np.where((pa <= pb) & (pa <= pc), a, np.where(pb <= pc, b, c)).astype(np.uint8)


def _filter_block(rows: np.ndarray, stride: int, y0: int, y1: int) -> bytes:
    """整块一起选滤波：每行只依赖原始行，行与行之间无递推，可并行。"""
    cur = rows[y0:y1]
    block = y1 - y0
    if y0 == 0:                                  # 首行没有上一行，其余行往上取
        prev = np.zeros((block, stride), np.uint8)
        prev[1:] = rows[0:block - 1]
    else:
        prev = rows[y0 - 1:y1 - 1]
    left, upleft = _shift_axis(cur), _shift_axis(prev)
    mid = ((left.astype(np.int16) + prev.astype(np.int16)) >> 1).astype(np.uint8)
    cands = np.stack([
        cur,
        (cur - left).astype(np.uint8),
        (cur - prev).astype(np.uint8),
        (cur - mid).astype(np.uint8),
        (cur - _paeth(left, prev, upleft)).astype(np.uint8),
    ])
    cost = np.abs(cands.view(np.int8)).sum(axis=2)          # (5, block)
    kind = cost.argmin(axis=0).astype(np.uint8)             # (block,)
    picked = cands[kind, np.arange(block)]
    out = np.empty((block, stride + 1), np.uint8)
    out[:, 0] = kind
    out[:, 1:] = picked
    return out.tobytes()


def _shift_axis(arr: np.ndarray) -> np.ndarray:
    """沿行内右移 BPP 字节（左邻），用于整块向量化。"""
    out = np.zeros_like(arr)
    out[:, BPP:] = arr[:, :-BPP]
    return out


def _filter(raw: bytes, w: int, h: int, jobs: int = 1) -> bytes:
    """逐行挑残差绝对值最小的滤波（0-4），再 zlib 压缩；jobs>1 时按块并行。"""
    stride = w * BPP
    rows = np.frombuffer(raw, np.uint8).reshape(h, stride)
    if jobs <= 1 or h < 64:
        comp = zlib.compressobj(9)
        parts = [_filter_block(rows, stride, y, y + 1) for y in range(h)]
        return b"".join(comp.compress(p) for p in parts) + comp.flush()

    # 每块约 48MB 候选：5 份残差 + 输出行
    block = max(1, (48 << 20) // max(5 * stride, 1))
    ranges = [(y, min(h, y + block)) for y in range(0, h, block)]
    comp = zlib.compressobj(9)
    out = bytearray()
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=jobs) as ex:
        for part in ex.map(lambda r: _filter_block(rows, stride, *r), ranges):
            out += comp.compress(part)
    out += comp.flush()
    return bytes(out)



def encode(q16: np.ndarray, dpi: float = 240.0, software: str = "hdr-gainmap",
           peak_nits: float | None = None, jobs: int = 1) -> bytes:
    """q16 (h,w,3) uint16 PQ 码值 → PNG 字节；jobs>1 时并行滤波。"""
    h, w, _ = q16.shape
    ppm = int(round(dpi / 0.0254))                       # 每米像素数
    chunks = (b"\x89PNG\r\n\x1a\n"
              + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 2, 0, 0, 0))
              + _chunk(b"cICP", bytes([9, 16, 0, 1]))
              + _chunk(b"mDCV", struct.pack(">8HII", *_MDCV_PRIMARIES,
                                            int(round(_MDCV_MAX_NITS / 1e-4)),
                                            int(round(_MDCV_MIN_NITS / 1e-4))))
              + _chunk(b"tEXt", b"Software\x00" + software.encode("ascii"))
              + _chunk(b"pHYs", struct.pack(">IIB", ppm, ppm, 1)))
    if peak_nits is not None and peak_nits > 0:
        units = int(round(float(peak_nits) / 1e-4))
        chunks += _chunk(b"cLLI", struct.pack(">II", units, units))
    body = _chunk(b"IDAT", _filter(q16.astype(">u2").tobytes(), w, h, jobs))
    return chunks + body + _chunk(b"IEND", b"")


def _ihdr_size(blob: bytes) -> tuple[int, int]:
    w, h = struct.unpack(">II", blob[16:24])
    return w, h


def _unfilter_fast(raw: bytes, w: int, h: int) -> np.ndarray:
    """向量化反滤波：0/1/2 全向量化，3/4 仍需沿行内依赖逐字节递推。"""
    stride = w * BPP
    out = np.empty((h, stride), np.uint8)
    prev = np.zeros(stride, np.uint8)
    for y in range(h):
        base = y * (stride + 1)
        kind = raw[base]
        line = np.frombuffer(raw, np.uint8, stride, base + 1)
        if kind == 0:
            cur = line.copy()
        elif kind == 1:                                   # Sub：按 BPP 分组做模 256 累加
            pad = (-stride) % BPP
            buf = line if not pad else np.concatenate([line, np.zeros(pad, np.uint8)])
            acc = np.cumsum(buf.reshape(-1, BPP).astype(np.uint32), axis=0) & 0xFF
            cur = acc.astype(np.uint8).reshape(-1)[:stride]
        elif kind == 2:                                   # Up：整行直接相加
            cur = (line.astype(np.uint16) + prev).astype(np.uint8)
        else:                                             # Average / Paeth：顺序依赖
            cur = line.copy()
            for i in range(stride):
                a = int(cur[i - BPP]) if i >= BPP else 0
                b = int(prev[i])
                c = int(prev[i - BPP]) if i >= BPP else 0
                if kind == 3:
                    pred = (a + b) >> 1
                else:
                    pp = a + b - c
                    pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                    pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                cur[i] = (int(cur[i]) + pred) & 0xFF
        out[y] = cur
        prev = cur
    return out


def decode(blob: bytes, fast: bool = True) -> np.ndarray:
    """反向解析，返回 (h,w,3) uint16；fast 时优先借 ffmpeg 解 rgb48le。"""
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    if fast:
        import shutil
        import subprocess
        ff = shutil.which("ffmpeg")
        if ff:
            try:
                w, h = _ihdr_size(blob)
                p = subprocess.run(
                    [ff, "-v", "error", "-i", "pipe:0", "-frames:v", "1",
                     "-f", "rawvideo", "-pix_fmt", "rgb48le", "pipe:1"],
                    input=blob, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                    check=True)
                arr = np.frombuffer(p.stdout, dtype="<u2")
                if arr.size == w * h * 3:
                    return arr.reshape(h, w, 3).astype(np.uint16)
            except (OSError, subprocess.SubprocessError):
                pass

    pos, idat = 8, bytearray()
    w = h = 0
    while pos + 8 <= len(blob):
        length = struct.unpack(">I", blob[pos:pos + 4])[0]
        tag, data = blob[pos + 4:pos + 8], blob[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            w, h = struct.unpack(">II", data[:8])
        elif tag == b"IDAT":
            idat += data
        pos += 12 + length

    raw = zlib.decompress(bytes(idat))
    stride = w * BPP
    if len(raw) != h * (stride + 1):
        raise ValueError(f"IDAT 解压长度 {len(raw)} != {h * (stride + 1)}")
    out = _unfilter_fast(raw, w, h)
    return out.view(">u2").reshape(h, w, 3)
