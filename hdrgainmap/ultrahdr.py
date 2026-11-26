"""Ultra HDR JPEG 封装与解析（MPF + XMP hdrgm 1.0 + ISO 21496-1）。

文件结构（与 libultrahdr 的写盘顺序一致）::

    SOI
    APP0 JFIF            ← 源图若有则原样保留在最前
    APP1 Exif            ← 只写 Software / ImageDescription
    APP2 ISO (主图)      ← 命名空间 + 两个 u16 版本号（4 字节存根，大端）
    APP2 MPF             ← 两条 16 字节条目，定位增益图
    APP1 XMP (主图)      ← GContainer 目录 + hdrgm:Version
    <主图其余段 + 熵编码 + EOI>
    SOI + APP1 XMP (增益图)  ← 完整 hdrgm 参数
        + APP2 ISO (增益图)  ← 61 字节完整元数据（单通道，大端）
        + 增益图熵编码 + EOI

三条硬约束（对应 libultrahdr `hasValidMpfAssociation()`）：

* 主条目 ``attr & 0x0fffffff == 0x00030000``、``offset == 0``；
* 增益图条目 ``attr & 0x0fffffff == 0``、``size == 增益图总长``，
  且 ``offset`` **相对 MPF TIFF 头**而不是文件起点；
* 主图长度 ↔ 增益图偏移必须迭代到不动点（XMP 里的 Item:Length 会改变长度）。

元数据分工：主图 XMP 带 GContainer 目录 + 完整 hdrgm 参数；增益图自己的 XMP
也带一份完整 hdrgm 参数（部分解析器只读增益图的）。ISO 21496-1 则主图只写
版本存根、增益图写完整元数据。
"""
from __future__ import annotations

import struct

from PIL import Image

MPF_SIG = b"MPF\x00"
XMP_NS = b"http://ns.adobe.com/xap/1.0/\x00"
ISO_NS = b"urn:iso:std:iso:ts:21496:-1\x00"
EXIF_ID = b"Exif\x00\x00"
ICC_ID = b"ICC_PROFILE\x00"

GAINMAP_NS = "http://ns.adobe.com/hdr-gain-map/1.0/"
CONTAINER_NS = "http://ns.google.com/photos/1.0/container/"
ITEM_NS = "http://ns.google.com/photos/1.0/container/item/"

DEFAULT_OFFSET = 1.0 / 64.0
SOFTWARE = "hdr-gainmap 0.1.1b1"

# MPF 段总长：marker(2)+length(2)+签名(4)+TIFF(4 字节序 + 4 IFD 偏移 + 42 IFD + 32 条目) = 90
_MPF_SEG_LEN = 90
_MPF_TIFF_IFD_END = 50          # TIFF 内条目起始偏移（相对签名之后）


# --------------------------------------------------------------------------- 基础


def _seg(marker: int, payload: bytes) -> bytes:
    """构造一个 JPEG 段：FF mark + 2 字节长度（含长度自身）+ payload。"""
    return bytes((0xFF, marker)) + struct.pack(">H", len(payload) + 2) + payload


def _u16(buf: bytes, off: int, endian: str = ">") -> int:
    return struct.unpack_from(endian + "H", buf, off)[0]


def _u32(buf: bytes, off: int, endian: str = ">") -> int:
    return struct.unpack_from(endian + "I", buf, off)[0]


def _insert_after_soi(jpeg: bytes, payload: bytes) -> bytes:
    return jpeg[:2] + payload + jpeg[2:]


# --------------------------------------------------------------------------- Exif


def _exif(software: str, description: str | None = None, dpi: float | None = None) -> bytes:
    """最小 Exif：IFD0 写 Software / ImageDescription / 分辨率（DPI）。"""
    items: list[tuple[int, int, bytes]] = []          # (tag, type, 原始值)
    if description:
        items.append((0x010E, 2, description.encode("ascii", "replace") + b"\x00"))
    if dpi:
        n = int(round(float(dpi) * 100))
        rat = struct.pack("<II", n, 100)               # 有理数：N/100
        items.append((0x011A, 5, rat))                 # XResolution
        items.append((0x011B, 5, rat))                 # YResolution
        items.append((0x0128, 3, struct.pack("<H", 2)))  # ResolutionUnit = 英寸
    items.append((0x0131, 2, software.encode("ascii", "replace") + b"\x00"))
    items.sort(key=lambda it: it[0])

    ifd_len = 2 + 12 * len(items) + 4
    data_off = 8 + ifd_len
    data = bytearray()
    ifd = bytearray(struct.pack("<H", len(items)))
    for tag, typ, raw in items:
        unit = {2: 1, 3: 2, 5: 8}[typ]                  # 每个元素占几字节
        count = len(raw) // unit
        if len(raw) <= 4:
            val = raw.ljust(4, b"\x00")
        else:
            val = struct.pack("<I", data_off + len(data))
            data += raw
        ifd += struct.pack("<HHI", tag, typ, count) + val
    ifd += struct.pack("<I", 0)                        # 没有下一个 IFD

    tiff = b"II\x2a\x00" + struct.pack("<I", 8) + bytes(ifd) + bytes(data)
    return EXIF_ID + tiff


# --------------------------------------------------------------------------- XMP


def _fmt(v: float) -> str:
    """数字格式化：保留足够精度，但不输出科学计数法/多余的 .0。"""
    s = f"{float(v):.8g}"
    return "0" if s == "-0" else s


def _xmp_primary(gain_len: int, gmax: float, offset: float) -> str:
    """主图 XMP：GContainer 目录 + hdrgm 参数（元素写法，与 v1.0 一致）。

    参数以子元素形式给出（<hdrgm:GainMapMax>…</hdrgm:GainMapMax>），而不是
    rdf:Description 的属性：元素写法在荣耀/Android 16 上显示正常，改成属性写法后
    高光出现黑斑，故这里保持元素写法。
    """
    g = _fmt(gmax)
    off = _fmt(offset)
    return (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="hdr-gainmap">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f'<rdf:Description rdf:about="" xmlns:Container="{CONTAINER_NS}" '
        f'xmlns:Item="{ITEM_NS}" xmlns:hdrgm="{GAINMAP_NS}" hdrgm:Version="1.0">'
        + _hdrgm_elements(g, off) +
        '<Container:Directory><rdf:Seq>'
        '<rdf:li rdf:parseType="Resource">'
        '<Container:Item Item:Semantic="Primary" Item:Mime="image/jpeg"/>'
        '</rdf:li>'
        '<rdf:li rdf:parseType="Resource">'
        f'<Container:Item Item:Semantic="GainMap" Item:Mime="image/jpeg" Item:Length="{gain_len}"/>'
        '</rdf:li>'
        '</rdf:Seq></Container:Directory>'
        '</rdf:Description></rdf:RDF></x:xmpmeta>'
    )


def _xmp_gainmap(gmax: float, offset: float) -> str:
    """增益图 XMP：完整 hdrgm 参数（同样用元素写法）。"""
    g = _fmt(gmax)
    off = _fmt(offset)
    return (
        '<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="hdr-gainmap">'
        '<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        f'<rdf:Description rdf:about="" xmlns:hdrgm="{GAINMAP_NS}" hdrgm:Version="1.0">'
        + _hdrgm_elements(g, off) +
        '</rdf:Description></rdf:RDF></x:xmpmeta>'
    )


def _hdrgm_elements(g: str, off: str) -> str:
    """hdrgm 参数以子元素输出（安卓/Skia 对元素写法兼容性最好）。"""
    return (
        '<hdrgm:GainMapMin>0</hdrgm:GainMapMin>'
        f'<hdrgm:GainMapMax>{g}</hdrgm:GainMapMax>'
        '<hdrgm:Gamma>1</hdrgm:Gamma>'
        f'<hdrgm:OffsetSDR>{off}</hdrgm:OffsetSDR>'
        f'<hdrgm:OffsetHDR>{off}</hdrgm:OffsetHDR>'
        '<hdrgm:HDRCapacityMin>0</hdrgm:HDRCapacityMin>'
        f'<hdrgm:HDRCapacityMax>{g}</hdrgm:HDRCapacityMax>'
        '<hdrgm:BaseRenditionIsHDR>False</hdrgm:BaseRenditionIsHDR>'
    )


# --------------------------------------------------------------------------- ISO


def _frac(v: float, denom: int = 100_000_000) -> bytes:
    """有符号分数（s32 分子 + u32 分母，大端）；ISO 元数据里的 N/D 对。

    ISO 21496-1 的 APP2 载荷按网络字节序（大端）存放；早期版本误用小端，
    解码端会把 0.89387586 读成 75 左右，表现就是高光区域发黑。
    """
    return struct.pack(">iI", int(round(float(v) * denom)), denom)


def _frac_u(v: float, denom: int = 100_000_000) -> bytes:
    """无符号分数（u32 分子 + u32 分母，大端）。"""
    return struct.pack(">II", int(round(float(v) * denom)), denom)


def _iso_metadata(gmax: float, offset: float, base_headroom: float = 0.0,
                  flags: int = 0x40) -> bytes:
    """61 字节单通道 ISO 21496-1 元数据（大端，min/writer 版本 0）。

    所有值都在 log2 域：headroom 与 gain map min/max 直接写 XMP 的
    ``HDRCapacity*`` / ``GainMap*`` 数值，不要先取 2 的幂。
    分母统一用 1e8，与 XMP 的 8 位小数逐位对齐（差值 0）。
    """
    out = struct.pack(">HHB", 0, 0, flags)
    out += _frac_u(base_headroom)                  # base headroom = 0/1e8
    out += _frac_u(gmax)                           # alternate headroom = GainMapMax
    out += _frac(0.0)                              # gain map min
    out += _frac(gmax)                             # gain map max
    out += struct.pack(">II", 1, 1)                # gamma = 1/1
    out += _frac(offset)                           # base offset
    out += _frac(offset)                           # alternate offset
    assert len(out) == 61, len(out)
    return out


# --------------------------------------------------------------------------- MPF


def _mpf(primary_len: int, gain_len: int, gain_rel: int) -> bytes:
    """MPF：标准 TIFF 头（``II*\\0`` + Index IFD 偏移），条目偏移相对 TIFF 起点。"""
    tiff = bytearray(b"II\x2a\x00")                               # 小端 TIFF 头
    tiff += struct.pack("<I", 8)                                  # Index IFD 偏移
    tiff += struct.pack("<H", 3)                                  # 3 个标签
    tiff += struct.pack("<HHI", 0xB000, 7, 4) + b"0100"           # Version
    tiff += struct.pack("<HHI", 0xB001, 4, 1) + struct.pack("<I", 2)  # NumberOfImages
    tiff += struct.pack("<HHI", 0xB002, 7, 32) + struct.pack("<I", _MPF_TIFF_IFD_END)
    tiff += struct.pack("<I", 0)                                  # 没有下一个 IFD
    assert len(tiff) == _MPF_TIFF_IFD_END
    tiff += struct.pack("<IIIHH", 0x00030000, primary_len, 0, 0, 0)
    tiff += struct.pack("<IIIHH", 0x00000000, gain_len, gain_rel, 0, 0)
    assert len(tiff) == 82
    return MPF_SIG + bytes(tiff)


# --------------------------------------------------------------------------- 主图清理


def _split_app0(jpeg: bytes) -> tuple[bytes, bytes]:
    """把 JFIF(APP0) 与其余部分分开，保证它仍在 SOI 之后第一位。"""
    if len(jpeg) >= 6 and jpeg[2:4] == b"\xff\xe0":
        length = _u16(jpeg, 4)                 # 长度字段从它自身算起
        end = 4 + length                       # 段尾（不含）：SOI 2 + marker 2 + length
        if 6 <= length and end <= len(jpeg):
            return jpeg[:end], jpeg[end:]
    return jpeg[:2], jpeg[2:]


def strip_metadata(jpeg: bytes) -> bytes:
    """去掉旧的 Exif / XMP / MPF / ISO 段，保留 ICC、JFIF、APP13 等。

    复用源 JPEG 字节时必须先剥掉这些段：否则会出现两份 Exif、两份 XMP，
    解码器可能读到旧的增益图参数。
    """
    if not jpeg.startswith(b"\xff\xd8"):
        raise ValueError("不是 JPEG（缺少 SOI）")
    out = bytearray(b"\xff\xd8")
    pos = 2
    while pos + 4 <= len(jpeg):
        if jpeg[pos] != 0xFF:
            out += jpeg[pos:]
            return bytes(out)
        marker = jpeg[pos + 1]
        if marker == 0xDA:                                  # SOS 之后是熵数据
            out += jpeg[pos:]
            return bytes(out)
        if marker == 0x01 or 0xD0 <= marker <= 0xD7:
            out += jpeg[pos:pos + 2]
            pos += 2
            continue
        length = _u16(jpeg, pos + 2)
        end = pos + 2 + length
        if end > len(jpeg):
            break
        payload = jpeg[pos + 4:end]
        drop = ((marker == 0xE1 and (payload.startswith(EXIF_ID) or payload.startswith(XMP_NS)))
                or (marker == 0xE2 and (payload.startswith(MPF_SIG) or payload.startswith(ISO_NS))))
        if not drop:
            out += jpeg[pos:end]
        pos = end
    out += jpeg[pos:]
    return bytes(out)


# --------------------------------------------------------------------------- 构建


def build(primary: bytes, gain_jpeg: bytes, gmax: float = 2.0,
          offset: float = DEFAULT_OFFSET, software: str = SOFTWARE,
          iso_flags: int = 0x40,
          description: str | None = None, dpi: float | None = None,
          icc: bytes | None = None) -> bytes:
    """把 SDR 主图与增益图 JPEG 封装成 Ultra HDR 1.1 JPEG。

    ``primary`` 是完整的 8bit JPEG（SOI 开头）；会先剥掉旧的 Exif/XMP/MPF/ISO 再封装，
    其余字节（DQT/SOF/DHT/SOS + 熵数据）原样保留——源图本身是 JPEG 时即零重编码。
    按 Ultra HDR 1.1 同时写 XMP ``hdrgm`` 与 ISO 21496-1：主图放 4 字节版本存根，
    增益图放完整元数据。1.1 向下兼容 1.0，因此不再提供 XMP-only 模式。
    """
    if not primary.startswith(b"\xff\xd8"):
        raise ValueError("主图必须是 JPEG")
    if not gain_jpeg.startswith(b"\xff\xd8"):
        raise ValueError("增益图必须是 JPEG")

    primary = strip_metadata(primary)
    gain = strip_metadata(gain_jpeg)
    jfif, rest = _split_app0(primary)
    if b"\xff\xda" in jfif:                       # 防御：元数据绝不能插进扫描数据里
        raise ValueError("主图 APP0 段异常：SOS 出现在 APP0 之内")

    gmax = float(gmax)
    offset = float(offset)
    icc_seg = b""
    if icc and not _find(primary, 0xE2, ICC_ID):    # 主图本来没有 ICC 才补（规范要求标色）
        icc_seg = _seg(0xE2, ICC_ID + b"\x01\x01" + icc)
    exif = _seg(0xE1, _exif(software, description, dpi))
    iso_stub = _seg(0xE2, ISO_NS + struct.pack(">HH", 0, 0))
    gain_head = (_seg(0xE1, XMP_NS + _xmp_gainmap(gmax, offset).encode("utf-8"))
                 + _seg(0xE2, ISO_NS + _iso_metadata(gmax, offset, flags=iso_flags)))
    gain_body = gain[2:]
    gain_total = 2 + len(gain_head) + len(gain_body)

    gain_rel = 0
    mpf = b""
    primary_len = 0
    for _ in range(8):
        xmp_primary = _seg(0xE1, XMP_NS + _xmp_primary(gain_total, gmax, offset).encode("utf-8"))
        head_len = len(icc_seg) + len(exif) + _MPF_SEG_LEN + len(xmp_primary) + len(iso_stub)
        primary_len = len(jfif) + head_len + len(rest)
        # MPF 的 TIFF 头紧跟 ICC / EXIF / ISO 之后，偏移必须把 ISO 段算进去。
        tiff_abs = len(jfif) + len(icc_seg) + len(exif) + len(iso_stub) + 8
        gain_rel = primary_len - tiff_abs
        mpf = _seg(0xE2, _mpf(primary_len, gain_total, gain_rel))
        if len(mpf) != _MPF_SEG_LEN:                  # 防御：长度只取决于定长字段
            raise AssertionError("MPF 长度异常")

    # 主图顺序与 libultrahdr 产物一致：ICC → EXIF → ISO → MPF → XMP → 图像数据。
    return (jfif + icc_seg + exif + iso_stub + mpf + xmp_primary + rest
            + b"\xff\xd8" + gain_head + gain_body)


# --------------------------------------------------------------------------- 解析


def _find(blob: bytes, marker: int, ident: bytes, stop: int | None = None) -> list[tuple[int, bytes]]:
    """在 JPEG 头部找指定 marker + 标识的段，返回 (段起点, payload)。"""
    out = []
    pos = 2
    limit = len(blob) if stop is None else min(stop, len(blob))
    while pos + 4 <= limit:
        if blob[pos] != 0xFF:
            break
        mk = blob[pos + 1]
        if mk == 0xDA:
            break
        if mk == 0x01 or 0xD0 <= mk <= 0xD7:
            pos += 2
            continue
        length = _u16(blob, pos + 2)
        end = pos + 2 + length
        if end > len(blob):
            break
        if mk == marker and blob.startswith(ident, pos + 4):
            out.append((pos, blob[pos + 4:end]))
        pos = end
    return out


def _parse_hdrgm(xmp: bytes) -> dict:
    import re
    text = xmp.decode("utf-8", "replace")
    keys = ("Version", "GainMapMin", "GainMapMax", "Gamma", "OffsetSDR",
            "OffsetHDR", "HDRCapacityMin", "HDRCapacityMax", "BaseRenditionIsHDR")
    out: dict = {}
    for k in keys:
        m = re.search(rf'hdrgm:{k}\s*=\s*"([^"]*)"', text)
        if not m:
            m = re.search(rf"<hdrgm:{k}>(.*?)</hdrgm:{k}>", text, re.S)
        if m:
            out[k] = m.group(1).strip()
    out["_text"] = text
    return out


def _parse_mpf_entry(blob: bytes, payload: bytes, payload_abs: int,
                     which: int) -> tuple[int, int, int, int] | None:
    """从 MPF 段里取第 which 条 (attr, size, 相对 TIFF 的偏移, TIFF 绝对起点)。"""
    if not payload.startswith(MPF_SIG):
        return None
    tiff = payload[4:]
    tiff_abs = payload_abs + 4
    if tiff[:2] == b"II":
        e = "<"
    elif tiff[:2] == b"MM":
        e = ">"
    else:
        return None
    ifd = _u32(tiff, 4, e)                 # TIFF 头：4 字节字节序 + 4 字节 IFD 偏移
    if ifd + 2 > len(tiff):
        return None
    count = _u16(tiff, ifd, e)
    entries = None
    for i in range(min(count, 64)):
        p = ifd + 2 + 12 * i
        if p + 12 > len(tiff):
            return None
        if _u16(tiff, p, e) == 0xB002:
            entries = _u32(tiff, p + 8, e)
    if entries is None:
        return None
    p = entries + which * 16
    if p + 16 > len(tiff):
        return None
    attr = _u32(tiff, p, e)
    size = _u32(tiff, p + 4, e)
    off = _u32(tiff, p + 8, e)
    return attr, size, off, tiff_abs


def parse(blob: bytes) -> dict:
    """解析 Ultra HDR JPEG，返回主图/增益图边界与增益参数。"""
    if not blob.startswith(b"\xff\xd8"):
        raise ValueError("不是 JPEG")
    xmps = _find(blob, 0xE1, XMP_NS)
    isos = _find(blob, 0xE2, ISO_NS)
    mpfs = _find(blob, 0xE2, MPF_SIG)
    primary_xmp = _parse_hdrgm(xmps[0][1][len(XMP_NS):]) if xmps else {}

    info: dict = {"primary_len": 0, "gain_offset": 0, "gain_len": 0,
                  "xmp_primary": primary_xmp.get("_text", ""),
                  "iso_primary": isos[0][1] if isos else None}

    entries = None
    if mpfs:
        entries = [_parse_mpf_entry(blob, mpfs[0][1], mpfs[0][0] + 4, i) for i in range(2)]
        if None in entries:
            entries = None
    if entries:
        info["entries"] = [{"attr": a, "size": s, "offset": o} for a, s, o, _t in entries]
        tiff_abs = entries[1][3]
        info["primary_len"] = tiff_abs + entries[1][2]
        info["gain_offset"] = info["primary_len"]
        info["gain_len"] = entries[1][1]
    else:
        # 没有 MPF 时退回 GContainer 的 Item:Length（能读，但合规性由 validate.py 判）
        import re
        m = re.search(r'Item:Semantic="GainMap".*?Item:Length="(\d+)"', info["xmp_primary"])
        if m and isos:
            info["gain_len"] = int(m.group(1))

    g0 = info["gain_offset"]
    g1 = g0 + info["gain_len"]
    if info["gain_len"] and g1 <= len(blob):
        gain = blob[g0:g1]
        gxmp = _find(gain, 0xE1, XMP_NS)
        giso = _find(gain, 0xE2, ISO_NS)
        gm = _parse_hdrgm(gxmp[0][1][len(XMP_NS):]) if gxmp else {}
        info["xmp_gainmap"] = gm.pop("_text", "")
        info["hdrgm"] = gm
        info["iso_gainmap"] = giso[0][1] if giso else None
        if "GainMapMax" in gm:
            info["gmax"] = float(gm["GainMapMax"])
        if "OffsetSDR" in gm:
            info["offset_sdr"] = float(gm["OffsetSDR"])
        if "OffsetHDR" in gm:
            info["offset_hdr"] = float(gm["OffsetHDR"])
    return info


def read_gainmap(blob: bytes) -> bytes:
    """取出增益图 JPEG 字节。"""
    info = parse(blob)
    if not info["gain_len"]:
        raise ValueError("找不到增益图（MPF / XMP 都没有给出长度）")
    return blob[info["gain_offset"]:info["gain_offset"] + info["gain_len"]]


# --------------------------------------------------------------------------- 增益应用


def reconstruct(sdr_linear, gain_norm, gmax: float, offset: float = DEFAULT_OFFSET):
    """按规范把归一化增益图应用到线性 SDR：``(S+off)·2^(gmax·g) − off``。

    gain_norm 是 [0,1] 的恢复度（已解码、已放大到主图尺寸）；g=0 时结果严格等于 SDR。
    """
    import numpy as np
    g = np.asarray(gain_norm, dtype=np.float32)
    s = np.asarray(sdr_linear, dtype=np.float32)
    boost = np.exp2(gmax * g)
    return np.clip((s + offset) * boost[..., None] - offset, 0.0, None).astype(np.float32)


def peak_nits(gain_norm, sdr_linear, gmax: float, white_nits: float,
              offset: float = DEFAULT_OFFSET) -> float:
    """按规范公式算峰值亮度（nits）。"""
    return float(reconstruct(sdr_linear, gain_norm, gmax, offset).max() * white_nits)
