"""命令行入口：python -m hdrgainmap <源图> [-o 输出目录] [选项]。"""
from __future__ import annotations

import argparse
import json
import sys

from . import gainmap
from .pipeline import build

FORMATS = ("jpg", "jpeg", "png", "heic", "heic_sdr")
PROFILE_HELP = {
    "strong": "实机推荐：深色背景 + 明确光源，峰值约 1200 nits",
    "detail": "高调素材：按局部结构选点（蕾丝/发丝），峰值约 600 nits",
    "lights": "只点光源，最克制",
    "soft": "温和整体提亮",
    "night": "夜景，阈值更低",
}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hdrgainmap",
        description="把 8bit SDR 插画转成 HDR：Ultra HDR JPEG / 16bit PQ PNG / 10bit HEIC。",
        epilog="例：python -m hdrgainmap a.png -o out -p strong -f jpg,heic_sdr",
    )
    p.add_argument("src", help="源图路径（PNG/JPEG，可带 alpha）")
    p.add_argument("-o", "--outdir", default="out", help="输出目录（默认 out）")
    p.add_argument("-p", "--profile", default="strong", choices=sorted(gainmap.PROFILES),
                   help="；".join(f"{k}={v}" for k, v in PROFILE_HELP.items()))
    p.add_argument("-f", "--formats", default="jpg,png,heic",
                   help=f"逗号分隔，可选：{','.join(FORMATS)}；jpg 与 jpeg 等价（默认 jpg,png,heic）")
    p.add_argument("--dpi", type=float, default=None, help="输出 DPI（默认沿用源图）")
    p.add_argument("--gmax", type=float, default=None,
                   help="最大增益档，单位 EV（默认取档位预设；2.0 = 4×）")
    p.add_argument("--transfer", default="PQ", choices=["PQ", "HLG"],
                   help="PNG/HEIC 传递函数（默认 PQ）")
    p.add_argument("--no-preview", action="store_true", help="不生成 SDR 模拟预览图")
    p.add_argument("--debug", action="store_true", help="额外落盘 base.jpg 与 gainmap.png，便于排查")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    return p


def main(argv: list[str] | None = None) -> int:
    p = _parser()
    args = p.parse_args(argv)

    formats = tuple(s.strip().lower() for s in args.formats.split(",") if s.strip())
    bad = [f for f in formats if f not in FORMATS]
    if bad:
        p.error(f"不支持的格式：{', '.join(bad)}；可选 {', '.join(FORMATS)}")

    res = build(args.src, args.outdir, profile=args.profile, formats=formats,
                dpi=args.dpi, gmax=args.gmax, transfer=args.transfer,
                preview=not args.no_preview, debug=args.debug)

    if args.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
        return 0

    s = res["stats"]
    if "sdr_only" in s:
        print(f"源图   : {res['src']}")
        print("模式   : 只换容器（8bit sRGB → HEIC，不做 HDR）")
    else:
        print(f"源图   : {res['src']}")
        print(f"档位   : {res['profile']}  gmax={res['gmax']} EV  dpi={res['dpi']:g}")
        print(f"增益   : 均值 {s['gain_mean']:.2f}×  最大 {s['gain_max']:.2f}×  "
              f"不动 {s['untouched_pct']:.1f}%  >2× {s['gt2x_pct']:.1f}%")
        if "peak_nits" in s:
            print(f"峰值   : {s['peak_nits']:.0f} nits（SDR 白 203 nits）")
        if "changed_gt2_pct" in s:
            print(f"可见变化: >2/255 {s['changed_gt2_pct']:.2f}%   "
                  f">16/255 {s['changed_gt16_pct']:.2f}%")
    for k, v in res["files"].items():
        print(f"  {k:8s} {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
