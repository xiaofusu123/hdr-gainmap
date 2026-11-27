"""hdr-gainmap：把 8bit SDR 插画转成 HDR 母版。

color 色彩与传递函数；light 光场分析；gainmap 发光判据与分通道权重；
pipeline 一条龙；ultrahdr / png16 / heic10 / avif / jxl 五种容器；validate 校验；cli 命令行。
"""
__version__ = "0.2.0b1"

from . import avif, color, gainmap, heic10, jxl, png16, ultrahdr  # noqa: F401
from .pipeline import FORMATS, QUALITY, build               # noqa: F401
