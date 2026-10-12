# -*- coding: utf-8 -*-
"""
实体关系抽取引擎（规则优先，可复现）

为什么主干用规则而不是 AI：
    老师会问「这个数怎么来的」。规则引擎能当场重跑出同一个结果，
    模型不能（同一份数据两次跑可能不同）。所以规则是主干，
    AI/NER 只在旁路（可选 hanlp 增强、抽样质检）参与。

设计的三条硬约束（缺一条就不许写入）：
    ① 每条实体/关系必须带 article_id —— 老师要求「点边看原文」
    ② 每条关系必须带 evidence，且 evidence 必须是**原文的连续片段**：
       由规则的命中偏移切出所在句子，而不是让模型「总结一句」，
       这样才能程序化断言 evidence 一定 in 原文
    ③ 正文 < MIN_CHARS 字的直接跳过并计数 —— 这类文章抽出来全是噪声

置信度分级（对应「已拍板：纯规则 + 规则内建置信度分级」）：
    政策/机构 ≥0.9  → 高置信，直接进图
    治理问题 0.85   → 中置信，进图
    措施    0.6~0.8 → 低置信，写库但标 needs_review，**不进默认图谱**
"""
import re
from typing import Any, Dict, Iterable, List, Optional, Tuple

from . import textseg

MIN_CHARS = 200          # 正文太短抽不出东西
MAX_BODY = 20000         # 超长正文只取前 N 字，控制单篇耗时
NEEDS_REVIEW_BELOW = 0.8  # 低于此值的实体写库但不进图

# ── 分句 ───────────────────────────────────────────────────────────────
# 必须含分号：公文的并列机关常在分号两侧（「A部；B局联合印发」），
# 只按句号切会把两个机关并进同一句，反而丢掉关键信息。
_SENT_END = "。！？；\n"


def split_sentences(text: str) -> List[Tuple[int, str]]:
    """切成 [(起点偏移, 句子)]，偏移用于回查与前端高亮。"""
    out: List[Tuple[int, str]] = []
    start = 0
    for i, ch in enumerate(text):
        if ch in _SENT_END:
            s = text[start:i + 1]
            if s.strip():
                out.append((start, s))
            start = i + 1
    tail = text[start:]
    if tail.strip():
        out.append((start, tail))
    return out


def sentence_at(text: str, pos: int) -> str:
    """取包含 pos 的那一句 —— 规则命中位置 → 原文证据。"""
    for start, s in split_sentences(text):
        if start <= pos <= start + len(s):
            return s.strip()
    return ""


# ── 政策 ───────────────────────────────────────────────────────────────
# 《》是最强信号：公文里的法规/规划几乎都带书名号
_BOOK_RE = re.compile(r"《([^《》]{3,60})》")
_POLICY_TAIL = ("法", "条例", "办法", "规定", "意见", "通知", "规划",
                "方案", "决定", "批复", "令", "准则", "章程")
# 文件的通用前缀/后缀，不能算进政策名
_POLICY_JUNK = ("关于", "有关", "的通知", "的意见", "的批复", "的决定")
_POLICY_CORE = ("互联网", "网络", "数据", "个人信息", "人工智能",
                "信息安全", "未成年人", "电信", "网络安全", "区块链")


def is_policy_name(name: str) -> bool:
    t = name.strip()
    if not (4 <= len(t) <= 50):
        return False
    if any(j in t for j in _POLICY_JUNK):
        return False
    # ⚠ 政策名里不该出现**动词/连接词**。没有这条的话，「各省级网信办要按照通知」
    # 「未成年人沉迷等突出问题依法」都会被当成政策名抽进去 —— 它们
    # 只是恰好以「通知/问题」结尾的普通句子。
    if _VERB_HINT.search(t):
        return False
    if t.endswith(_POLICY_TAIL):
        return True
    return any(c in t for c in _POLICY_CORE) and 6 <= len(t) <= 40


# 句子里出现这些字，说明它是「一句有动作的话」而不是「一个文件的名称」
_VERB_HINT = re.compile(
    r"(要|应当|应|须|将|开展|进行|实施|加强|推动|按照|根据|指出|强调|"
    r"要求|发布|印发|出台|切实|全面|深入|进一步|继续|依法|确保|"
    r"等|者|中|内|时|后|前|了|的|和|与|及|及部)")


# 无书名号的政策：「XX法」「XX条例」这类靠后缀直接匹配。
# ⚠ 后缀里**不能有 通知/意见/决定/批复** —— 这四个太容易撞上普通句子
#（「…要按照通知」「…的突出问题」），误抽率极高。只保留强特征后缀。
_POLICY_TAIL_STRONG = ("条例", "办法", "规定", "准则", "章程", "规划", "方案")
_POLICY_SCAN_RE = re.compile(
    r"[一-鿿]{2,16}(?:" + "|".join(_POLICY_TAIL_STRONG) + r")")


# ── 措施 ───────────────────────────────────────────────────────────────
# 措施是「动作」，靠动词模式识别。刻意保守 —— 低置信就标 needs_review。
_MEASURE_PATTERNS = [
    (re.compile(r"(?:开展|启动|实施|推进|组织)[^，。；、！？]{0,16}?"
                r"(?:专项行动|专项治理|专项检查|专项整治|集中整治)"), 0.8),
    (re.compile(r"依法(?:查处|处罚|约谈|责令整改|责令改正)"), 0.75),
    (re.compile(r"(?:约谈|通报批评|责令整改)"), 0.7),
    (re.compile(r"(?:下架|下线|封禁|关闭)[^，。；、！？]{0,10}?"
                r"(?:APP|应用|账号|平台|网站|小程序)?"), 0.7),
    (re.compile(r"(?:发布|公布)[^，。；、！？]{0,8}(?:通报|名单|指南|规范)"), 0.6),
]
_measure_cache: Dict[str, List[Tuple[str, int, float]]] = {}


def find_measures(text: str) -> List[Tuple[str, int, float]]:
    """→ [(措施名, 位置, 置信)]"""
    hit = _measure_cache.get(text)
    if hit is None:
        hit = []
        for rx, conf in _MEASURE_PATTERNS:
            for m in rx.finditer(text):
                name = m.group(0).strip()
                if 2 <= len(name) <= 30:
                    hit.append((name, m.start(), conf))
        _measure_cache[text] = hit
        # 逐篇调用，缓存不能无限增长，否则长文本会把内存吃掉
        if len(_measure_cache) > 512:
            _measure_cache.clear()
    return hit


# ── 关系触发词 ─────────────────────────────────────────────────────────
_REL_TRIGGER = {
    "issue": (re.compile(r"(印发|发布|出台|公布|施行)"), 3.0),
    "target": (re.compile(r"(针对|整治|打击|治理|遏制|查处|清理)"), 3.0),
    "adopt": (re.compile(r"(开展|启动|实施|推进)"), 2.0),
}
_JOINT_TITLE = re.compile(r"(联合印发|联合发布|联合开展|会同|联发|共同印发)")
_JOINT_BODY = re.compile(r"(联合|会同|联发|共同印发|联合开展|联合部署)")


def _org_mentions(text: str) -> List[Tuple[str, int]]:
    """
    找出机构名 + 位置。

    ⚠ 两条坑都在这里踩过：
      ① 并列切分：「公安部、市场监管总局」必须切成两个 —— 不切的话
         机构名互相污染，一篇文章能把两个部门绑成同一个节点。
      ② **jieba 会把标点单独切成 token**，拼 n-gram 时若不剥掉，
         会拼出「。各省级网信办」这种带句号的假机构名。
         textseg.org_like 只检查首尾是否命中 _BAD_HEAD/_BAD_TAIL，
         句号不在里面，所以拦不住 —— 必须在拼之前就跳过标点 token。
    """
    j = textseg._get()
    if j is None:
        return []
    toks = [t for t in j.cut(text) if t.strip()]
    n = len(toks)
    hits: List[Tuple[str, int]] = []
    seen = set()
    for i in range(n):
        # 标点 token 是断点：拼之前就跳过，别指望后面清洗
        if _SPLIT_RE.fullmatch(toks[i]) or _is_punct(toks[i]):
            continue
        acc = ""
        for jx in range(i, min(i + 4, n)):
            if _SPLIT_RE.fullmatch(toks[jx]) or _is_punct(toks[jx]):
                break
            acc += toks[jx]
            if len(acc) > 18:
                break
            if not (3 <= len(acc) <= 18):
                continue
            if textseg.org_like(acc):
                name = acc.strip()
                # ② 抽取层的**第二道**机构过滤。
                #    textseg.org_like 是给「挖掘候选词」用的粗判，
                #    挡得住「含动词的词组」但挡不住这些：
                #      习近平向大会 / 的欢迎宴会 / 周年高级别对话会 /
                #      四中全会 / 乌镇峰会 / 第三届会员代表大会
                #    实测 500 篇：它们占了机构的 60%+，
                #    并把联合发文边污染成「服务联合会×联合会」这种垃圾。
                if not _plausible_org(name):
                    break
                if name not in seen:
                    seen.add(name)
                    hits.append((name, i))
                # 命中即停：否则「中央网信办」后面还会拼出
                # 「中央网信办公安部」这种超长垃圾
                break
    return hits


# 以这些词结尾的，一律不是机构（会议/峰会/活动/代表大会…）
_NOT_ORG_TAIL = ("大会", "峰会", "论坛", "全会", "会议", "代表大会",
                 "研讨会", "座谈会", "发布会", "招待会", "宴会",
                 "开幕式", "闭幕式", "表彰", "仪式", "活动", "日",
                 "讲话", "贺电", "贺信", "致辞", "批示")
# 以这些词开头的，不是机构（人名 + 动作、指示性词）
_NOT_ORG_HEAD = ("习近平", "李强", "赵乐际", "王沪宁", "蔡奇", "丁薛祥",
                 "李希", "韩正", "第", "各", "该", "本", "全", "共",
                 "向", "的", "在", "与", "和", "及", "届", "年", "月")
# 纯数字/字母/单字，不是机构
_NOT_ORG_ONLY = re.compile(r"^[\dA-Za-z]+$")

# ⚠ **通用词做机构后缀时区分度不够**。「经济社会」「研究院」「发展格局」
#   「国际社会」都以机构后缀结尾，但它们是抽象名词/通称，
#   当成节点会把图谱搅成一团（实测它们挤进了机构 Top 15）。
#   判据：这些词**本身带行政/地域前缀才是机构**（如「中国社科院研究院」
#   可以，「研究院」不行）。所以要求至少有一个**具体限定成分**。
_GENERIC_ONLY = {
    "经济社会", "研究院", "发展格局", "国际社会", "委员会", "联合会",
    "理事会", "协会", "学会", "基金会", "集团", "公司", "中心", "协会",
    "社会", "世界", "全国", "各地", "各方", "全社会", "国际", "国内",
    "有关方面", "相关部门", "地方政府", "社会各界", "各国", "多方",
    "有关国家", "全球", "亚太", "地区", "领域", "行业", "部门",
}

# ⚠ 动词短语会蹭上机构后缀：「构建新发展格局」以「局」结尾、
#   「以…为中心」以「心」结尾、「XX为中心」…
#   判据：机构名里不该有动词/介词。看到就否掉。
_ORG_VERB = re.compile(r"(构建|建立|推动|加强|促进|围绕|坚持|加快|深化|"
                       r"完善|优化|落实|贯彻|实现|形成|打造|探索|推进|"
                       r"为中心|为重点|为目标|为中心工作)")


def _plausible_org(name: str) -> bool:
    """机构名的合理性硬判据（抽取层专用，比 org_like 严得多）"""
    if _NOT_ORG_ONLY.match(name):
        return False
    if name.endswith(_NOT_ORG_TAIL):
        return False
    if name.startswith(_NOT_ORG_HEAD):
        return False
    if name[0].isdigit():
        return False
    if _ORG_VERB.search(name):
        return False
    # 通用词裸奔：只有「XX研究院」这种带前缀的才算机构
    if name in _GENERIC_ONLY:
        return False
    return True


_SPLIT_RE = re.compile(r"[、，,；;]|与|和|及|等")
# 纯标点 token（jieba 会把句号、括号等单独切出来）
_PUNCT_ONLY_RE = re.compile(r"^[\s，。；、！？,.;!?：:（）()《》“”\"'‘’"
                            r"【】\[\]—…%·\d]+$")


def _is_punct(tok: str) -> bool:
    return bool(_PUNCT_ONLY_RE.match(tok))


_problem_terms_cache = None


def _problem_terms():
    """
    延迟导入 db（db 不依赖 extract，延迟可避免循环 import）。
    治理问题词表是 db.Database.PROBLEM_TERMS，两边共用一份，不重复维护。
    """
    global _problem_terms_cache
    if _problem_terms_cache is None:
        from .db import Database
        _problem_terms_cache = Database.PROBLEM_TERMS
    return _problem_terms_cache


# ── 单篇抽取 ───────────────────────────────────────────────────────────
def extract_article(art: Dict[str, Any],
                    with_measures: bool = True) -> Dict[str, Any]:
    """
    抽一篇。art 需含 id / title / content_text / url。
    → {entities, relations, skipped, reason}
    """
    aid = art["id"]
    title = (art.get("title") or "").strip()
    body = (art.get("content_text") or "")[:MAX_BODY]

    if len(body) < MIN_CHARS:
        return {"entities": [], "relations": [], "skipped": True,
                # ⚠ 原因要**聚合**，不能带具体字数 ——
                #   否则 500 篇能产出几十个不同 reason，报告里没法看。
                "reason": f"正文不足 {MIN_CHARS} 字"}

    # 标题权重更高（标题点题），所以拼在正文前面
    full = (title + "\n" + body) if title else body

    ents: List[Dict[str, Any]] = []
    rels: List[Dict[str, Any]] = []

    def add_ent(name: str, etype: str, pos: int, conf: float):
        name = name.strip()
        if not name or len(name) > 40:
            return
        e = {"name": name, "type": etype, "article_id": aid,
             "evidence": sentence_at(full, pos), "extractor": "rule",
             "confidence": conf}
        # ⚠ 用 `<=` 而不是 `<`：专项检查类的措施置信度正好是 0.8，
        # 若用严格小于，它会**恰好卡在阈值上而不被标记**，
        # 于是低置信措施混进默认图谱 —— 这是实测才发现的边界 bug。
        if conf <= NEEDS_REVIEW_BELOW:
            e["needs_review"] = True
        ents.append(e)

    def add_rel(src: str, dst: str, rtype: str, pos: int, w: float):
        rels.append({"src_name": src, "dst_name": dst, "rel_type": rtype,
                     "article_id": aid, "evidence": sentence_at(full, pos),
                     "extractor": "rule", "weight": w})

    # 1) 政策：书名号优先，其次后缀扫描
    for m in _BOOK_RE.finditer(full):
        if is_policy_name(m.group(1)):
            add_ent(m.group(1), "policy", m.start(), 0.95)
    for m in _POLICY_SCAN_RE.finditer(full):
        add_ent(m.group(0), "policy", m.start(), 0.85)

    # 2) 机构
    orgs = _org_mentions(full)
    for name, tok_i in orgs:
        add_ent(name, "org", _tok_pos(full, tok_i), 0.9)

    # 3) 治理问题
    for pname, kws in _problem_terms():
        for kw in kws:
            p = full.find(kw)
            if p >= 0:
                add_ent(pname, "problem", p, 0.85)
                break

    # 4) 措施（低置信 → 标 needs_review，不进默认图谱）
    if with_measures:
        for name, pos, conf in find_measures(full):
            add_ent(name, "measure", pos, conf)

    # 5) 建边
    _build_relations(full, title, orgs, ents, add_rel)

    return {"entities": ents, "relations": rels, "skipped": False,
            "reason": ""}


def _joint_pairs(full: str, title: str,
                 org_names: List[str]) -> List[Tuple[str, str, int]]:
    """
    找真正的联合发文主体对 → [(A, B, 位置)]

    判据：**同一个含联合触发词的句子**里同时出现的机构。
    标题里的机构直接全体两两配对（标题的并列机关就是联合发文机关）；
    正文只在触发句内配对，避免把后文顺带提到的机关也拉进来。
    """
    pairs: List[Tuple[str, str, int]] = []
    seen = set()

    def _emit(names: List[str], base: int):
        top = names[:6]
        for x, a in enumerate(top):
            for b in top[x + 1:]:
                if a == b:
                    continue
                key = (a, b) if a < b else (b, a)
                if key in seen:
                    continue
                seen.add(key)
                pairs.append((key[0], key[1], base))

    # 标题：并列机关 = 联合发文机关
    t_orgs = [n for n in org_names if n in title]
    if _JOINT_TITLE.search(title) and len(t_orgs) >= 2:
        _emit(t_orgs, 0)
        return pairs

    # 正文：逐句找「含触发词 且 含≥2 机构」的句子
    if _JOINT_BODY.search(full):
        j = textseg._get()
        for start, sent in split_sentences(full):
            if not _JOINT_BODY.search(sent):
                continue
            in_sent = [n for n in org_names if n in sent]
            if len(in_sent) >= 2:
                _emit(in_sent, start)
    return pairs


def _tok_pos(full: str, tok_index: int) -> int:
    """
    token 下标 → 原文偏移。
    靠 jieba 重新切一次对齐有偏移风险，所以这里退化为「找该 token
    在原文里第一次出现的位置」—— 只需要句子级精度，够用且不会越界。
    """
    j = textseg._get()
    if j is None:
        return 0
    toks = [t for t in j.cut(full) if t.strip()]
    if 0 <= tok_index < len(toks):
        p = full.find(toks[tok_index])
        if p >= 0:
            return p
    return 0


def _build_relations(full: str, title: str, orgs, ents, add_rel):
    """
    建边。四类关系各用各的触发词。
    ⚠ 扇出上限：公文的落款常有十几个机关，全连成一团图就没法看了，
      所以机构只取前若干个建边（证据仍完整保留在 entities 里）。
    """
    org_names = [n for n, _ in orgs]
    pol = [e["name"] for e in ents if e["type"] == "policy"]
    prob = [e["name"] for e in ents if e["type"] == "problem"]
    meas = [e["name"] for e in ents if e["type"] == "measure"]

    # ── 联合发文：老师点名要的关系，权重最高 ──
    # ⚠ **只连真正共现的机构对**，不能把全文所有机构无差别互连。
    #   实测过一篇文章里 8 个机构会连出 56 条边，图直接糊掉 ——
    #   而且多数是假的（正文后面才提到的机关根本没参与联合发文）。
    #   正确做法：只在**含联合触发词的那一句**里找机构，那才是联合发文主体。
    joint_pairs = _joint_pairs(full, title, org_names)
    for a, b, pos in joint_pairs:
        w = 3.0 if _JOINT_TITLE.search(title) else 2.0
        add_rel(a, b, "co_issue", pos, w)

    def _link(rx, objs, rtype, w, obj_n=5, org_n=3):
        if not objs or not org_names:
            return
        m = rx.search(full)
        if not m:
            return
        for o in org_names[:org_n]:
            for obj in objs[:obj_n]:
                add_rel(o, obj, rtype, m.start(), w)

    for rt in ("issue", "target", "adopt"):
        rx, w = _REL_TRIGGER[rt]
        objs = {"issue": pol, "target": prob, "adopt": meas}[rt]
        _link(rx, objs, rt, w)


# ── 批量 ───────────────────────────────────────────────────────────────
def extract_batch(articles: List[Dict[str, Any]],
                  with_measures: bool = True,
                  progress=None) -> Dict[str, Any]:
    """
    逐篇抽取。articles 需含 id / title / content_text / url。
    → {entities, relations, articles, skipped, skipped_reasons, low_confidence}
    """
    ents: List[Dict[str, Any]] = []
    rels: List[Dict[str, Any]] = []
    skipped = 0
    low = 0
    reasons: Dict[str, int] = {}
    for i, a in enumerate(articles):
        r = extract_article(a, with_measures=with_measures)
        if r["skipped"]:
            skipped += 1
            reasons[r["reason"]] = reasons.get(r["reason"], 0) + 1
        else:
            low += sum(1 for e in r["entities"] if e.get("needs_review"))
        ents.extend(r["entities"])
        rels.extend(r["relations"])
        if progress and (i + 1) % 25 == 0:
            progress(i + 1, len(articles), len(ents), len(rels))
    return {"entities": ents, "relations": rels, "articles": len(articles),
            "skipped": skipped, "skipped_reasons": reasons,
            "low_confidence": low}


def verify_evidence(entities: List[Dict[str, Any]],
                    relations: List[Dict[str, Any]],
                    content_of) -> Dict[str, Any]:
    """
    **硬校验**：每条 evidence 必须能在原文里原样找到。

    这是整个引擎的可信度锚点 —— 规则命中偏移切出的句子，
    必然是原文的连续子串。若这条断言不过，后面所有统计都不可信。

    ⚠ `content_of(article_id)` 必须返回**和 extract_article 拼法完全一致的文本**
       （即 `title + "\n" + body`）。若这里只返回正文、而抽取时拼了标题，
       就会把「标题里命中但正文里没有」的句子误判成证据失效 ——
       实测 500 篇误报 1196 条就是这么来的。
    content_of(article_id) -> str
    """
    cache: Dict[int, str] = {}
    bad_e: List[Dict[str, Any]] = []
    bad_r: List[Dict[str, Any]] = []

    def _content(aid):
        if aid not in cache:
            cache[aid] = content_of(aid) or ""
        return cache[aid]

    for e in entities:
        ev = (e.get("evidence") or "").strip()
        if not ev:
            bad_e.append({"name": e.get("name"), "why": "证据为空",
                          "article_id": e.get("article_id")})
            continue
        c = _content(e["article_id"])
        if ev not in c:
            bad_e.append({"name": e.get("name"), "why": "原文里找不到",
                          "article_id": e.get("article_id"),
                          "evidence": ev[:40]})
    for r in relations:
        ev = (r.get("evidence") or "").strip()
        if not ev:
            bad_r.append({"src": r.get("src_name"), "why": "证据为空",
                          "article_id": r.get("article_id")})
            continue
        c = _content(r["article_id"])
        if ev not in c:
            bad_r.append({"src": r.get("src_name"), "why": "原文里找不到",
                          "article_id": r.get("article_id"),
                          "evidence": ev[:40]})
    return {"entities_total": len(entities), "entities_bad": len(bad_e),
            "relations_total": len(relations), "relations_bad": len(bad_r),
            "ok": not bad_e and not bad_r,
            "bad_entity_samples": bad_e[:5],
            "bad_relation_samples": bad_r[:5]}