# -*- coding: utf-8 -*-
"""AI 包：云端优先，本地可切"""
from .base import AIError, Provider, build, status, strip_think
from .tasks import (advisory, diagnose, parse_json, summarize, tag_many,
                    theme_many, THEME_LABELS)

__all__ = ["AIError", "Provider", "build", "status", "strip_think",
           "diagnose", "advisory", "summarize", "tag_many", "parse_json",
           "theme_many", "THEME_LABELS"]