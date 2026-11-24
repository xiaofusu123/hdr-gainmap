"""10bit HDR HEIC（HEVC Main10 + BT.2020 + PQ/HLG）。

HEIC 是安卓/iOS 交换 HDR 的实用格式，但要注意：
  · 12bit（Main12）Windows 的 HEVC 扩展解不了，会报 0xC00D3E8C —— 只出 10bit；
  · HEIF 容器没有 DPI 字段，只能借 EXIF 的 XResolution/YResolution 携带，
    Windows/WIC 会读，多数第三方 App 不看。
"""
from __future__ import annotations

import io

import numpy as np
from PIL import Image
import pillow_heif as ph

SDR_WHITE_NITS = 203.0        # 本管线的 SDR 白参考亮度


def exif_with_dpi(dpi: float) -> bytes:
    """构造只含分辨率的 EXIF（282/283 分辨率, 296 单位=英寸）。"""
    ex = Image.new("RGB", (1, 1)).getexif()
    ex[282] = float(dpi)
    ex[283] = float(dpi)
    ex[296] = 2
    return ex.tobytes()


def encode(q16: np.ndarray, transfer: str = "PQ", bits: int = 10,
           quality: int = 92, dpi: float | None = None) -> bytes:
    """q16: (h,w,3) uint16 的 PQ/HLG 码值 → HEIC 字节。"""
    h, w, _ = q16.shape
    ph.options.SAVE_HDR_TO_12_BIT = (bits == 12)
    kwargs = dict(quality=quality,
                  color_primaries=ph.HeifColorPrimaries.ITU_R_BT_2020_2_AND_2100_0,
                  transfer_characteristics=(ph.HeifTransferCharacteristics.ITU_R_BT_2100_0_HLG
                                            if transfer == "HLG"
                                            else ph.HeifTransferCharacteristics.ITU_R_BT_2100_0_PQ))
    if dpi:
        kwargs["exif"] = exif_with_dpi(dpi)
    buf = io.BytesIO()
    ph.encode("RGB;16", (w, h), np.ascontiguousarray(q16).tobytes(), buf, **kwargs)
    return buf.getvalue()


def encode_sdr(im: Image.Image, quality: int = 92, dpi: float | None = None) -> bytes:
    """纯换容器：8bit sRGB → HEIC，不做任何 HDR 编码。高调素材走这条。"""
    kwargs = dict(quality=quality)
    if dpi:
        kwargs["exif"] = exif_with_dpi(dpi)
    buf = io.BytesIO()
    ph.register_heif_opener()                                # 让 PIL 认识 HEIF
    im.convert("RGB").save(buf, "HEIF", **kwargs)
    return buf.getvalue()


def decode(blob: bytes, to_8bit: bool = False) -> tuple[np.ndarray, dict]:
    """解回码值数组与元信息（位深 / 色度 / nclx）。"""
    f = ph.open_heif(io.BytesIO(blob), convert_hdr_to_8bit=to_8bit)[0]
    arr = np.asarray(f)                                      # RGB;16 不能走 to_pillow()
    nclx = {k: v for k, v in f.info.get("nclx_profile", {}).items() if not k.endswith(("_x", "_y"))}
    return arr, {"bit_depth": f.info.get("bit_depth"), "chroma": f.info.get("chroma"), "nclx": nclx}
