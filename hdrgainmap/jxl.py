"""24bit HDR JPEG XL：PQ/HLG 码值经 PFM 交给 cjxl 无损封装。

cjxl 不读 PNG 的 PQ 标记；PFM 的 float32 正好装下 24bit 整数码值，
``--override_bitdepth=24`` 避免伪高位深。DPI 写容器 Exif box（JXL 无原生分辨率字段）。
工具查找：HDRGAINMAP_CJXL / HDRGAINMAP_JXLINFO，其次 PATH。
"""
from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import tempfile
from pathlib import Path

CJXL_ENV = "HDRGAINMAP_CJXL"
JXLINFO_ENV = "HDRGAINMAP_JXLINFO"

CONTAINER_MAGIC = bytes.fromhex("0000000c4a584c200d0a870a")
CODESREAM_MAGIC = b"\xff\x0a"

_TRANSFER_NAMES = {"PQ": "Rec2100PQ", "HLG": "Rec2100HLG"}


def _find(env_name: str, exe: str) -> Path | None:
    override = os.environ.get(env_name)
    if override:
        path = Path(override)
        return path if path.is_file() else None
    found = shutil.which(exe)
    return Path(found) if found else None


def cjxl_path() -> Path | None:
    """返回 cjxl 可执行文件；不存在时返回 None，由调用方决定是否报错。"""
    return _find(CJXL_ENV, "cjxl")


def jxlinfo_path() -> Path | None:
    """返回 jxlinfo 可执行文件；校验位深/传递函数时使用。"""
    return _find(JXLINFO_ENV, "jxlinfo")


def available() -> bool:
    """cjxl 可用才允许请求 jxl 格式。"""
    return cjxl_path() is not None


def encode(pfm: str | Path, out: str | Path, transfer: str, *,
           effort: int = 7, dpi: float | None = None) -> None:
    """PQ/HLG 浮点 PFM → 24bit JPEG XL（无损）；给 dpi 时写 Exif box。"""
    transfer = transfer.upper()
    if transfer not in _TRANSFER_NAMES:
        raise ValueError(f"JXL 传递函数必须是 PQ 或 HLG，收到 {transfer}")
    cjxl = cjxl_path()
    if cjxl is None:
        raise RuntimeError(
            "找不到 cjxl。请安装 libjxl 并把 cjxl 放进 PATH，"
            f"或设置环境变量 {CJXL_ENV} 指向 cjxl 可执行文件。")
    cmd = [
        str(cjxl), str(pfm), str(out),
        "--override_bitdepth=24",
        "-x", f"color_space={_TRANSFER_NAMES[transfer]}",
        "-e", str(max(1, min(10, int(effort)))),
        "-d", "0",
        "--compress_boxes=0",                         # Exif 明文，免 Brotli 依赖
        "--quiet",
    ]
    exif_path: Path | None = None
    if dpi:
        fd, name = tempfile.mkstemp(suffix=".exif", dir=str(Path(out).parent))
        os.close(fd)
        exif_path = Path(name)
        exif_path.write_bytes(exif_tiff(dpi))
        cmd += ["-x", f"exif={exif_path}"]
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True)
    finally:
        if exif_path is not None:
            exif_path.unlink(missing_ok=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        tail = detail[-1] if detail else f"exit {proc.returncode}"
        raise RuntimeError(f"cjxl 编码失败：{tail}")


def exif_tiff(dpi: float, software: str = "hdr-gainmap") -> bytes:
    """构造写进 JXL Exif box 的 TIFF 载荷（小端、IFD0 只有分辨率与软件）。"""
    from .ultrahdr import EXIF_ID, _exif

    return _exif(software, dpi=dpi)[len(EXIF_ID):]


def exif_dpi(blob: bytes) -> float | None:
    """从 JXL 容器的 Exif box 读 XResolution；没有 Exif 时返回 None。"""
    if not blob.startswith(CONTAINER_MAGIC):
        return None
    pos = len(CONTAINER_MAGIC)
    while pos + 8 <= len(blob):
        size, kind = struct.unpack(">I4s", blob[pos:pos + 8])
        header = 8
        if size == 1:                               # 64 位 largesize
            if pos + 16 > len(blob):
                break
            size = struct.unpack(">Q", blob[pos + 8:pos + 16])[0]
            header = 16
        if size < header:
            break
        if kind == b"Exif":
            payload = blob[pos + header:pos + size]
            if len(payload) < 4:
                return None
            skip = struct.unpack(">I", payload[:4])[0]   # TIFF 头相对负载起点的偏移
            return _tiff_xres(payload[4 + skip:])
        pos += size
    return None


def _tiff_xres(tiff: bytes) -> float | None:
    """解析 TIFF IFD0，取 0x011A 有理数的商。"""
    if len(tiff) < 8 or tiff[:2] not in (b"II", b"MM"):
        return None
    bo = "<" if tiff[:2] == b"II" else ">"
    ifd = struct.unpack(bo + "I", tiff[4:8])[0]
    if ifd + 2 > len(tiff):
        return None
    count = struct.unpack(bo + "H", tiff[ifd:ifd + 2])[0]
    for i in range(count):
        off = ifd + 2 + 12 * i
        if off + 12 > len(tiff):
            return None
        tag, typ, n = struct.unpack(bo + "HHI", tiff[off:off + 8])
        if tag != 0x011A or typ != 5 or n != 1:
            continue
        off_val = struct.unpack(bo + "I", tiff[off + 8:off + 12])[0]
        if off_val + 8 > len(tiff):
            return None
        num, den = struct.unpack(bo + "II", tiff[off_val:off_val + 8])
        return num / den if den else None
    return None


def info(path: str | Path) -> dict:
    """用 jxlinfo 读位深、传递函数、原色；没有 jxlinfo 时返回空 dict。"""
    jxlinfo = jxlinfo_path()
    if jxlinfo is None:
        return {}
    proc = subprocess.run([str(jxlinfo), str(path)], capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(f"jxlinfo 读取失败：{(proc.stderr or proc.stdout).strip()}")
    text = proc.stdout
    result: dict = {"raw": text}
    m = re.search(r"(\d+)-bit\s+RGB", text)
    if m:
        result["bits"] = int(m.group(1))
    for key, label in (("transfer", "Transfer function"),
                       ("primaries", "Primaries"),
                       ("white_point", "White point"),
                       ("intensity_target", "Intensity target")):
        m = re.search(rf"{label}:\s*(.+)", text)
        if m:
            value = m.group(1).strip()
            if key == "intensity_target":
                try:
                    value = float(value.split()[0])
                except ValueError:
                    pass
            result[key] = value
    return result
