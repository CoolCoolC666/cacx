# -*- coding: utf-8 -*-
"""
AI 具体任务
================================================================
    summarize  文章摘要 + 主题标签
    tag        只出标签
    diagnose   doctor 的补充诊断：把规则层发现的问题交给模型，给修复建议
    suggest    给站点/栏目提建议（可选）

所有任务默认 dry_run=True，只预览不真发请求；要真跑显式 dry_run=False。
"""

import json
import re
from typing import Any, Dict, List, Optional

from .base import AIError, build

SYS_SUMMARIZE = (
    "你是舆情研究助理。阅读给定的政府网站文章，输出严格 JSON："
    '{"summary": "不超过120字的中文摘要", "tags": ["3到6个主题标签"], '
    '"sentiment": "neutral|positive|negative", "entities": ["涉及机构或人物"]}。'
    "只输出 JSON，不要任何解释文字。"
)


def _article_block(art: Dict[str, Any], limit: int = 2500) -> str:
    body = (art.get("content_text") or "")[:limit]
    return (f"标题：{art.get('title','')}\n"
            f"栏目：{art.get('column_name','')}\n"
            f"日期：{art.get('pub_date','')}\n"
            f"来源：{art.get('source_text','')}\n"
            f"正文：\n{body}\n")


def summarize(cfg: Dict, art: Dict[str, Any], dry_run: bool = True) -> Any:
    """单篇摘要+标签。返回 dict；dry_run 时返回预览 dict。"""
    p = build(cfg, task="summarize")
    if p is None:
        raise AIError("AI 未配置（cacx config set ai.provider openai_compat）")
    raw = p.chat(SYS_SUMMARIZE, _article_block(art), dry_run=dry_run)
    if dry_run:
        return json.loads(raw)
    return parse_json(raw, {"summary": "", "tags": [], "sentiment": "neutral",
                            "entities": []})


def tag_many(cfg: Dict, arts: List[Dict[str, Any]], dry_run: bool = True) -> Any:
    """批量打标签，一次调用处理多篇（省配额）"""
    p = build(cfg, task="tag")
    if p is None:
        raise AIError("AI 未配置")
    blocks = "\n\n---\n\n".join(
        _article_block(a, 700) for a in arts[:20])
    sys_p = ("为下列每篇文章提取 3-5 个中文主题标签，输出 JSON 数组，"
             "每个元素形如 {\"id\": 1, \"tags\": [\"...\"]}。只输出 JSON。")
    raw = p.chat(sys_p, blocks, dry_run=dry_run)
    if dry_run:
        return json.loads(raw)
    return parse_json(raw, [])


SYS_DIAGNOSE = (
    "你是爬虫维护工程师。下面是一个政务网站采集器的自检结果，"
    "请判断最可能的原因并给出具体修复建议。"
    "输出严格 JSON：{\"likely_cause\": \"...\", \"evidence\": [\"...\"], "
    "\"fix_steps\": [\"...\"], \"need_human\": true/false}。只输出 JSON。"
)


def diagnose(cfg: Dict, problems: List[Dict], db=None) -> Optional[Dict[str, str]]:
    """
    doctor 的 AI 补充层。AI 没配置或调用失败时返回 None，
    **不影响规则层结论** —— 规则层永远先跑。
    """
    p = build(cfg, task="doctor")
    if p is None:
        return None
    detail = json.dumps(problems, ensure_ascii=False, indent=2)
    extra = ""
    if db is not None:
        f = db.facets()
        extra = (f"\n\n当前库内统计：文章 {f['total']} 篇，空正文 {f['empty']} 篇，"
                 f"站外条目 {f['external']} 条。")
    try:
        raw = p.chat(SYS_DIAGNOSE, detail + extra)
    except AIError:
        return None
    parsed = parse_json(raw, {})
    if not isinstance(parsed, dict):
        return None
    out = {}
    cause = str(parsed.get("likely_cause", ""))[:200]
    steps = parsed.get("fix_steps") or []
    if isinstance(steps, list):
        steps = [str(s)[:160] for s in steps[:4]]
    else:
        steps = []
    out["可能原因"] = cause or "（模型未给出）"
    out["修复建议"] = " → ".join(steps) if steps else "（模型未给出）"
    out["需人工确认"] = "是" if parsed.get("need_human") else "否"
    return out


# ---------------------------------------------------------------- 工具
def parse_json(text: str, fallback: Any = None) -> Any:
    """从模型输出里抠出 JSON。模型常包 markdown 代码块或加前后缀。"""
    if text is None:
        return fallback
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*", "", t)
    t = re.sub(r"\s*```$", "", t)
    try:
        return json.loads(t)
    except Exception:
        pass
    m = re.search(r"\{[\s\S]*\}", t) or re.search(r"\[[\s\S]*\]", t)
    if m:
        try:
            return json.loads(m.group(0))
        except Exception:
            pass
    return fallback