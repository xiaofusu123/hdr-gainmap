"""严格校验 HDR 产物：Ultra HDR JPEG / 16bit PQ/HLG PNG / 10/12bit HDR HEIC / HDR AVIF / 24bit JXL。

Ultra HDR 部分逐条镜像 Android Ultra HDR v1.1 与 libultrahdr 的
``hasValidMpfAssociation()``：任何一条不满足，解码器都会悄悄忽略增益图回退 SDR。
这里把"errors"（解码器必然/极可能忽略）与"warnings"（能显示但不合规或兼容性差）
分开，方便定位到底是哪一环写错了。

用法::

    python -m hdrgainmap.validate out/x_ultrahdr.jpeg out/x_hdr16.png out/x_hdr10_pq.heic
"""
from __future__ import annotations

import io
import re
import struct
import sys
import zlib

import numpy as np
from PIL import Image

MPF_SIG = b"MPF\x00"
ISO_NS = b"urn:iso:std:iso:ts:21496:-1\x00"
XMP_ID = b"http://ns.adobe.com/xap/1.0/\x00"
EXIF_ID = b"Exif\x00\x00"
ICC_ID = b"ICC_PROFILE\x00"

SOI = b"\xff\xd8"
MASK = 0x0FFFFFFF                      # 属性低 28 位：format + type
PRIMARY_ATTR = 0x00030000              # CIPA: Baseline MP Primary Image
SECONDARY_ATTR = 0x00000000            # CIPA: JPEG format / undefined type
MPF_ENTRY_SIZE = 16

CONTAINER_NS = "http://ns.google.com/photos/1.0/container/"
ITEM_NS = "http://ns.google.com/photos/1.0/container/item/"
GAINMAP_NS = "http://ns.adobe.com/hdr-gain-map/1.0/"

HDRGM_KEYS = ("Version", "BaseRenditionIsHDR", "GainMapMin", "GainMapMax",
              "Gamma", "OffsetSDR", "OffsetHDR", "HDRCapacityMin", "HDRCapacityMax")


class Report:
    """一次校验的结果：errors 阻断、warnings 提醒、info 供人看。"""

    def __init__(self, kind: str = "", path: str = ""):
        self.kind = kind
        self.path = path
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.info: dict = {}

    def err(self, msg: str) -> None:
        self.errors.append(msg)

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)

    @property
    def ok(self) -> bool:
        return not self.errors

    def extend(self, other: "Report") -> None:
        self.errors += other.errors
        self.warnings += other.warnings
        self.info.update(other.info)

    def as_dict(self) -> dict:
        return {"kind": self.kind, "path": self.path, "ok": self.ok,
                "errors": list(self.errors), "warnings": list(self.warnings),
                "info": self.info}

    def format(self, verbose: bool = True) -> str:
        head = f"{self.kind or 'file'}: {self.path or '<bytes>'}"
        state = "OK" if self.ok else f"FAIL ({len(self.errors)} error)"
        lines = [f"{head}  ->  {state}"]
        for e in self.errors:
            lines.append(f"  [error] {e}")
        for w in self.warnings:
            lines.append(f"  [warn ] {w}")
        if verbose and self.info:
            lines.append("  [info ] " + ", ".join(f"{k}={v}" for k, v in self.info.items()))
        return "\n".join(lines)


# --------------------------------------------------------------------------- bytes


def _u16(buf: bytes, off: int, endian: str = ">") -> int:
    return struct.unpack_from(endian + "H", buf, off)[0]


def _u32(buf: bytes, off: int, endian: str = ">") -> int:
    return struct.unpack_from(endian + "I", buf, off)[0]


def segments(blob: bytes) -> list[dict]:
    """遍历 JPEG 主图段（到 SOS 为止），返回 marker/payload 的绝对偏移。"""
    if not blob.startswith(SOI):
        raise ValueError("不是 JPEG（缺少 SOI）")
    out: list[dict] = []
    pos = 2
    while pos + 4 <= len(blob):
        if blob[pos] != 0xFF:
            break                                # 已经进入熵编码数据
        marker = blob[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        if marker == 0xDA:                       # SOS
            out.append({"marker": marker, "start": pos, "payload_start": pos + 4,
                        "payload_end": pos + 4, "payload": b""})
            break
        length = _u16(blob, pos + 2)
        start = pos + 4
        end = pos + 2 + length
        out.append({"marker": marker, "start": pos, "payload_start": start,
                    "payload_end": end, "payload": blob[start:end]})
        pos = end
    return out


def _app_payloads(blob: bytes, segs: list[dict], marker: int, ident: bytes | None = None) -> list[dict]:
    found = []
    for s in segs:
        if s["marker"] != marker:
            continue
        if ident is None or blob.startswith(ident, s["payload_start"]):
            found.append(s)
    return found


# --------------------------------------------------------------------------- MPF


def parse_mpf(blob: bytes, seg: dict, report: Report | None = None) -> dict | None:
    """解析 APP2 MPF；返回绝对偏移的条目。失败时把原因写进 report 并返回 None。"""
    rep = report or Report()

    def bad(msg: str) -> None:
        rep.err(f"MPF: {msg}")

    payload = seg["payload"]
    if not payload.startswith(MPF_SIG):
        bad("APP2 段不是 MPF（缺 'MPF\\0' 签名）")
        return None
    tiff_abs = seg["payload_start"] + len(MPF_SIG)
    tiff_end = seg["payload_end"]
    if tiff_end - tiff_abs < 8:
        bad("MPF TIFF 头不足 8 字节（4 字节字节序 + 4 字节 IFD 偏移）")
        return None

    endian_tag = blob[tiff_abs:tiff_abs + 4]
    if endian_tag == b"II\x2a\x00":
        endian = "<"
    elif endian_tag == b"MM\x00\x2a":
        endian = ">"
    else:
        bad(f"MPF 字节序/魔数非法：{endian_tag!r}（应为 b'II*\\0' 或 b'MM\\0*'）")
        return None

    ifd_off = _u32(blob, tiff_abs + 4, endian)
    if ifd_off > tiff_end - tiff_abs:
        bad(f"IFD 偏移 {ifd_off} 越界")
        return None
    ifd = tiff_abs + ifd_off
    if ifd + 2 > tiff_end:
        bad("IFD 头部越界")
        return None

    count = _u16(blob, ifd, endian)
    if count > 64:
        bad(f"IFD 标签数异常：{count}")
        return None

    seen: dict[int, tuple[int, int, int]] = {}
    for i in range(count):
        p = ifd + 2 + 12 * i
        if p + 12 > tiff_end:
            bad("IFD 标签越界")
            return None
        tid, typ, n, value = _u16(blob, p, endian), _u16(blob, p + 2, endian), \
            _u32(blob, p + 4, endian), _u32(blob, p + 8, endian)
        if tid in seen:
            bad(f"标签 0x{tid:04X} 重复")
            return None
        seen[tid] = (typ, n, value, p)

    # 0xB000 Version: type 7, count 4, 值 "0100"
    if 0xB000 not in seen:
        bad("缺少 0xB000 Version 标签")
    else:
        typ, n, value, p = seen[0xB000]
        if typ != 7 or n != 4:
            bad(f"Version 标签类型/计数错误：type={typ} count={n}（应为 7/4）")
        elif blob[p + 8:p + 12] != b"0100":
            bad(f"Version 值错误：{blob[p + 8:p + 12]!r}（应为 b'0100'）")

    image_count = None
    if 0xB001 not in seen:
        bad("缺少 0xB001 NumberOfImages 标签")
    else:
        typ, n, value, _ = seen[0xB001]
        if typ != 4 or n != 1:
            bad(f"NumberOfImages 类型/计数错误：type={typ} count={n}（应为 4/1）")
        image_count = value
        if value != 2:
            bad(f"NumberOfImages={value}（应为 2）")

    if 0xB002 not in seen:
        bad("缺少 0xB002 MPEntry 标签")
        return None
    typ, n, entries_rel, _ = seen[0xB002]
    if typ != 7:
        bad(f"MPEntry 类型错误：type={typ}（应为 7/UNDEFINED）")
        return None
    if n < 2 * MPF_ENTRY_SIZE:
        bad(f"MPEntry 计数 {n} 小于 32")
        return None
    if image_count is not None and n != image_count * MPF_ENTRY_SIZE:
        bad(f"MPEntry 计数 {n} != NumberOfImages×16 = {image_count * MPF_ENTRY_SIZE}")
        return None
    entries_abs = tiff_abs + entries_rel
    if entries_abs + n > tiff_end:
        bad("MPEntry 数据越界")
        return None

    entries = []
    for k in range(n // MPF_ENTRY_SIZE):
        p = entries_abs + k * MPF_ENTRY_SIZE
        attr, size, off = _u32(blob, p, endian), _u32(blob, p + 4, endian), _u32(blob, p + 8, endian)
        entries.append({"attr": attr, "size": size, "offset": off,
                        "abs_offset": tiff_abs + off})

    return {"tiff_abs": tiff_abs, "endian": "little" if endian == "<" else "big",
            "entries": entries, "segment": seg}


# --------------------------------------------------------------------------- XMP


def _xmp_value(text: str, key: str) -> str | None:
    """从 XMP 里取 hdrgm:key（属性写法优先，兼容元素写法）。"""
    m = re.search(rf'hdrgm:{key}\s*=\s*"([^"]*)"', text)
    if m:
        return m.group(1)
    m = re.search(rf"<hdrgm:{key}>(.*?)</hdrgm:{key}>", text, re.S)
    return m.group(1).strip() if m else None


def parse_hdrgm(xmp: str) -> dict:
    out = {}
    for k in HDRGM_KEYS:
        v = _xmp_value(xmp, k)
        if v is not None:
            out[k] = v
    return out


def parse_container(xmp: str) -> list[dict]:
    """解析 GContainer 的 Container:Directory，按顺序返回每个 media item。"""
    items: list[dict] = []
    for block in re.findall(r"<rdf:li\b[^>]*>(.*?)</rdf:li>", xmp, re.S):
        item: dict = {}
        for key in ("Semantic", "Mime", "Length"):
            m = re.search(rf'Item:{key}\s*=\s*"([^"]*)"', block)
            if not m:
                m = re.search(rf"<Container:Item\b[^>]*Item:{key}\s*=\s*\"([^\"]*)\"", block)
            if not m:
                m = re.search(rf"<Item:{key}>(.*?)</Item:{key}>", block, re.S)
            if m:
                item[key.lower()] = m.group(1).strip()
        if item:
            items.append(item)
    return items


# --------------------------------------------------------------------------- ISO


def parse_iso(payload: bytes) -> dict | None:
    """解析 ISO 21496-1 元数据（命名空间之后的部分），返回结构化的分数与浮点值。"""
    if not payload.startswith(ISO_NS):
        return None
    body = payload[len(ISO_NS):]
    if len(body) < 4:
        return None
    out: dict = {"min_version": _u16(body, 0), "writer_version": _u16(body, 2),
                 "bytes": len(body), "stub": len(body) == 4}
    if out["stub"]:
        return out
    if len(body) < 5:
        return out
    flags = body[4]
    channels = 3 if flags & 0x80 else 1
    use_common = bool(flags & 0x08)
    out.update({"flags": flags, "channels": channels,
                "use_base_color_space": bool(flags & 0x40),
                "backward_direction": bool(flags & 0x04)})
    p = 5
    if use_common:
        if p + 4 > len(body):
            return out
        common = _u32(body, p)
        p += 4
        out["common_denominator"] = common

        def frac(name: str, signed: bool = False) -> None:
            nonlocal p
            if p + 4 > len(body):
                return
            n = struct.unpack_from(">i" if signed else ">I", body, p)[0]
            p += 4
            out[name] = n / common if common else float("nan")

        frac("base_headroom")
        frac("alt_headroom")
        for c in range(channels):
            frac(f"min{c}", True)
            frac(f"max{c}", True)
            frac(f"gamma{c}")
            frac(f"base_offset{c}", True)
            frac(f"alt_offset{c}", True)
    else:
        def pair(name: str) -> None:
            nonlocal p
            if p + 8 > len(body):
                return
            n, d = struct.unpack_from(">iI", body, p)
            p += 8
            out[name] = n / d if d else float("nan")
        pair("base_headroom")
        pair("alt_headroom")
        for c in range(channels):
            pair(f"min{c}")
            pair(f"max{c}")
            pair(f"gamma{c}")
            pair(f"base_offset{c}")
            pair(f"alt_offset{c}")
    return out


# --------------------------------------------------------------------------- Ultra HDR


def _check_xmp_params(xmp: dict, where: str, rep: Report) -> None:
    if xmp.get("Version") != "1.0":
        rep.err(f"{where} XMP: hdrgm:Version={xmp.get('Version')!r}（必须为 \"1.0\"）")
    if xmp.get("GainMapMax") is None:
        rep.err(f"{where} XMP: 缺少必填项 hdrgm:GainMapMax")
        return
    try:
        gmin = float(xmp.get("GainMapMin", "0"))
        gmax = float(xmp["GainMapMax"])
        gamma = float(xmp.get("Gamma", "1"))
        off_s = float(xmp.get("OffsetSDR", "0.015625"))
        off_h = float(xmp.get("OffsetHDR", "0.015625"))
        cap_min = float(xmp.get("HDRCapacityMin", "0"))
        cap_max = float(xmp.get("HDRCapacityMax", xmp["GainMapMax"]))
    except ValueError as exc:
        rep.err(f"{where} XMP: 数值解析失败（{exc}）")
        return
    if gmin > gmax:
        rep.err(f"{where} XMP: GainMapMin({gmin}) > GainMapMax({gmax})")
    if gamma <= 0:
        rep.err(f"{where} XMP: Gamma({gamma}) 必须 > 0")
    if off_s < 0 or off_h < 0:
        rep.err(f"{where} XMP: OffsetSDR/OffsetHDR 不能为负")
    if cap_max <= cap_min:
        rep.err(f"{where} XMP: HDRCapacityMax({cap_max}) 必须 > HDRCapacityMin({cap_min})")
    if xmp.get("BaseRenditionIsHDR") not in (None, "False"):
        rep.err(f"{where} XMP: BaseRenditionIsHDR={xmp['BaseRenditionIsHDR']!r}（必须为 False）")


def validate_ultrahdr(blob: bytes, path: str = "", deep: bool = True) -> Report:
    rep = Report("ultrahdr", path)
    try:
        segs = segments(blob)
    except ValueError as exc:
        rep.err(str(exc))
        return rep

    if not any(s["marker"] == 0xDA for s in segs):
        rep.err("找不到 SOS：文件可能在主图之前就被截断")
        return rep

    xmp_primary_segs = _app_payloads(blob, segs, 0xE1, XMP_ID)
    exif_segs = _app_payloads(blob, segs, 0xE1, EXIF_ID)
    mpf_segs = _app_payloads(blob, segs, 0xE2, MPF_SIG)
    iso_segs = _app_payloads(blob, segs, 0xE2, ISO_NS)
    icc_segs = _app_payloads(blob, segs, 0xE2, ICC_ID)

    if len(exif_segs) > 1:
        rep.warn(f"主图有 {len(exif_segs)} 个 Exif 段（重复元数据）")
    if len(xmp_primary_segs) > 1:
        rep.err(f"主图有 {len(xmp_primary_segs)} 个 XMP 段（必须唯一）")
    if len(mpf_segs) > 1:
        rep.err(f"主图有 {len(mpf_segs)} 个 MPF 段（必须唯一）")
    if not xmp_primary_segs:
        rep.err("主图缺少 XMP（无法识别为 Ultra HDR）")
    if not mpf_segs:
        rep.err("主图缺少 APP2 MPF（解码器无法定位增益图）")
    if not icc_segs:
        rep.warn("主图没有 ICC profile（规范要求 ICC 标色）")

    info: dict = {}
    xmp_text = ""
    if xmp_primary_segs:
        raw = xmp_primary_segs[0]["payload"][len(XMP_ID):]
        xmp_text = raw.decode("utf-8", "replace")
        primary_xmp = parse_hdrgm(xmp_text)
        info["primary_version"] = primary_xmp.get("Version")
        if primary_xmp.get("Version") != "1.0":
            rep.err("主图 XMP 缺少 hdrgm:Version=\"1.0\"（文件不会被识别为 Ultra HDR）")

    if not mpf_segs:
        return rep
    mpf = parse_mpf(blob, mpf_segs[0], rep)
    if mpf is None:
        return rep
    entries = mpf["entries"]
    info["mpf_endian"] = mpf["endian"]
    if len(entries) != 2:
        rep.err(f"MPEntry 条目数 {len(entries)}（应为 2）")
        return rep

    primary, secondary = entries
    if primary["attr"] & MASK != PRIMARY_ATTR:
        rep.err(f"主图 attr=0x{primary['attr']:08X}（低 28 位应为 0x00030000）")
    if primary["attr"] & ~MASK:
        rep.warn(f"主图 attr 高位非零：0x{primary['attr']:08X}")
    if primary["offset"] != 0:
        rep.err(f"主图 offset={primary['offset']}（必须为 0）")
    if secondary["attr"] & MASK != SECONDARY_ATTR:
        rep.err(f"增益图 attr=0x{secondary['attr']:08X}（低 28 位应为 0x00000000）")

    gain_start = secondary["abs_offset"]
    gain_end = gain_start + secondary["size"]
    if gain_start < 0 or gain_start >= len(blob):
        rep.err(f"增益图偏移 {gain_start} 超出文件长度 {len(blob)}")
        return rep
    if xmp_primary_segs and gain_start < xmp_primary_segs[0]["payload_end"]:
        rep.err("增益图偏移落在主图头部之内")
    gain = blob[gain_start:gain_end]
    if len(gain) != secondary["size"]:
        rep.err(f"增益图被截断：声明 {secondary['size']} 字节，实际 {len(gain)}")
    if not gain.startswith(SOI):
        rep.err("增益图偏移处不是 JPEG（缺 SOI）")
    if primary["size"] != gain_start:
        rep.warn(f"主图 size={primary['size']} 与实际主图长度 {gain_start} 不一致"
                 "（libultrahdr 容忍，但部分解码器可能不信）")

    info.update({"primary_len": gain_start, "gain_len": len(gain),
                 "gain_offset": gain_start, "file_len": len(blob)})
    if gain_end != len(blob):
        rep.warn(f"增益图之后还有 {len(blob) - gain_end} 字节数据")

    # 主图尺寸
    try:
        with Image.open(io.BytesIO(blob[:gain_start])) as im:
            info["primary_size"] = f"{im.width}x{im.height}"
            primary_size = (im.width, im.height)
    except Exception as exc:                       # noqa: BLE001
        rep.err(f"主图无法解析：{exc}")
        primary_size = None

    # 增益图 JPEG
    if deep and gain.startswith(SOI):
        try:
            with Image.open(io.BytesIO(gain)) as gm:
                gm.load()
                info["gain_size"] = f"{gm.width}x{gm.height}"
                info["gain_mode"] = gm.mode
                if gm.mode not in ("L", "I;16", "I"):
                    rep.warn(f"增益图 mode={gm.mode}（单通道灰度最通用）")
                if primary_size and gm.width and gm.height:
                    ar_g = gm.width / gm.height
                    ar_p = primary_size[0] / primary_size[1]
                    if abs(ar_g - ar_p) / ar_p > 0.01:
                        rep.warn(f"增益图宽高比 {ar_g:.4f} 与主图 {ar_p:.4f} 不一致")
        except Exception as exc:                   # noqa: BLE001
            rep.err(f"增益图不是可解码的 JPEG：{exc}")

    # 增益图内部的 XMP / ISO
    try:
        gsegs = segments(gain)
    except ValueError:
        gsegs = []
    gxmp_segs = _app_payloads(gain, gsegs, 0xE1, XMP_ID)
    giso_segs = _app_payloads(gain, gsegs, 0xE2, ISO_NS)
    if not gxmp_segs:
        rep.err("增益图缺少 XMP（规范要求 hdrgm 元数据写在增益图的 XMP 里）")
        gain_xmp: dict = {}
    else:
        gain_xmp = parse_hdrgm(gxmp_segs[0]["payload"][len(XMP_ID):].decode("utf-8", "replace"))
        _check_xmp_params(gain_xmp, "增益图", rep)
    info["gainmap_max"] = gain_xmp.get("GainMapMax")

    # 主图的 GContainer
    if xmp_text:
        items = parse_container(xmp_text)
        info["container_items"] = [i.get("semantic") for i in items]
        if not items:
            rep.err("主图 XMP 里没有 Container:Directory 条目")
        else:
            if items[0].get("semantic") != "Primary":
                rep.err(f"目录第一项是 {items[0].get('semantic')!r}（Primary 必须第一）")
            if sum(1 for i in items if i.get("semantic") == "Primary") != 1:
                rep.err("Primary 条目必须恰好一个")
            if sum(1 for i in items if i.get("semantic") == "GainMap") > 1:
                rep.err("GainMap 条目最多一个")
            if items[0].get("mime") not in (None, "image/jpeg"):
                rep.warn(f"Primary Item:Mime={items[0].get('mime')!r}（应为 image/jpeg）")
            if len(items) > 1:
                gm_item = items[1]
                if gm_item.get("semantic") != "GainMap":
                    rep.err(f"第二项是 {gm_item.get('semantic')!r}（应为 GainMap）")
                length = gm_item.get("length")
                if length is None:
                    rep.err("GainMap 条目缺少 Item:Length（secondary 必填）")
                elif not length.isdigit() or int(length) <= 0:
                    rep.err(f"GainMap Item:Length={length!r} 非法")
                elif int(length) != len(gain):
                    rep.err(f"GainMap Item:Length={length} 与实际增益图长度 {len(gain)} 不符")

    # 主图的 ISO 存根
    if iso_segs:
        prime_iso = parse_iso(iso_segs[0]["payload"])
        if prime_iso is None:
            rep.err("主图 ISO 元数据段不完整")
        elif not prime_iso["stub"]:
            rep.warn(f"主图写了完整 ISO 元数据（{prime_iso['bytes']} 字节），"
                     "libultrahdr 只在主图写 4 字节版本存根")
        elif prime_iso["min_version"] != 0:
            rep.err(f"主图 ISO min_version={prime_iso['min_version']}（应为 0）")
    else:
        rep.info["iso_primary"] = "无（Ultra HDR 1.0 旧文件，或非本工具产物）"

    # 增益图的 ISO 完整元数据
    iso = None
    if giso_segs:
        iso = parse_iso(giso_segs[0]["payload"])
        if iso is None or iso.get("stub"):
            rep.err("增益图 ISO 段只有命名空间/版本，缺完整 61 字节元数据")
        else:
            info["iso"] = {k: iso[k] for k in
                           ("min_version", "writer_version", "channels", "use_base_color_space",
                            "base_headroom", "alt_headroom", "min0", "max0", "gamma0",
                            "base_offset0", "alt_offset0") if k in iso}
            if iso["min_version"] != 0:
                rep.err(f"ISO min_version={iso['min_version']}（必须为 0）")
            if iso["channels"] != 1:
                rep.warn(f"ISO 声明 {iso['channels']} 个通道，但增益图是单通道灰度")
            if not iso["use_base_color_space"]:
                rep.warn("ISO flags 未置 useBaseColorSpace（bit6）：增益图会被认为在 alternate 空间")
            for key in ("base_headroom", "alt_headroom"):
                if np.isnan(iso.get(key, 0.0)):
                    rep.err(f"ISO {key} 分母为 0")
            for c in range(iso["channels"]):
                if iso.get(f"max{c}", 0) < iso.get(f"min{c}", 0):
                    rep.err(f"ISO 通道{c} max < min")
                if iso.get(f"gamma{c}", 0) <= 0:
                    rep.err(f"ISO 通道{c} gamma <= 0")
            if gain_xmp.get("GainMapMax"):
                try:
                    xmax = float(gain_xmp["GainMapMax"])
                    xmin = float(gain_xmp.get("GainMapMin", "0"))
                    xsdr = float(gain_xmp.get("OffsetSDR", "0.015625"))
                    if abs(iso.get("alt_headroom", xmax) - xmax) > 1e-6:
                        rep.warn(f"ISO alt_headroom={iso.get('alt_headroom'):.8f} 与 "
                                 f"XMP GainMapMax={xmax:.8f} 不一致")
                    if abs(iso.get("max0", xmax) - xmax) > 1e-6:
                        rep.warn(f"ISO gain_map_max={iso.get('max0'):.8f} 与 "
                                 f"XMP GainMapMax={xmax:.8f} 不一致")
                    if abs(iso.get("min0", xmin) - xmin) > 1e-6:
                        rep.warn(f"ISO gain_map_min={iso.get('min0'):.8f} 与 "
                                 f"XMP GainMapMin={xmin:.8f} 不一致")
                    if abs(iso.get("base_offset0", xsdr) - xsdr) > 1e-6:
                        rep.warn(f"ISO base_offset={iso.get('base_offset0'):.8f} 与 "
                                 f"XMP OffsetSDR={xsdr:.8f} 不一致")
                except ValueError:
                    pass
    else:
        rep.info["iso_gain"] = "无（解码端回退读 XMP hdrgm；默认路径）"

    rep.info.update(info)
    return rep


# --------------------------------------------------------------------------- PNG


def validate_png16(blob: bytes, path: str = "", deep: bool = True) -> Report:
    rep = Report("png16", path)
    if blob[:8] != b"\x89PNG\r\n\x1a\n":
        rep.err("不是 PNG（签名错误）")
        return rep
    pos = 8
    chunks: dict[str, list[bytes]] = {}
    idat = bytearray()
    w = h = bits = ctype = interlace = 0
    while pos + 8 <= len(blob):
        length = _u32(blob, pos)
        tag = blob[pos + 4:pos + 8]
        data = blob[pos + 8:pos + 8 + length]
        if len(data) != length:
            rep.err(f"块 {tag!r} 声明 {length} 字节，实际 {len(data)}")
            break
        if tag == b"IDAT":
            idat += data
        else:
            chunks.setdefault(tag.decode("latin1"), []).append(data)
        if tag == b"IHDR":
            if length != 13:
                rep.err("IHDR 长度不是 13")
            w, h = _u32(data, 0), _u32(data, 4)
            bits, ctype, comp, filt, interlace = data[8], data[9], data[10], data[11], data[12]
            if bits != 16 or ctype != 2:
                rep.err(f"IHDR 为 {bits}bit/colortype {ctype}（应为 16bit/2=RGB）")
            if comp != 0 or filt != 0:
                rep.err(f"IHDR 压缩/滤波方法异常：{comp}/{filt}")
        pos += 12 + length
    if not idat:
        rep.err("没有 IDAT")
        return rep

    rep.info.update({"size": f"{w}x{h}", "bits": bits, "colortype": ctype,
                     "interlace": interlace, "bytes": len(blob)})

    cicp = chunks.get("cICP", [None])[0]
    if cicp is None:
        rep.err("缺少 cICP（播放器无法知道这是 BT.2020 + PQ）")
    elif len(cicp) != 4:
        rep.err(f"cICP 长度 {len(cicp)}（应为 4）")
    elif tuple(cicp) not in ((9, 16, 0, 1), (9, 18, 0, 1)):
        rep.err(f"cICP={tuple(cicp)}（应为 (9,16,0,1)=BT.2020/PQ 或 (9,18,0,1)=BT.2020/HLG）")

    phys = chunks.get("pHYs", [None])[0]
    if phys is None:
        rep.warn("缺少 pHYs（DPI 信息丢失）")
    elif len(phys) == 9:
        rep.info["dpi"] = round(_u32(phys, 0) * 0.0254, 1)
        if phys[8] != 1:
            rep.warn(f"pHYs 单位={phys[8]}（应为 1=米）")

    if deep:
        try:
            raw = zlib.decompress(bytes(idat))
        except zlib.error as exc:
            rep.err(f"IDAT 解压失败：{exc}")
            return rep
        stride = 1 + w * 6
        expect = stride * h
        if len(raw) != expect:
            rep.err(f"IDAT 解压后 {len(raw)} 字节（应为 {expect}）")
        else:
            kinds = {raw[y * stride] for y in range(h)}
            bad = {k for k in kinds if k > 4}
            if bad:
                rep.err(f"非法的行滤波类型：{sorted(bad)}")
    return rep


# --------------------------------------------------------------------------- HEIC


def validate_heic(blob: bytes, path: str = "", expect_bits: int | None = None,
                  deep: bool = True) -> Report:
    """HEIC 校验。8bit 且 sRGB 传递曲线视为 SDR 变体，其余按 10bit PQ/HLG 要求。"""
    rep = Report("heic", path)
    try:
        from . import heic10
        arr, meta = heic10.decode(blob)
    except Exception as exc:                       # noqa: BLE001
        rep.err(f"HEIC 解码失败：{exc}")
        return rep
    rep.info.update({"size": f"{arr.shape[1]}x{arr.shape[0]}", **meta})
    nclx = meta.get("nclx") or {}
    tc = nclx.get("transfer_characteristics")
    cp = nclx.get("color_primaries")
    tc_val = getattr(tc, "value", tc)
    cp_val = getattr(cp, "value", cp)
    bits = meta.get("bit_depth")
    sdr = bits == 8 and tc_val not in (16, 18)
    if sdr:
        rep.kind = "heic-sdr"
        if cp_val not in (1, 2, None):
            rep.warn(f"nclx color_primaries={cp_val}（SDR 应为 1=sRGB，2=unspecified）")
        if tc_val is not None and tc_val not in (13, 1, 2, 6, 14):
            rep.warn(f"nclx transfer_characteristics={tc_val}（SDR 通常是 13=sRGB）")
        return rep

    want = expect_bits
    if want is not None and bits != want:
        rep.err(f"位深 {bits}（应为 {want}）")
    elif want is None and bits not in (10, 12):
        rep.err(f"位深 {bits}（HDR HEIC 应为 10 或 12）")
    if cp_val not in (9, None):
        rep.warn(f"nclx color_primaries={cp_val}（应为 9=BT.2020）")
    if tc_val is not None and tc_val not in (16, 18):
        rep.err(f"nclx transfer_characteristics={tc_val}（应为 16=PQ 或 18=HLG）")
    mat = nclx.get("matrix_coefficients")
    mat_val = getattr(mat, "value", mat)
    if mat_val not in (9, None):
        rep.err(f"nclx matrix_coefficients={mat_val}（BT.2020 内容应为 9，"
                f"写成 6(BT.601) 会让解码器按 601 反矩阵、颜色偏）")
    if deep and arr.size:
        peak = int(arr.max())
        rep.info["code_max"] = peak
        if peak == 0:
            rep.err("HDR 图像全黑")
        if peak < 0.45 * 65535:
            rep.warn(f"最大码值 {peak} 偏低（PQ 白点 203nits≈0.58×65535）")
    return rep


# --------------------------------------------------------------------------- 分发


def validate_jpeg(blob: bytes, path: str = "") -> Report:
    """普通 JPEG：确认结构完整即可；带 MPF/XMP 时转交 Ultra HDR 校验。"""
    rep = Report("jpeg", path)
    try:
        segs = segments(blob)
    except ValueError as exc:
        rep.err(str(exc))
        return rep
    if not any(s["marker"] == 0xDA for s in segs):
        rep.err("找不到 SOS")
        return rep
    if not blob.rstrip(b"\x00").endswith(b"\xff\xd9"):
        rep.warn("文件末尾没有 EOI（可能被截断）")
    try:
        with Image.open(io.BytesIO(blob)) as im:
            rep.info.update({"size": f"{im.width}x{im.height}", "mode": im.mode,
                             "bytes": len(blob)})
            dpi = im.info.get("dpi")
            if dpi:
                rep.info["dpi"] = round(float(dpi[0]), 1)
    except Exception as exc:                       # noqa: BLE001
        rep.err(f"JPEG 解码失败：{exc}")
    return rep


def validate_avif(blob: bytes, path: str = "", deep: bool = True,
                  expect_transfer: tuple[int, ...] = (16, 18)) -> Report:
    rep = Report("avif", path)
    from . import avif as avifmod

    brand = blob[8:12].decode("latin1")
    rep.info.update({"brand": brand, "bytes": len(blob)})
    if brand not in ("avif", "avis"):
        rep.err(f"ftyp major brand={brand}（应为 avif）")

    nclx = avifmod.read_nclx(blob)
    rep.info["nclx"] = nclx
    if not nclx:
        rep.err("缺少 colr/nclx 色彩标签（播放器只能猜色彩和传递函数）")
    else:
        if nclx["primaries"] != 9:
            rep.err(f"nclx primaries={nclx['primaries']}（应为 9=BT.2020）")
        if nclx["transfer"] not in expect_transfer:
            rep.err(f"nclx transfer={nclx['transfer']}（应为 16=PQ 或 18=HLG）")
        if nclx["matrix"] not in (9, 10):
            rep.warn(f"nclx matrix={nclx['matrix']}（BT.2020 非恒定亮度应为 9）")

    bits = avifmod.pixi_bits(blob)
    rep.info["pixi"] = bits
    if not bits:
        rep.warn("缺少 pixi box，位深只能由解码器推断")
    elif max(bits) not in (10, 12):
        rep.err(f"pixi 位深 {bits}（应为 10 或 12）")

    if deep:
        try:
            arr = avifmod.decode(blob)
            rep.info["size"] = f"{arr.shape[1]}x{arr.shape[0]}"
            peak = int(arr.max())
            rep.info["code_max"] = peak
            if peak == 0:
                rep.err("AVIF 图像全黑")
            elif peak < 0.45 * 65535:
                rep.warn(f"最大码值 {peak} 偏低（PQ 白点 203nits≈0.58×65535）")
        except Exception as exc:                   # noqa: BLE001
            rep.err(f"AVIF 解码失败：{exc}")
        if bits and max(bits) == 12:
            rep.info["note"] = "12bit AVIF：解码端支持有限，桌面 Chrome/Safari 可看，Windows 需扩展"
    return rep


def validate_jxl(blob: bytes, path: str = "", deep: bool = True,
                 expect_transfer: str | None = None) -> Report:
    """检查 24bit JXL 的容器签名、位深与 Rec.2100 色彩编码。

    位深/传递函数只能靠 jxlinfo 读；没有 jxlinfo 时退化为签名检查并给出警告，
    不把"文件能打开"误报成"24bit 正确"。
    """
    from . import jxl as jxlmod

    rep = Report("jxl", path)
    rep.info["bytes"] = len(blob)
    if not (blob.startswith(jxlmod.CONTAINER_MAGIC) or blob.startswith(jxlmod.CODESREAM_MAGIC)):
        rep.err("不是 JPEG XL（容器/码流签名错误）")
        return rep

    if not deep:
        return rep
    try:
        info = jxlmod.info(path)
    except Exception as exc:                       # noqa: BLE001
        rep.err(f"jxlinfo 读取失败：{exc}")
        return rep
    if not info:
        rep.warn("未找到 jxlinfo，无法校验位深与传递函数（设 HDRGAINMAP_JXLINFO 或放入 PATH）")
        return rep
    rep.info.update({k: v for k, v in info.items() if k != "raw"})
    bits = info.get("bits")
    if bits != 24:
        rep.err(f"位深 {bits}bit（应为 24bit）")
    transfer = str(info.get("transfer", ""))
    if expect_transfer:
        if not transfer.upper().startswith(expect_transfer):
            rep.err(f"传递函数 {transfer!r}（应为 {expect_transfer}）")
    elif not transfer.upper().startswith(("PQ", "HLG")):
        rep.err(f"传递函数 {transfer!r}（应为 PQ 或 HLG）")
    primaries = str(info.get("primaries", ""))
    if primaries and "2100" not in primaries:
        rep.err(f"原色 {primaries!r}（应为 Rec.2100）")
    return rep


def validate_file(path: str, deep: bool = True) -> Report:
    with open(path, "rb") as fh:
        blob = fh.read()
    if blob.startswith(b"\xff\xd8"):
        if b"MPF\x00" in blob[:4096] or b"hdrgm:Version" in blob[:65536]:
            return validate_ultrahdr(blob, path, deep=deep)
        return validate_jpeg(blob, path)
    from . import jxl as jxlmod
    if blob.startswith(jxlmod.CONTAINER_MAGIC) or blob.startswith(jxlmod.CODESREAM_MAGIC):
        lower = path.lower()
        expect = "PQ" if lower.endswith("_pq.jxl") else "HLG" if lower.endswith("_hlg.jxl") else None
        return validate_jxl(blob, path, deep=deep, expect_transfer=expect)
    if blob[:8] == b"\x89PNG\r\n\x1a\n":
        return validate_png16(blob, path, deep=deep)
    if blob[4:8] == b"ftyp":
        brand = blob[8:12]
        if brand in (b"avif", b"avis"):
            return validate_avif(blob, path, deep=deep)
        return validate_heic(blob, path, deep=deep)
    rep = Report("unknown", path)
    rep.err("无法识别的文件类型（既不是 JPEG / PNG，也不是 HEIF）")
    return rep


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    bad = 0
    for path in args:
        rep = validate_file(path)
        print(rep.format())
        bad += 0 if rep.ok else 1
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
