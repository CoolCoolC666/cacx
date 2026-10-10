# -*- coding: utf-8 -*-
"""采集器包"""
from .base import SiteCollector, html_to_text
from .cac import CacCollector
from .zjwx import ZjwxCollector

COLLECTORS = {"cac": CacCollector, "zjwx": ZjwxCollector}

__all__ = ["SiteCollector", "html_to_text", "CacCollector",
           "ZjwxCollector", "COLLECTORS"]