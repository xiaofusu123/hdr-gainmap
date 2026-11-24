"""AVIF（AV1 still image）封装：借 ffmpeg 编码，再自己补 nclx 色彩标签。

为什么不用 ffmpeg 直接写：ffmpeg 9.1 的 `avif` muxer 会把 `colr/nclx` 写成
`primaries=2, transfer=2`（unspecified），即使命令行给了 `-color_primaries bt2020
-color_trc smpte2084`。对这些字段来说等于没写，播放器只能靠猜。这里在编码后
定位 `nclx` box 直接改写 7 个字节，成本几乎为零。

12bit：libaom-av1 支持 `yuv420p12le`（profile 2）。SVT-AV1 的常用构建只到 10bit，
所以 12bit 先用 libaom，失败再退 10bit（libsvtav1 更快）。
"""
from __future__ import annotations

import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

import numpy as np

PRIMARIES_BT2020 = 9
MATRIX_BT2020_NCL = 9
TRANSFER = {"PQ": 16, "HLG": 18}

NCLX_LEN = 7                                  # primaries(2) transfer(2) matrix(2) full_range(1)


def available() -> bool:
    """ffmpeg 是否可用（没有就跳过 AVIF，不影响其它格式）。"""
    return shutil.which("ffmpeg") is not None


def _run(cmd: list[str], raw: bytes, out_path: Path) -> None:
    p = subprocess.run(cmd, input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if p.returncode != 0 or not out_path.exists():
        msg = p.stderr.decode("utf-8", "replace").strip().splitlines()
        raise RuntimeError("AVIF 编码失败：" + (msg[-1] if msg else f"ffmpeg rc={p.returncode}"))


def _encode_once(q16: np.ndarray, out_path: Path, transfer: str, bits: int,
                 codec: str, crf: int, cpu_used: int, chroma: str = "444") -> None:
    h, w, _ = q16.shape
    pix_fmt = f"yuv{chroma}p{bits}le"
    cmd = [
        "ffmpeg", "-v", "error", "-f", "rawvideo", "-pix_fmt", "rgb48le",
        "-s", f"{w}x{h}", "-r", "1", "-i", "pipe:0",
        "-vf", f"scale=out_color_matrix=bt2020nc:out_range=tv,format={pix_fmt}",
        "-c:v", codec, "-pix_fmt", pix_fmt, "-still-picture", "1",
        "-crf", str(crf), "-cpu-used", str(cpu_used),
        "-f", "avif", str(out_path), "-y",
    ]
    _run(cmd, q16.astype("<u2").tobytes(), out_path)


def patch_nclx(blob: bytes, transfer: str = "PQ") -> bytes:
    """把 colr/nclx 里的 primaries/transfer/matrix 改成 BT.2020 + PQ/HLG。"""
    idx = blob.find(b"nclx")
    if idx < 0:
        raise ValueError("AVIF 里没有 colr/nclx box")
    want = struct.pack(">HHHB", PRIMARIES_BT2020, TRANSFER[transfer.upper()],
                       MATRIX_BT2020_NCL, 0)      # full_range=0（limited/tv）
    return blob[:idx + 4] + want + blob[idx + 4 + NCLX_LEN:]


def _iter_boxes(buf: bytes, start: int, end: int):
    """遍历 [start, end) 里的 ISOBMFF box，产出 (type, box_start, header_len, box_end)。"""
    pos = start
    while pos + 8 <= end:
        size = struct.unpack(">I", buf[pos:pos + 4])[0]
        typ = buf[pos + 4:pos + 8]
        hl = 8
        if size == 1:
            if pos + 16 > end:
                break
            size = struct.unpack(">Q", buf[pos + 8:pos + 16])[0]
            hl = 16
        elif size == 0:
            size = end - pos
        if size < hl or pos + size > end:
            break
        yield typ, pos, hl, pos + size
        pos += size


def _tiff_dpi(dpi: float) -> bytes:
    """最小 TIFF/Exif：只要 XResolution/ YResolution / ResolutionUnit。"""
    x_num, x_den = int(round(dpi * 100)), 100      # 有理数，保留两位小数（240.00 dpi）
    entries = 3
    ifd_off = 8
    data_off = ifd_off + 2 + 12 * entries + 4
    out = bytearray(b"II*\x00" + struct.pack("<I", ifd_off) + struct.pack("<H", entries))
    for tag, typ, count, val in ((0x011A, 5, 1, data_off), (0x011B, 5, 1, data_off + 8),
                                 (0x0128, 3, 1, None)):
        out += struct.pack("<HHI", tag, typ, count)
        out += b"\x02\x00\x00\x00" if val is None else struct.pack("<I", val)
    out += struct.pack("<I", 0)                       # next IFD
    out += struct.pack("<II", x_num, x_den) + struct.pack("<II", x_num, x_den)
    return bytes(out)


def _parse_iloc(blob: bytes, start: int, hl: int, end: int) -> tuple[int, list[dict], int, int, int, int]:
    """解析 iloc，返回 (version, entries, offset_size, length_size, base_size, index_size)。"""
    p = start + hl
    ver = blob[p]
    p += 4
    off_size, len_size = blob[p] >> 4, blob[p] & 0x0F
    p += 1
    base_size, index_size = blob[p] >> 4, blob[p] & 0x0F
    p += 1
    count = struct.unpack(">H", blob[p:p + 2])[0]
    p += 2
    entries = []
    for _ in range(count):
        item_id = struct.unpack(">H", blob[p:p + 2])[0]
        p += 2
        method = 0
        if ver in (1, 2):
            method = struct.unpack(">H", blob[p:p + 2])[0] & 0x0F
            p += 2
        dref = struct.unpack(">H", blob[p:p + 2])[0]
        p += 2
        base = int.from_bytes(blob[p:p + base_size], "big") if base_size else 0
        p += base_size
        n_ext = struct.unpack(">H", blob[p:p + 2])[0]
        p += 2
        exts = []
        for _ in range(n_ext):
            off = int.from_bytes(blob[p:p + off_size], "big")
            p += off_size
            ln = int.from_bytes(blob[p:p + len_size], "big")
            p += len_size
            exts.append([off, ln])
        entries.append({"id": item_id, "method": method, "dref": dref, "base": base, "exts": exts})
    return ver, entries, off_size, len_size, base_size, index_size


def _build_iloc(ver: int, entries: list[dict], off_size: int, len_size: int,
                base_size: int, index_size: int) -> bytes:
    body = bytearray([ver, 0, 0, 0, (off_size << 4) | len_size, (base_size << 4) | index_size])
    body += struct.pack(">H", len(entries))
    for e in entries:
        body += struct.pack(">H", e["id"])
        if ver in (1, 2):
            body += struct.pack(">H", e["method"])
        body += struct.pack(">H", e["dref"])
        if base_size:
            body += e["base"].to_bytes(base_size, "big")
        body += struct.pack(">H", len(e["exts"]))
        for off, ln in e["exts"]:
            body += off.to_bytes(off_size, "big")
            body += ln.to_bytes(len_size, "big")
    return struct.pack(">I", 8 + len(body)) + b"iloc" + bytes(body)


def add_exif_dpi(blob: bytes, dpi: float | None) -> bytes:
    """给 AVIF 塞一个 Exif item，让资源管理器/看图软件能读到 DPI。

    ffmpeg 的 avif muxer 不写 Exif，Windows 面板就会退回默认 72 dpi。这里手工构造
    `infe(Exif)` + `iloc` + `iref(cdsc)`，数据追加到文件末尾，meta 变大后把已有
    item 的绝对偏移整体平移。
    """
    if not dpi or dpi <= 0:
        return blob
    top = {t: (s, hl, e) for t, s, hl, e in _iter_boxes(blob, 0, len(blob))}
    if b"meta" not in top or b"mdat" not in top:
        return blob

    meta_s, meta_hl, meta_e = top[b"meta"]
    mdat_s, mdat_hl, mdat_e = top[b"mdat"]
    mdat_data = mdat_s + mdat_hl
    children = [(t, blob[s:e]) for t, s, hl, e in _iter_boxes(blob, meta_s + meta_hl + 4, meta_e)]

    primary = 1
    for t, box in children:
        if t == b"pitm":
            ver = box[8]
            primary = struct.unpack(">H", box[12:14])[0] if ver == 0 else struct.unpack(">I", box[12:16])[0]

    # HEIF Exif item：u32 偏移（指向 TIFF 头）+ "Exif\0\0" + TIFF。
    # libheif 写的是 offset=6（4 字节偏移字段 + "Exif\0\0" 之后），Windows 的
    # 属性处理器按这个布局找 TIFF，缺了前缀会整张图的属性全部读不出来。
    payload = struct.pack(">I", 6) + b"Exif\x00\x00" + _tiff_dpi(dpi)
    exif_id = primary + 1000                      # 用主 item 之外的号段，避免撞号

    delta = 0
    exif_abs = 0
    for _ in range(3):
        out_children: list[bytes] = []
        for t, box in children:
            if t == b"iloc":
                ver, entries, off_size, len_size, base_size, index_size = _parse_iloc(box, 0, 8, len(box))
                for e in entries:
                    if e["method"] == 0:
                        for ext in e["exts"]:
                            if e["base"] + ext[0] >= mdat_data:
                                ext[0] += delta
                entries.append({"id": exif_id, "method": 0, "dref": 0, "base": 0,
                                "exts": [[exif_abs, len(payload)]]})
                out_children.append(_build_iloc(ver, entries, off_size, len_size, base_size, index_size))
            elif t == b"iinf":
                ver = box[8]
                cnt_off = 12
                if ver == 0:
                    count = struct.unpack(">H", box[cnt_off:cnt_off + 2])[0]
                    count_bytes = struct.pack(">H", count + 1)
                    rest_start = cnt_off + 2
                else:
                    count = struct.unpack(">I", box[cnt_off:cnt_off + 4])[0]
                    count_bytes = struct.pack(">I", count + 1)
                    rest_start = cnt_off + 4
                # infe box 的 version 必须是 2：v0/v1 没有 item_type 字段，
                # 写成 0 会被解析器当成未知类型而忽略（Exif 就永远读不到）。
                infe_payload = struct.pack(">I", 2 << 24) + struct.pack(">HH", exif_id, 0) + b"Exif\x00"
                infe = struct.pack(">I", 8 + len(infe_payload)) + b"infe" + infe_payload
                content = box[8:cnt_off] + count_bytes + box[rest_start:] + infe
                out_children.append(struct.pack(">I", 8 + len(content)) + b"iinf" + content)
            else:
                out_children.append(box)
        # HEIF 的 iref 子条目本身是 box（size + 'cdsc' + from + count + to...），
        # 不是 ISOBMFF 那种「先 count 再裸条目」的写法，ffmpeg 只认这一种。
        iref_body = struct.pack(">I", 0) + struct.pack(">I", 14) + b"cdsc" + \
            struct.pack(">HHH", exif_id, 1, primary)
        iref = struct.pack(">I", 8 + len(iref_body)) + b"iref" + iref_body
        has_iref = any(t == b"iref" for t, _ in children)
        merged: list[bytes] = []
        for t, box in zip([c[0] for c in children], out_children):
            merged.append(box)
            if t == b"iinf" and not has_iref:
                merged.append(iref)
        new_meta_len = meta_hl + 4 + sum(len(b) for b in merged)
        new_delta = new_meta_len - (meta_e - meta_s)
        # Exif 数据追加在 mdat 末尾（顶层不能留裸数据，否则 demuxer 会当成畸形 box）
        exif_abs = mdat_e + new_delta
        if new_delta == delta:
            break
        delta = new_delta

    meta = struct.pack(">I", new_meta_len) + b"meta" + blob[meta_s + 8:meta_s + 12] + b"".join(merged)
    mdat_size = (mdat_e - mdat_s) + len(payload)
    mdat_head = struct.pack(">I", mdat_size) + b"mdat" + blob[mdat_s + 8:mdat_s + mdat_hl]
    return blob[:meta_s] + meta + mdat_head + blob[mdat_s + mdat_hl:mdat_e] + payload


def read_nclx(blob: bytes) -> dict:
    idx = blob.find(b"nclx")
    if idx < 0:
        return {}
    prim, trc, mat, full = struct.unpack(">HHHB", blob[idx + 4:idx + 11])
    return {"primaries": prim, "transfer": trc, "matrix": mat, "full_range": full}


def pixi_bits(blob: bytes) -> tuple[int, ...]:
    idx = blob.find(b"pixi")
    if idx < 0:
        return ()
    body = blob[idx + 8:]
    n = body[0]
    return tuple(body[1:1 + n])


def ispe_size(blob: bytes) -> tuple[int, int]:
    """返回 (width, height)：meta 里第一个 ispe（primary item 尺寸）。"""
    idx = blob.find(b"ispe")
    if idx < 0:
        raise ValueError("AVIF 里没有 ispe box")
    w = struct.unpack(">I", blob[idx + 8:idx + 12])[0]
    h = struct.unpack(">I", blob[idx + 12:idx + 16])[0]
    return w, h


def decode(blob: bytes) -> np.ndarray:
    """AVIF → rgb48 数组（BT.2020 矩阵反变换，含 4:2:0 上采样）。

    AVIF 是 ISOBMFF，ffmpeg 需要可 seek 的输入，所以走临时文件而不是管道。
    """
    if not available():
        raise RuntimeError("找不到 ffmpeg，无法解码 AVIF")
    w, h = ispe_size(blob)
    cmd = ["ffmpeg", "-v", "error", "-i", "{in}", "-frames:v", "1",
           "-vf", "scale=in_color_matrix=bt2020nc:in_range=tv",
           "-f", "rawvideo", "-pix_fmt", "rgb48le", "pipe:1"]
    with tempfile.TemporaryDirectory() as tmp:
        p = Path(tmp) / "x.avif"
        p.write_bytes(blob)
        cmd[cmd.index("{in}")] = str(p)
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    if proc.returncode != 0:
        raise RuntimeError("AVIF 解码失败：" + proc.stderr.decode("utf-8", "replace").strip()[:200])
    arr = np.frombuffer(proc.stdout, dtype="<u2")
    if arr.size != w * h * 3:
        raise ValueError(f"AVIF 解码长度异常：{arr.size} != {w*h*3}")
    return arr.reshape(h, w, 3)


def encode(q16: np.ndarray, transfer: str = "PQ", bits: int = 12, quality: int = 100,
           cpu_used: int = 6, chroma: str = "444",
           dpi: float | None = None) -> tuple[bytes, int]:
    """q16 (h,w,3) → AVIF 字节。返回 (blob, 实际位深)。

    默认 12bit 4:4:4（AV1 profile 2）：插画色边锐利，4:2:0 会把彩色高光糊掉。
    12bit 失败时依次退 4:2:0 12bit → 10bit 4:4:4 → 10bit 4:2:0。
    quality 100 → crf 0（视觉无损），90 → crf 6，80 → crf 13。
    """
    if not available():
        raise RuntimeError("找不到 ffmpeg，无法生成 AVIF")
    transfer = transfer.upper()
    if transfer not in TRANSFER:
        raise ValueError(f"不支持的传递函数：{transfer}")
    crf = max(0, min(63, round((100 - quality) * 0.63)))

    attempts: list[tuple[int, str, str]] = []
    if bits >= 12:
        attempts.append((12, "libaom-av1", "444"))
        attempts.append((12, "libaom-av1", "420"))
    attempts.append((10, "libaom-av1", "444"))
    attempts.append((10, "libsvtav1", "420"))       # SVT 只有 4:2:0，但最快

    last: Exception | None = None
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "x.avif"
        for b, codec, chroma_try in attempts:
            try:
                if out.exists():
                    out.unlink()
                _encode_once(q16, out, transfer, b, codec, crf, cpu_used, chroma_try)
                blob = out.read_bytes()
                return add_exif_dpi(patch_nclx(blob, transfer), dpi), b
            except Exception as exc:            # noqa: BLE001  换下一个编码器
                last = exc
    raise RuntimeError(f"AVIF 编码全部失败：{last}")
