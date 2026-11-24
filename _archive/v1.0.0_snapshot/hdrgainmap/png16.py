"""16bit PQ 的 HDR PNG（IHDR 16bit RGB + cICP 标记 + pHYs）。

PNG 规范只允许 8/16bit，没有 10bit；16bit PQ 是超集，能承载同样的精度。
cICP(9,16,0,1) = BT.2020 原色 / PQ 传递 / RGB / 全范围，Chrome/Edge/Win11 认这个。
"""
from __future__ import annotations

import struct
import zlib

import numpy as np

BPP = 6          # 3 通道 × 2 字节


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
        kind, data = min(cands, key=lambda c: np.abs(c[1].astype(np.int8).astype(np.int32)).sum())
        out.append(kind)
        out += data.tobytes()
        prev = cur
    return zlib.compress(bytes(out), 9)


def encode(q16: np.ndarray, dpi: float = 240.0, software: str = "hdr-gainmap") -> bytes:
    """q16: (h,w,3) uint16 的 PQ 码值 → PNG 字节。"""
    h, w, _ = q16.shape
    ppm = int(round(dpi / 0.0254))                       # 每米像素数
    head = (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 16, 2, 0, 0, 0))
            + _chunk(b"cICP", bytes([9, 16, 0, 1]))
            + _chunk(b"tEXt", b"Software\x00" + software.encode("ascii"))
            + _chunk(b"pHYs", struct.pack(">IIB", ppm, ppm, 1)))
    body = _chunk(b"IDAT", _filter(q16.astype(">u2").tobytes(), w, h))
    return head + body + _chunk(b"IEND", b"")


def decode(blob: bytes) -> np.ndarray:
    """反向解析（校验用），返回 (h,w,3) uint16。"""
    assert blob[:8] == b"\x89PNG\r\n\x1a\n"
    pos, idat = 8, b""
    while pos < len(blob):
        length = struct.unpack(">I", blob[pos:pos + 4])[0]
        tag, data = blob[pos + 4:pos + 8], blob[pos + 8:pos + 8 + length]
        if tag == b"IHDR":
            w, h = struct.unpack(">II", data[:8])
        elif tag == b"IDAT":
            idat += data
        pos += 12 + length

    raw = zlib.decompress(idat)
    stride = w * BPP
    out = np.zeros((h, stride), np.uint8)
    prev = np.zeros(stride, np.uint8)
    p = 0
    for y in range(h):
        kind = raw[p]
        line = np.frombuffer(raw[p + 1:p + 1 + stride], np.uint8)
        p += 1 + stride
        if kind == 2:
            cur = (line.astype(np.uint16) + prev).astype(np.uint8)
        elif kind in (1, 3, 4):
            cur = line.copy()
            for i in range(stride):
                a = int(cur[i - BPP]) if i >= BPP else 0
                b = int(prev[i])
                c = int(prev[i - BPP]) if i >= BPP else 0
                if kind == 1:
                    pred = a
                elif kind == 3:
                    pred = (a + b) >> 1
                else:
                    pp = a + b - c
                    pa, pb, pc = abs(pp - a), abs(pp - b), abs(pp - c)
                    pred = a if (pa <= pb and pa <= pc) else (b if pb <= pc else c)
                cur[i] = (int(cur[i]) + pred) & 0xFF
        else:
            cur = line.copy()
        out[y] = cur
        prev = cur
    return out.view(">u2").reshape(h, w, 3)
