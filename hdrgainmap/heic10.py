"""HDR HEIC（HEVC Main10 / Main12 + BT.2020 + PQ/HLG）。

要点：
  · 10bit 兼容性最好（Windows/macOS/iOS/Android 都认）；
    12bit（Main12）色深更高，但 Windows 自带的 HEVC 扩展解不了，
    会报 0xC00D3E8C —— 需要系统装 HEVC 视频扩展/第三方解码器。
  · 颜色必须写全：primaries=9(BT.2020)、transfer=16(PQ)/18(HLG)、
    matrix=9(BT.2020 NCL)。matrix 不写会落成 6(BT.601)，部分解码器
    按 601 反矩阵，颜色直接偏。
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
           quality: int = 100, dpi: float | None = None) -> bytes:
    """q16: (h,w,3) uint16 的 PQ/HLG 码值 → HEIC 字节。bits 支持 10/12。"""
    if bits not in (10, 12):
        raise ValueError(f"HEIC 只支持 10/12bit，收到 {bits}")
    h, w, _ = q16.shape
    ph.options.SAVE_HDR_TO_12_BIT = (bits == 12)
    kwargs = dict(
        quality=quality,
        color_primaries=ph.HeifColorPrimaries.ITU_R_BT_2020_2_AND_2100_0,
        transfer_characteristics=(ph.HeifTransferCharacteristics.ITU_R_BT_2100_0_HLG
                                  if transfer == "HLG"
                                  else ph.HeifTransferCharacteristics.ITU_R_BT_2100_0_PQ),
        matrix_coefficients=ph.HeifMatrixCoefficients.ITU_R_BT_2020_2_NON_CONSTANT_LUMINANCE,
    )
    if dpi:
        kwargs["exif"] = exif_with_dpi(dpi)
    buf = io.BytesIO()
    ph.encode("RGB;16", (w, h), np.ascontiguousarray(q16).tobytes(), buf, **kwargs)
    return buf.getvalue()


def decode(blob: bytes, to_8bit: bool = False) -> tuple[np.ndarray, dict]:
    """解回码值数组与元信息（位深 / 色度 / nclx）。"""
    f = ph.open_heif(io.BytesIO(blob), convert_hdr_to_8bit=to_8bit)[0]
    arr = np.asarray(f)                                      # RGB;16 不能走 to_pillow()
    nclx = {k: v for k, v in f.info.get("nclx_profile", {}).items() if not k.endswith(("_x", "_y"))}
    return arr, {"bit_depth": f.info.get("bit_depth"), "chroma": f.info.get("chroma"), "nclx": nclx}
