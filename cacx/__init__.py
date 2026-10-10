# -*- coding: utf-8 -*-
"""
cacx —— 网信办公开语料库工具
================================
中国网信办 (www.cac.gov.cn) 与浙江网信网 (www.zjwx.gov.cn) 公开文章的
采集、检索、分析一体化工具。

模块速览：
    cacx.config     配置持久化（密钥掩码）
    cacx.db         SQLite 存储 + FTS5 中文全文检索
    cacx.importer   从既有 json 语料批量入库
    cacx.collector  两站采集器（列表 + 正文）
    cacx.ai         AI 适配层（云端 / 本地可切换）
    cacx.web        FastAPI 检索服务
    cacx.cli        后台命令符入口
"""

__version__ = "0.1.0"
__all__ = ["config", "db", "importer", "collector", "ai", "web", "cli"]