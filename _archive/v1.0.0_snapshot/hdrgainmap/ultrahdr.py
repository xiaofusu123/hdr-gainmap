"""Ultra HDR（ISO 21496-1 / Adobe hdrgm 1.0）JPEG 封装与解析。

文件布局：
    SOI
    APP1 Exif            Software / ImageDescription
    APP2 MPF             两幅 JPEG 的索引（absolute offset）
    APP1 XMP             GContainer 目录 + hdrgm 参数
    APP2 ISO 标记        urn:iso:std:iso:ts:21496:-1
    <原主图字节（含 EOI）>
    <增益图 JPEG，半分辨率灰度>

主图一个字节都不重编码，所以 SDR 观感与源图完全一致；
不支持增益图的软件看到的也就是原图。
"""
from __future__ import annotations

import io
import re
import struct

from PIL import Image

ISO_MARKER = b"urn:iso:std:iso:ts:21496:-1\0"
XMP_HEADER = b"http://ns.adobe.com/xap/1.0/\0"
DEFAULT_OFFSET = 0.015625  # 1/64，ISO 21496-1 缺省值


def _seg(marker: int, payload: bytes) -> bytes:
    """拼一个 JPEG 段（marker 只给低字节，如 0xE1）。"""
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def _exif(software: str, description: str) -> bytes:
    """最小 TIFF/EXIF：只写 Software(0x131) 与 ImageDescription(0x10E)。"""
    entries = sorted([(0x0131, software), (0x010E, description)])
    data_off = 8 + 2 + 12 * len(entries) + 4
    values, blobs, body = [], b"", struct.pack("<H", len(entries))
    for tag, text in entries:
        raw = text.encode("ascii") + b"\0"
        if len(raw) <= 4:                                     # 不超 4 字节直接内联
            values.append(struct.pack("<HHI", tag, 2, len(raw)) + raw.ljust(4, b"\0"))
        else:
            values.append(struct.pack("<HHI", tag, 2, len(raw)) + struct.pack("<I", data_off + len(blobs)))
            blobs += raw
    body += b"".join(values) + struct.pack("<I", 0) + blobs
    return b"Exif\0\0" + b"II*\0" + struct.pack("<I", 8) + body


def _ratio(v: float) -> tuple[int, int]:
    """小数转分数：优先用在分母 2 的幂里能整除的那个，保证 1/64 这类值是精确的。"""
    for d in (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024, 4096, 65536):
        n = v * d
        if abs(n - round(n)) < 1e-9:
            return int(round(n)), d
    return int(round(v * 1_000_000)), 1_000_000


def _iso_metadata(gmax: float, offset: float) -> bytes:
    """ISO 21496-1 二进制元数据（单通道，独立分数对，按 libultrahdr 的顺序）。

    布局：最小版本(2) 写入版本(2) flags(1) | 基础峰值(除) | 备选峰值(除) | 每通道 5 组 (N, D)。
    基础峰值 = log2(capacity_min) = 0，备选峰值 = log2(capacity_max) = gmax。
    """
    hdr = struct.pack("<HH", 0, 0) + b"\0"                       # flags=0：单通道、基础色彩空间
    hdr += struct.pack("<II", *_ratio(0.0))                      # base headroom
    hdr += struct.pack("<II", *_ratio(gmax))                     # alternate headroom
    cap_n, cap_d = _ratio(gmax)
    off_n, off_d = _ratio(offset)
    body = struct.pack("<iI", *_ratio(0.0))                      # GainMapMin
    body += struct.pack("<iI", cap_n, cap_d)                     # GainMapMax
    body += struct.pack("<II", 1, 1)                             # Gamma
    body += struct.pack("<iI", off_n, off_d)                     # OffsetSDR
    body += struct.pack("<iI", off_n, off_d)                     # OffsetHDR
    return hdr + body


def _mpf(primary_len: int, gain_len: int, gain_abs: int, base: int) -> bytes:
    """MPF 索引，按 CIPA DC-007 / libultrahdr 的校验口径写。

    每条 MPEntry 16 字节：属性(4) 长度(4) 偏移(4) 依赖(2+2)。
    - 属性：主图 0x00030000，增益图 0x00000000（格式位 JPEG=0）。
    - 偏移：**相对 MP 头**（即 'MPF\\0' 之后的 TIFF 头起点），不是文件开头。
    libultrahdr 用 `tiff + secondary_offset == 增益图起始位置` 做相等校验，
    写成文件绝对偏移会导致校验失败 → 解码器回退 SDR。
    """
    entries = [(0x00030000, primary_len, 0),   # 主图：偏移必须为 0
               (0x00000000, gain_len, gain_abs - base)]  # 增益图：相对 MP 头
    body = b"".join(struct.pack("<II", attr, size) + struct.pack("<I", off) + b"\0\0\0\0"
                    for attr, size, off in entries)
    ifd_off = 8
    entry_off = ifd_off + 12 * 3 + 4 + 2
    ifd = struct.pack("<H", 3)
    ifd += struct.pack("<HHI", 0xB000, 7, 4) + b"0100"          # MPFVersion "0100"
    ifd += struct.pack("<HHII", 0xB001, 4, 1, len(entries))     # NumberOfImages
    ifd += struct.pack("<HHII", 0xB002, 7, len(body), entry_off)
    return b"MPF\0" + b"II*\0" + struct.pack("<I", ifd_off) + ifd + struct.pack("<I", 0) + body


def _xmp(primary_len: int, gain_len: int, gmax: float, offset: float) -> bytes:
    """GContainer 目录 + hdrgm 1.0 参数。"""
    return (f'''<?xpacket begin="\ufeff" id="W5M0MpCehiHzreSzNTczkc9d"?>
<x:xmpmeta xmlns:x="adobe:ns:meta/">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:hdrgm="http://ns.adobe.com/hdr-gain-map/1.0/"
    xmlns:Container="http://ns.google.com/photos/1.0/container/"
    xmlns:Item="http://ns.google.com/photos/1.0/container/item/"
    hdrgm:Version="1.0">
   <hdrgm:GainMapMin>0</hdrgm:GainMapMin>
   <hdrgm:GainMapMax>{gmax:.1f}</hdrgm:GainMapMax>
   <hdrgm:Gamma>1.0</hdrgm:Gamma>
   <hdrgm:OffsetSDR>{offset:.6f}</hdrgm:OffsetSDR>
   <hdrgm:OffsetHDR>{offset:.6f}</hdrgm:OffsetHDR>
   <hdrgm:HDRCapacityMin>0</hdrgm:HDRCapacityMin>
   <hdrgm:HDRCapacityMax>{gmax:.1f}</hdrgm:HDRCapacityMax>
   <hdrgm:BaseRenditionIsHDR>False</hdrgm:BaseRenditionIsHDR>
   <Container:Directory>
    <rdf:Seq>
     <rdf:li rdf:parseType="Resource">
      <Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary"
        Item:Length="{primary_len}" Item:Data=""/>
     </rdf:li>
     <rdf:li rdf:parseType="Resource">
      <Container:Item Item:Mime="image/jpeg" Item:Semantic="GainMap"
        Item:Length="{gain_len}" Item:Data=""/>
     </rdf:li>
    </rdf:Seq>
   </Container:Directory>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
<?xpacket end="w"?>''').encode("utf-8")


def build(primary: bytes, gain_jpeg: bytes, gmax: float = 2.0, offset: float = DEFAULT_OFFSET,
          software: str = "Emissive GainMap 0.1",
          description: str = "Ultra HDR gain map from heuristic emissive separation") -> bytes:
    """把普通 SDR JPEG 与增益图 JPEG 合成 Ultra HDR。"""
    if primary[:2] != b"\xff\xd8":
        raise ValueError("主图必须是 JPEG")
    tail = primary[2:]                              # SOI 之后到 EOI 的全部字节
    exif_seg = _seg(0xE1, _exif(software, description))
    # 主图只放"版本占位"（ns + 最小版本 + 写入版本），完整元数据放增益图那一段
    iso_seg = _seg(0xE2, ISO_MARKER + b"\0\0\0\0")
    # 增益图带自己的 ISO 元数据段，解码器按规范优先读它
    secondary = (gain_jpeg[:2]
                 + _seg(0xE2, ISO_MARKER + _iso_metadata(gmax, offset))
                 + gain_jpeg[2:])
    gain_len = len(secondary)

    # 元数据长度依赖偏移，偏移又依赖元数据长度 → 迭代到不动点
    tiff_abs = 2 + len(exif_seg) + 8                 # MPF 载荷里 TIFF 头的绝对位置

    def head_for(plen: int) -> bytes:
        # 首轮 plen=0 只用来量长度，给个不会为负的占位值
        gain_abs = plen if plen > tiff_abs else tiff_abs + 16
        mpf = _seg(0xE2, _mpf(plen, gain_len, gain_abs, tiff_abs))
        return (exif_seg + mpf
                + _seg(0xE1, XMP_HEADER + _xmp(plen, gain_len, gmax, offset))
                + iso_seg)

    primary_len = 2 + len(head_for(0)) + len(tail)
    for _ in range(8):
        trial = head_for(primary_len)
        new_len = 2 + len(trial) + len(tail)
        if new_len == primary_len:
            break
        primary_len = new_len
    return b"\xff\xd8" + head_for(primary_len) + tail + secondary


def parse(blob: bytes) -> dict:
    """解析 Ultra HDR 文件的关键字段（结构自检用）。"""
    info: dict = {"entries": [], "primary_len": None, "gain_len": None}
    xmp = blob.find(b"<hdrgm:GainMapMax>")
    if xmp != -1:
        info["gmax"] = float(re.search(rb"<hdrgm:GainMapMax>([\d.]+)<", blob).group(1))
        info["gamma"] = float(re.search(rb"<hdrgm:Gamma>([\d.]+)<", blob).group(1))
        info["primary_len"] = int(re.search(rb'Item:Semantic="Primary"\s+Item:Length="(\d+)"', blob).group(1))
        info["gain_len"] = int(re.search(rb'Item:Semantic="GainMap"\s+Item:Length="(\d+)"', blob).group(1))

    pos = 2
    while pos + 4 <= len(blob) and blob[pos] == 0xFF:
        marker = blob[pos + 1]
        if marker == 0xDA:                                  # SOS，后面是熵编码数据
            break
        length = struct.unpack(">H", blob[pos + 2:pos + 4])[0]
        payload = blob[pos + 4:pos + 2 + length]
        if marker == 0xE2 and payload.startswith(b"MPF\0"):
            tiff = payload[4:]
            tiff_abs = pos + 8                           # TIFF 头在文件里的绝对位置
            ifd = struct.unpack("<I", tiff[4:8])[0]
            count = struct.unpack("<H", tiff[ifd:ifd + 2])[0]
            for i in range(count):
                tag = struct.unpack("<H", tiff[ifd + 2 + 12 * i: ifd + 4 + 12 * i])[0]
                if tag == 0xB002:
                    blob_len = struct.unpack("<I", tiff[ifd + 6 + 12 * i: ifd + 10 + 12 * i])[0]
                    off = struct.unpack("<I", tiff[ifd + 10 + 12 * i: ifd + 14 + 12 * i])[0]
                    for k in range(blob_len // 16):      # 每条 MPEntry 固定 16 字节
                        attr, size, o = struct.unpack("<III", tiff[off + 16 * k: off + 16 * k + 12])
                        absolute = tiff_abs + o          # MPEntry 偏移相对 MP 头
                        info["entries"].append({"attr": attr, "size": size, "offset": o,
                                                "abs": absolute,
                                                "soi": blob[absolute:absolute + 2] == b"\xff\xd8"})
        pos += 2 + length
    return info


def read_gainmap(blob: bytes) -> Image.Image:
    """把尾部的增益图单独解出来（灰度）。"""
    info = parse(blob)
    start = info.get("primary_len") or (len(blob) - info["gain_len"])
    return Image.open(io.BytesIO(blob[start:]))
