"""hdrgainmap —— 把 8bit SDR 插画转成 HDR 的小工具。

模块划分：
  color     色彩空间与传递函数（sRGB / PQ / HLG / BT.2020）
  gainmap   启发式"发光体分离"增益图
  ultrahdr  Ultra HDR（ISO 21496-1）JPEG 封装与解析
  png16     16bit PQ 的 HDR PNG
  heic10    10bit PQ/HLG 的 HDR HEIC
  pipeline  一条龙构建
  cli       命令行入口
"""
__version__ = "1.0.0"

from .pipeline import build          # noqa: F401
from . import color, gainmap, heic10, png16, ultrahdr  # noqa: F401
