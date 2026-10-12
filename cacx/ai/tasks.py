# -*- coding: utf-8 -*-
"""
AI 具体任务
================================================================
    summarize  文章摘要 + 主题标签
    tag        只出标签
    diagnose   doctor 的补充诊断：把规则层发现的问题交给模型，给修复建议
    suggest    给站点/栏目提建议（可选）
    review     分词实验室：AI 预勾选新词候选（只建议，不写词典）

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


#: 主题分类的**候选词表**。
#: ⚠ 必须是受控词表，不能让模型自由发挥主题名 ——
#:   4513 篇自由打标会得到几百个不同标签名，压根没法按月聚合画图，
#:   还会顺手引入提示注入（模型可能被正文里的句子带跑，
#:   输出「忽略前面的指令」之类的东西当标签）。
#:   这与「模型返回的 word 必须过候选白名单」是同一条纪律。
THEME_LABELS = (
    "网络谣言", "网络诈骗", "未成年人", "个人信息", "数据安全",
    "网络暴力", "深度伪造", "生成式AI", "算法推荐",
    "App违规", "账号链条", "数据出境", "跨境数据",
    "网络执法", "清朗行动", "个人信息保护", "反诈宣传",
    "党建学习", "基层治理", "数字化转型", "other",
)

_SYS_THEME = (
    "你是舆情文本分类助手。下面是若干篇政府网站文章的梗概，"
    "每篇以 `id: N` 开头，以 --- 分隔。\n"
    "请为**每一篇**从下列候选主题中选出最多 2 个最贴切的：\n"
    "  {labels}\n"
    "规则：\n"
    "1. 只能从候选里选，**不要自造主题名**；都不贴切就选 other。\n"
    "2. 一篇最多 2 个，宁缺毋滥；确实没有就给 []。\n"
    "3. 只依据文章内容判断，不要输出解释。\n"
    "输出严格 JSON 数组，每个元素形如 "
    '{{"id": 1, "themes": ["个人信息"]}}，必须覆盖输入里的每个 id。'
    "只输出 JSON，不要任何前后缀。"
)


def theme_many(cfg: Dict, arts: List[Dict[str, Any]],
               dry_run: bool = True, per_call: int = 12) -> Any:
    """
    批量主题分类 —— 一次调用给多篇打主题标签。

    ⚠ MiroFish 那套「批量 LLM 调用」的思路（多个请求打包成一次
      API 调用，成本降 ~50%、速度升 5–10 倍）已用于此处：
      **一次装 12 篇**，而不是 4513 篇逐个调。

    ⚠ per_call 不能贪大：每篇截 600 字，12 篇约 7200 字 +
      prompt 约 400 字。批次太大会被模型悄悄截断后半段，
      表现为「后面的文章莫名其妙全是 other」——
      这类静默劣化比报错难查得多。
    """
    p = build(cfg, task="theme")
    if p is None:
        raise AIError("AI 未配置")
    use = arts[:max(1, int(per_call))]
    blocks = "\n\n---\n\n".join(
        _article_block(a, 600) for a in use)
    sys_p = _SYS_THEME.format(labels="、".join(THEME_LABELS))
    raw = p.chat(sys_p, blocks, dry_run=dry_run)
    if dry_run:
        return json.loads(raw)
    out = parse_json(raw, [])
    # ⚠ 白名单过滤：模型自造的主题名一律丢弃，绝不写库。
    #   不静默丢弃 —— 丢掉的数量在批处理层可见，否则界面上的
    #   「AI 分类占比」会缺一块却没人知道。
    clean = []
    dropped = 0
    for row in out if isinstance(out, list) else []:
        if not isinstance(row, dict):
            continue
        themes = row.get("themes")
        themes = themes if isinstance(themes, list) else []
        ok = [str(t).strip() for t in themes
              if str(t).strip() in THEME_LABELS]
        dropped += len(themes) - len(ok)
        clean.append({"id": row.get("id"), "themes": ok[:2]})
    return {"rows": clean, "dropped": dropped,
            "asked": len(use), "labels": list(THEME_LABELS)}


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


# ---------------------------------------------------------------- 新词评审
SYS_REVIEW = (
    "你是中文分词词典的评审员。下面是从「网信/网络安全舆情」公文语料里"
    "用统计法（凝固度+邻字熵+可切分性）挖出来的候选词，"
    "请判断每个候选**该不该收进分词词典**。\n"
    "判据（按重要性排序）：\n"
    "1. 它是不是一个**有独立含义的词或固定专名**，"
    "而不是短语（短语该由词拼出来，不该整体入词典）；\n"
    "2. 它对本项目（网信政策、网络治理、安全、数据、个人信息）"
    "有没有实际检索价值；\n"
    "3. 它是不是被截断的残串（如「央和中央军委」这种前半截丢了字）。\n"
    "输出严格 JSON："
    '{"decisions":[{"word":"候选词原文","verdict":"keep|drop|unsure",'
    '"reason":"不超过25字的理由"}]}。\n'
    "verdict 含义：keep=建议收进词典；drop=建议丢弃；unsure=拿不准，交人判断。\n"
    "⚠ word 必须与输入候选词**逐字一致**，不要改写、不要新增、不要输出候选表以外的词。\n"
    "⚠ 只输出 JSON，不要任何解释文字。"
)

# 一次调用装多少个候选。
# ⚠ 这个数是**prompt 体积**与**输出 token**的折中：60 个候选的
#   JSON 输出（每条含 reason）大约 1500~2500 token。调大要多花配额，
#   调小要多次调用、每次都重新解释背景。宁可分批也别超 max_tokens 被截断 ——
#   输出被截断的 JSON 解析不出来，整批白花钱。
REVIEW_BATCH = 40


def review_new_words(cfg: Dict, cands: List[Dict[str, Any]],
                     dry_run: bool = True,
                     batch: int = REVIEW_BATCH,
                     on_batch=None) -> Dict[str, Any]:
    """
    AI 预勾选新词候选。

    ⚠⚠ **它只给建议，一个字都不会写进词典。**
      采纳仍然必须由人在界面上点「✅ 采纳勾选」。原因不是保守，
      是这个任务本身的能力边界：统计只保证「大概率是词」，
      模型只保证「大概率该收」，两者都是概率，
      而写进词典的后果是**持续污染**所有后续检索与统计。
      把「建议」和「生效」分成两步，是让错误可逆的唯一办法。

    返回：{"decisions": {word: {"verdict":…, "reason":…}}, "stats": {…},
           "_model": …}
    """
    p = build(cfg, task="review")
    if p is None:
        raise AIError("AI 未配置（先在设置页填供应商/地址/模型/密钥）")

    # 喂给模型的是**带口径说明**的结构，不是原始 dict：
    # 字段名直接扔过去，模型会把 cohesion 当成「相关度」、
    # 把 count 当成「重要度」，判据就全歪了（advisory 里踩过同一个坑）。
    payload = [{
        "候选词": c.get("word", ""),
        "出现次数": c.get("count", 0),
        "凝固度_越高说明该粘在一起": c.get("cohesion", 0),
        "左邻字自由度": c.get("left_entropy", 0),
        "右邻字自由度": c.get("right_entropy", 0),
        "本项目加权标记": c.get("hint") or "无",
    } for c in cands]

    # 候选词白名单：模型返回的 word 必须落在这个集合里。
    # ⚠ 不是洁癖，是防提示注入：候选文本来自**抓来的网页正文**，
    #   一段精心构造的正文可能诱导模型在 verdict 里塞进任意词，
    #   而这些词下一步就会被写进用户词典。交集之外的一律丢弃。
    allowed = {str(c.get("word", "")).strip() for c in cands}
    allowed.discard("")

    decisions: Dict[str, Dict[str, str]] = {}
    errors: List[str] = []
    chunk = max(1, int(batch))
    for i in range(0, len(payload), chunk):
        part = payload[i:i + chunk]
        if on_batch:
            on_batch(i // chunk + 1, len(part))
        try:
            raw = p.chat(SYS_REVIEW,
                         json.dumps(part, ensure_ascii=False, indent=2),
                         dry_run=dry_run)
            if dry_run:
                return json.loads(raw)
            # ⚠ fallback 用 None 而不是 {}：parse_json 解析不出来时会返回它。
            #   若用 {}，模型回一句「抱歉我无法完成」也会被当成「判了但一个都没给」，
            #   errors 保持空 —— 用户只看到「判了 0 个」，完全不知道发生了什么。
            got = parse_json(raw, None)
        except AIError as e:
            # ⚠ 一批失败**不能吞掉整次调用**：其余批次的结果仍然有用，
            #   全盘丢弃等于用户白花了钱还什么都没拿到。
            errors.append(_clip(str(e), 120))
            continue
        if not isinstance(got, dict):
            errors.append("模型输出不是 JSON")
            continue
        for d in (got.get("decisions") or []):
            if not isinstance(d, dict):
                continue
            w = str(d.get("word", "")).strip()
            if w not in allowed:
                continue          # 白名单外 → 丢弃，不采纳
            v = str(d.get("verdict", "")).strip().lower()
            if v not in ("keep", "drop", "unsure"):
                v = "unsure"      # 模型给了别的值 → 按拿不准处理，不硬猜
            decisions[w] = {"verdict": v, "reason": _clip(d.get("reason"), 30)}

    miss = [w for w in allowed if w not in decisions]
    return {
        "decisions": decisions,
        "stats": {
            "candidates": len(allowed),
            "judged": len(decisions),
            "keep": sum(1 for v in decisions.values() if v["verdict"] == "keep"),
            "drop": sum(1 for v in decisions.values() if v["verdict"] == "drop"),
            "unsure": sum(1 for v in decisions.values() if v["verdict"] == "unsure"),
            "missing": len(miss),
            "errors": errors,
        },
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