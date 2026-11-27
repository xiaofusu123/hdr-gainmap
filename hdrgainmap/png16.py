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


def _filter(raw: bytes, w: int, h: int) -> bytes:
    """逐行挑残差绝对值最小的滤波（0-4），再 zlib 压缩。"""
    stride = w * BPP
    rows = np.frombuffer(raw, np.uint8).reshape(h, stride)
    prev = np.zeros(stride, np.uint8)
    out = bytearray()
    for y in range(h):
        cur = rows[y]
        left, upleft = _shift(cur, BPP), _shift(prev, BPP)
        cands = [
            (0, cur),
            (1, (cur - left).astype(np.uint8)),
            (2, (cur - prev).astype(np.uint8)),
            (3, (cur - ((left.astype(np.int16) + prev.astype(np.int16)) >> 1).astype(np.uint8)).astype(np.uint8)),
            (4, (cur - _paeth(left, prev, upleft)).astype(np.uint8)),
        ]
        kind, data = min(cands, key=lambda c: np.abs(c[1].view(np.int8)).sum())
        out.append(kind)
        out += data.tobytes()
        prev = cur
    return zlib.compress(bytes(out), 9)


def encode(q16: np.ndarray, dpi: float = 240.0, software: str = "hdr-gainmap",
           peak_nits: float | None = None) -> bytes:
    """q16 (h,w,3) uint16 PQ 码值 → PNG 字节；给 peak_nits 时写 mDCV/cLLI。"""
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
    body = _chunk(b"IDAT", _filter(q16.astype(">u2").tobytes(), w, h))
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
