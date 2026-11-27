"""命令行入口：python -m hdrgainmap <源图...> [-o 输出目录] [选项]。

不设档位：增益参数按每张图自动计算；支持一次处理多张图。
"""
from __future__ import annotations

import argparse
import json
import sys

from . import __version__
from .pipeline import FORMATS, QUALITY, build

FORMAT_HELP = {
    "jpeg": "Ultra HDR JPEG（全分辨率 RGB 增益图 + MPF + XMP + 三通道 ISO 21496-1）",
    "png": "16bit 无损母版（PQ + BT.2020，cICP 9/16/0/1）",
    "heic10": "10bit HEVC，兼容性最好",
    "heic12": "12bit HEVC，色深最高（Windows 需 HEVC 扩展）",
    "avif": "12bit AV1 4:4:4，体积最小",
    "jxl": "24bit JPEG XL（PQ + BT.2020，无损，需要 cjxl）",
}


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hdrgainmap",
        description="把 8bit SDR 插画转成 HDR 母版：Ultra HDR JPEG / 16bit PNG / 10bit & 12bit HEIC / 12bit AVIF / 24bit JPEG XL。",
        epilog="例：python -m hdrgainmap a.png b.jpg -o out -f jpeg,png,heic10,heic12,avif,jxl",
    )
    p.add_argument("src", nargs="+", help="源图路径（PNG/JPEG 等，可带 alpha），可一次给多张")
    p.add_argument("-o", "--outdir", default="out", help="输出目录（默认 out）")
    p.add_argument("-f", "--formats", default=",".join(FORMATS),
                   help=f"逗号分隔，可选：{','.join(FORMATS)}（默认全部）")
    p.add_argument("-q", "--quality", type=int, default=QUALITY, choices=range(50, 101),
                   metavar="50-100", help=f"有损编码质量（默认 {QUALITY}）")
    p.add_argument("--dpi", type=float, default=None, help="输出 DPI（默认沿用源图）")
    p.add_argument("--alpha-bg", default="black", choices=["black", "white"],
                   help="透明图合成底色（默认 black）")
    p.add_argument("--validate", action="store_true",
                   help="生成后对每个产物跑一遍合规校验")
    p.add_argument("--debug", action="store_true",
                   help="额外落盘 _emis / _gainmap / _gainmap_embedded 调试图")
    p.add_argument("-j", "--jobs", type=int, default=4, metavar="N",
                   help="并行编码的格式数 / 各编码器线程数（默认 4）")
    p.add_argument("--gpu", default="auto", choices=["auto", "on", "off"],
                   help="GPU 加速（WebGPU；默认 auto，无可用显卡时自动回退 CPU）")
    p.add_argument("--quiet", action="store_true", help="不打印进度")
    p.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    p.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    return p


def _run_one(args, src: str, alpha_bg: tuple[int, int, int], log) -> dict:
    """处理一张图；校验结果也合并进返回值。"""
    formats = tuple(s.strip().lower() for s in args.formats.split(",") if s.strip())
    res = build(src, args.outdir, formats=formats, quality=args.quality,
                dpi=args.dpi, debug=args.debug, alpha_bg=alpha_bg,
                use_gpu=args.gpu != "off", jobs=max(1, args.jobs), log=log)
    if args.validate:
        from concurrent.futures import ThreadPoolExecutor

        from . import validate
        items = list(res["files"].items())
        with ThreadPoolExecutor(max_workers=max(1, min(args.jobs, len(items)))) as pool:
            reports = dict(zip((k for k, _ in items),
                               pool.map(lambda kv: validate.validate_file(kv[1]).as_dict(),
                                        items)))
        res["report"] = reports
        res["validation_ok"] = all(r["ok"] for r in reports.values())
        res["ok"] = bool(res["ok"]) and res["validation_ok"]
    return res


def _print_human(res: dict, args, reports: dict) -> None:
    s = res["stats"]
    print(f"源图   : {res['src']}")
    print(f"参数   : gmax={res['gmax']:.2f} EV（按光场自适应）  dpi={res['dpi']:g}"
          f"  {res['transfer']}  质量 {args.quality}")
    print(f"增益   : 均值 {s['gain_mean']:.2f}×  最大 {s['gain_max']:.2f}×  "
          f"不动 {s['untouched_pct']:.1f}%  >2× {s['gt2x_pct']:.1f}%")
    print(f"峰值   : {s['peak_nits']:.0f} nits（SDR 白 203 nits）")
    if res.get("gpu"):
        print(f"加速   : GPU {res['gpu']['adapter']}  并行 {res.get('workers', 1)} 路")
    for k, v in res["files"].items():
        line = f"  {k:7s} {v}"
        if k in reports:
            line += "   [校验通过]" if reports[k]["ok"] else "   [校验失败]"
        print(line)
    for k, err in res.get("errors", {}).items():
        print(f"  ! {k}: {err}")
    timings = res.get("timings", {})
    if timings:
        print("耗时   : " + "  ".join(f"{k} {v:.2f}s" for k, v in timings.items()))


def main(argv: list[str] | None = None) -> int:
    p = _parser()
    args = p.parse_args(argv)

    formats = tuple(s.strip().lower() for s in args.formats.split(",") if s.strip())
    bad = [f for f in formats if f not in FORMATS]
    if bad:
        p.error(f"不支持的格式：{', '.join(bad)}；可选 {', '.join(FORMATS)}")
    if not formats:
        p.error("至少选择一种输出格式")

    alpha_bg = (255, 255, 255) if args.alpha_bg == "white" else (0, 0, 0)
    log = None if args.quiet else (lambda msg: print(msg, file=sys.stderr))

    results: list[dict] = []
    for src in args.src:
        try:
            res = _run_one(args, src, alpha_bg, log)
        except Exception as exc:                 # noqa: BLE001  一张失败不影响其余
            res = {"src": src, "ok": False, "files": {}, "errors": {"build": str(exc)},
                   "stats": {}}
        results.append(res)

    ok = all(r.get("ok", True) for r in results)
    if args.json:
        payload = results[0] if len(results) == 1 else {"results": results, "ok": ok}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0 if ok else 1

    for i, res in enumerate(results):
        if len(results) > 1:
            if i:
                print()
            print(f"=== [{i + 1}/{len(results)}] {res.get('src', '')} ===")
        if res.get("stats"):
            _print_human(res, args, res.get("report", {}))
        else:
            print(f"源图   : {res.get('src', '')}")
            for k, err in res.get("errors", {}).items():
                print(f"  ! {k}: {err}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
