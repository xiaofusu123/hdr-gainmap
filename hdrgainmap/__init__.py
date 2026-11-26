"""hdrgainmap —— 把 8bit SDR 插画转成 HDR 母版。

模块划分：
  color     色彩空间与传递函数（sRGB / PQ / HLG / BT.2020）
  gainmap   自适应"发光体分离"增益（阈值按每张图的亮度分布计算）
  png16     16bit PQ/HLG 的 HDR PNG（cICP 9/16/0/1）
  heic10    10/12bit PQ/HLG 的 HDR HEIC（nclx 9/16/9）
  avif      12bit PQ/HLG 的 HDR AVIF（ffmpeg + libaom，nclx 9/16/9）
  ultrahdr  Ultra HDR JPEG（主图 + 增益图 + MPF + XMP + ISO 21496-1）封装与解析
  validate  产物合规校验（PNG cICP / HEIC nclx / AVIF nclx / Ultra HDR）
  pipeline  一条龙构建
  cli       命令行入口
"""
__version__ = "0.1.1b1"

from . import avif, color, gainmap, heic10, png16, ultrahdr  # noqa: F401
from .pipeline import FORMATS, QUALITY, build               # noqa: F401
