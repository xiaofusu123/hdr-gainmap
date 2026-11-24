"""命令行入口：python -m hdrgainmap <源图> [-o 输出目录] [选项]。

不设"档位"：增益参数按每张图的亮度分布自动计算；
用户只需要选输出格式和质量。
"""
from __future__ import annotations

import argparse
import json
import sys

from .pipeline import FORMATS, QUALITY, build

FORMAT_HELP = {
    "jpeg": "Ultra HDR JPEG（主图 + 增益图 + MPF + XMP + ISO 21496-1），安卓原生",
    "png": "16bit 无损母版（PQ/HLG，cICP 9/16/0/1）",
    "heic10": "10bit HEVC，兼容性最好",
    "heic12": "12bit HEVC，色深最高（Windows 需 HEVC 扩展）",
    "avif": "12bit AV1，体积最小",
}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hdrgainmap",
        description="把 8bit SDR 插画转成 HDR 母版：Ultra HDR JPEG / 16bit PNG / 10bit & 12bit HEIC / 12bit AVIF。",
        epilog="例：python -m hdrgainmap a.png -o out -f jpeg,png,heic10,heic12,avif",
    )
    p.add_argument("src", help="源图路径（PNG/JPEG，可带 alpha）")
    p.add_argument("-o", "--outdir", default="out", help="输出目录（默认 out）")
    p.add_argument("-f", "--formats", default=",".join(FORMATS),
                   help=f"逗号分隔，可选：{','.join(FORMATS)}（默认全部）")
    p.add_argument("-q", "--quality", type=int, default=QUALITY, choices=range(50, 101),
                   metavar="50-100", help=f"有损编码质量（默认 {QUALITY}）")
    p.add_argument("--dpi", type=float, default=None, help="输出 DPI（默认沿用源图）")
    p.add_argument("--transfer", default="PQ", choices=["PQ", "HLG"],
                   help="传递函数（默认 PQ）")
    p.add_argument("--validate", action="store_true",
                   help="生成后对每个产物跑一遍合规校验")
    p.add_argument("--debug", action="store_true", help="额外落盘 gainmap.png，便于排查")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return p


def main(argv: list[str] | None = None) -> int:
    p = _parser()
    args = p.parse_args(argv)

    formats = tuple(s.strip().lower() for s in args.formats.split(",") if s.strip())
    bad = [f for f in formats if f not in FORMATS]
    if bad:
        p.error(f"不支持的格式：{', '.join(bad)}；可选 {', '.join(FORMATS)}")

    res = build(args.src, args.outdir, formats=formats, quality=args.quality,
                dpi=args.dpi, transfer=args.transfer, debug=args.debug)

    reports = {}
    if args.validate:
        from . import validate
        for kind, path in res["files"].items():
            rep = validate.validate_file(path)
            reports[kind] = rep.as_dict()
            res.setdefault("report", {})[kind] = rep.as_dict()
        res["ok"] = all(r["ok"] for r in reports.values())

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0 if res.get("ok", True) else 1

    s = res["stats"]
    print(f"源图   : {res['src']}")
    print(f"参数   : gmax={res['gmax']:.2f} EV（按光影自适应）  dpi={res['dpi']:g}"
          f"  {res['transfer']}  质量 {args.quality}")
    print(f"增益   : 均值 {s['gain_mean']:.2f}×  最大 {s['gain_max']:.2f}×  "
          f"不动 {s['untouched_pct']:.1f}%  >2× {s['gt2x_pct']:.1f}%")
    print(f"峰值   : {s['peak_nits']:.0f} nits（SDR 白 203 nits）")
    for k, v in res["files"].items():
        line = f"  {k:7s} {v}"
        if k in reports:
            line += "   [校验通过]" if reports[k]["ok"] else "   [校验失败]"
        print(line)
    return 0 if res.get("ok", True) else 1


if __name__ == "__main__":
    sys.exit(main())
