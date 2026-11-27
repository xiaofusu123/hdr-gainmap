"""24bit HDR JPEG XL：把 PQ/HLG 码值交给 cjxl（libjxl）无损封装。

JPEG XL 的整数位深最高支持 32bit，这里固定用 24bit（每通道 3 字节），
不是 8bit SDR：输入是 BT.2020 + PQ/HLG 的浮点码值，写入时按 24bit 整数保存，
色彩编码显式标成 Rec.2100 PQ 或 Rec.2100 HLG。

cjxl 不读 PNG 的 PQ 元数据（PNG 只有 16bit 整数），所以这里用 PFM 传浮点码值：
PFM 的 float32 能无损装下 24bit 整数码值，cjxl 用 --override_bitdepth=24 量化，
避免"16bit 重标成 24bit"的伪高位深。PFM 的行序是自下而上，写文件时要翻转。

工具查找顺序：环境变量 HDRGAINMAP_CJXL / HDRGAINMAP_JXLINFO → PATH 里的
cjxl / jxlinfo。cjxl 0.13+ 才有 --override_bitdepth。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
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
           effort: int = 7) -> None:
    """把 PQ/HLG 浮点 PFM 编码成 24bit JPEG XL（无损，-d 0）。

    `transfer` 只接受 PQ/HLG；未知值直接报错，避免悄悄写错色彩标记。
    """
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
        "--quiet",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        tail = detail[-1] if detail else f"exit {proc.returncode}"
        raise RuntimeError(f"cjxl 编码失败：{tail}")


def info(path: str | Path) -> dict:
    """用 jxlinfo 读位深、传递函数、原色与强度目标。

    没有 jxlinfo 时返回空 dict；校验端会退化成只查文件签名。
    """
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
