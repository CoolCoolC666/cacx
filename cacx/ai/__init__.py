# -*- coding: utf-8 -*-
"""AI 包：云端优先，本地可切"""
from .base import AIError, Provider, build, status, strip_think
from .tasks import diagnose, parse_json, summarize, tag_many

__all__ = ["AIError", "Provider", "build", "status", "strip_think",
           "diagnose", "summarize", "tag_many", "parse_json"]