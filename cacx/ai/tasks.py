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


def content_limit(cfg: Dict) -> int:
    """
    送进模型的正文截断字数；**0 = 不截断**。

    为什么改成可配 + 默认不截断：
      原先 summarize 硬编码 2500 字。政务长文（讲话全文、政策解读）
      常超这个数，摘要只看得到前半段 —— 结论容易偏，甚至把
      「文末才出现的关键结论」直接漏掉，而用户完全无从察觉。
      截断的真实用途只是控制 token 成本，所以把选择权交回用户：
      留空/0 不截断（默认），有成本诉求的再自己设一个上限。

    ⚠ 兼容老配置：显式设了 max_content_chars 就用它；没设则取
      DEFAULT_CONTENT_LIMIT（0=不截断）。
    """
    v = (cfg.get("ai") or {}).get("max_content_chars", 0)
    try:
        n = int(v or 0)
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else 0


def _article_block(art: Dict[str, Any], limit: int = 0) -> str:
    """limit<=0 = 不截断。"""
    body = art.get("content_text") or ""
    if limit and limit > 0:
        body = body[:limit]
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
    raw = p.chat(SYS_SUMMARIZE, _article_block(art, content_limit(cfg)),
                 dry_run=dry_run)
    if dry_run:
        return json.loads(raw)
    return parse_json(raw, {"summary": "", "tags": [], "sentiment": "neutral",
                            "entities": []})


def tag_many(cfg: Dict, arts: List[Dict[str, Any]], dry_run: bool = True) -> Any:
    """批量打标签，一次调用处理多篇（省配额）"""
    p = build(cfg, task="tag")
    if p is None:
        raise AIError("AI 未配置")
    # ⚠ 这里的 700 与 summarize 的 max_content_chars 是**两回事**，不要合并：
    # 本函数一次调用要装 20 篇，若每篇都不截断，请求体会大到被模型
    # 直接拒收（或悄悄截掉后半批）。批量标签本来就只需要每篇的梗概。
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
    "\"fix_steps\": [\"...\"], \"need_human\": true/false, "
    "\"risk\": \"low|medium|high\", \"summary\": \"给使用者看的一句话结论\"}。只输出 JSON。"
)

SYS_ADVISORY = (
    "你是舆情数据研究助理。下面是一个政务网站语料库的采集与健康状况快照。"
    "请评估数据质量、指出潜在风险、给出后续采集与分析建议。"
    "输出严格 JSON：{\"summary\": \"一段话结论\", \"quality_score\": 0到100的整数, "
    "\"data_quality\": [\"...\"], \"risks\": [\"...\"], \"suggestions\": [\"...\"], "
    "\"need_human\": true/false}。只输出 JSON。"
)


def _clip(x, n=200):
    s = str(x if x is not None else "")
    return s[:n] + ("…" if len(s) > n else "")


def diagnose(cfg: Dict, problems: List[Dict], db=None) -> Optional[Dict[str, Any]]:
    """
    doctor 的 AI 补充层。AI 没配置或调用失败时返回 None，
    **不影响规则层结论** —— 规则层永远先跑。

    注意这里必须 **dry_run=False**：doctor 的 AI 结论是要真落进
    日志给人看的，预览没意义。但也因为会真发请求，调用方要自己判断。
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
        raw = p.chat(SYS_DIAGNOSE, detail + extra, dry_run=False)
    except AIError as e:
        return {"_error": _clip(str(e), 160)}
    parsed = parse_json(raw, {})
    if not isinstance(parsed, dict):
        return {"_error": "模型输出不是 JSON"}
    steps = parsed.get("fix_steps") or []
    evidence = parsed.get("evidence") or []
    if not isinstance(steps, list):
        steps = []
    if not isinstance(evidence, list):
        evidence = []
    return {
        "可能原因": _clip(parsed.get("likely_cause")) or "（模型未给出）",
        "判断依据": [_clip(e, 140) for e in evidence[:4]],
        "修复建议": [_clip(s, 160) for s in steps[:5]],
        "风险等级": {"low": "低", "medium": "中", "high": "高"}.get(
            str(parsed.get("risk", "")).lower(), "未知"),
        "需人工确认": "是" if parsed.get("need_human") else "否",
        "_model": getattr(p, "model", "") or "",
    }


def advisory(cfg: Dict, db, reports: Optional[List[Dict]] = None,
             recent_runs: Optional[List[Dict]] = None) -> Optional[Dict[str, Any]]:
    """
    数据健康顾问 —— 面向「一切正常」的场景。

    原先 diagnose 只在出问题时才跑，全绿时 AI 永远不出声，
    于是「AI 辅助」实际上等于没有。现在补上这条正向链路：
    即使接口全好，也会评估数据质量、给后续采集与分析建议。

    ⚠ 喂给模型的字段必须**重命名并加口径说明**，不能直接把采集器的
    原始 dict 丢过去。两站原始字段名都不一样（CAC 叫 total_rec、
    浙江叫 count），语义都是「源站声称的总量」。实测把原始字段喂进去后，
    模型把 total_rec 理解成了「自检记录条数」，进而得出
    「自检 2040 条 vs 库存 2956 篇，口径不一致」的结论 ——
    那不是数据问题，是**我把字段名没说清楚**。
    """
    p = build(cfg, task="doctor")
    if p is None:
        return None
    f = db.facets()

    # 站点自检：把内部字段翻译成有口径说明的名字
    site_reports = []
    for r in (reports or []):
        claimed = r.get("total_rec") if "total_rec" in r else r.get("count")
        site_reports.append({
            "站点": r.get("site_name") or r.get("site"),
            "接口是否可用": bool(r.get("ok")),
            "栏目数": r.get("checked_columns"),
            "源站声称的该栏目总条数": claimed,
            "本工具已入库该站点总数": next(
                (s["n"] for s in f.get("sites", []) if s["site"] == r.get("site")),
                None),
            "发现的问题": r.get("issues") or [],
        })

    snap = {
        "_口径说明": {
            "源站声称的该栏目总条数": "站点列表接口自己报的 total，与本工具实际入库数无关；"
                                 "两者不等是正常的（收录范围不同、分页口径不同）",
            "本工具已入库该站点总数": "本工具 config 里设定的采集时间窗内，实际抓到的篇数",
            "最近同步批次的 listed": "本次同步列表接口扫到的条数（含库里已有的）",
            "最近同步批次的 failed": "抓不到正文因而未入库的条数，多为站外链接（微信公号等）",
        },
        "数据总量": {
            "文章数": f["total"], "总字数": f["chars"],
            "覆盖天数": f["days"],
            "空正文_源站即空": f["empty"], "站外链接条目": f["external"],
        },
        "分站点": [{k: v for k, v in s.items() if k != "chars"}
                   for s in f.get("sites", [])],
        "分年份": {y["year"]: y["n"] for y in f.get("years", [])},
        "站点接口自检": site_reports,
        "最近同步批次": (recent_runs or [])[:5],
    }
    try:
        raw = p.chat(SYS_ADVISORY,
                     json.dumps(snap, ensure_ascii=False, indent=2), dry_run=False)
    except AIError as e:
        return {"_error": _clip(str(e), 160)}
    parsed = parse_json(raw, {})
    if not isinstance(parsed, dict):
        return {"_error": "模型输出不是 JSON"}

    def lst(key, n=6):
        v = parsed.get(key) or []
        return [_clip(x, 150) for x in v[:n]] if isinstance(v, list) else []

    score = parsed.get("quality_score", 0)
    try:
        score = max(0, min(100, int(score)))
    except Exception:
        score = 0
    return {
        "结论": _clip(parsed.get("summary"), 300) or "（模型未给出）",
        "数据质量评分": score,
        "数据质量观察": lst("data_quality"),
        "潜在风险": lst("risks"),
        "后续建议": lst("suggestions"),
        "需人工确认": "是" if parsed.get("need_human") else "否",
        "_model": getattr(p, "model", "") or "",
    }


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