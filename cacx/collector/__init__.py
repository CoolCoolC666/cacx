# -*- coding: utf-8 -*-
"""采集器包"""
from .base import SiteCollector, html_to_text
from .cac import CacCollector
from .zjwx import ZjwxCollector

# 内置采集器（代码实现）；自定义站点走 GenericCollector，配置驱动
COLLECTORS = {"cac": CacCollector, "zjwx": ZjwxCollector}

__all__ = ["SiteCollector", "html_to_text", "CacCollector",
           "ZjwxCollector", "COLLECTORS", "GenericCollector"]


def __getattr__(name):
    # 延迟导入：generic 依赖 bs4，别让只想用内置采集器的调用方被迫装它
    if name == "GenericCollector":
        from .generic import GenericCollector
        return GenericCollector
    raise AttributeError(name)
