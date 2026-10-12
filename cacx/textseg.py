# -*- coding: utf-8 -*-
"""
中文分词（jieba 封装）+ 网信办领域词典。

为什么不能直接 `jieba.cut()`：
    「中央网信办部署开展清朗网络专项行动」
    → 中央 / 网信 / 办 / 部署 / 开展 / 清朗 / 网络 / 专项 / 行动

「中央网信办」被切成三段。而本作业要抽的恰恰是**机构、政策、
治理问题、措施**四类实体 —— 全是这种长串。
默认分词把实体切碎，等于在给下游制造垃圾。

所以必须喂领域词典。三层来源：
  ① 内置常用机构/政策（gov 系统高频，直接写死）
  ② 从本项目语料自动挖掘（`mine_orgs`，高频且带机构后缀的串）
  ③ 调用方临时补充（`add_word`）

jieba 是**可选依赖**：没装时全文检索照常工作，只是退化成不切词。
不能因为一个可选的分词器就让整个程序起不来。
"""
from __future__ import annotations

import re
import warnings
from typing import Iterable, List, Optional, Set

_jieba = None
_load_failed = False
_dict_path = None

# 内置领域词。词频给大，才能压过默认词典里的切分倾向。
# 格式："词 词频 词性"
_BASE_TERMS = """
中央网信办 100000 n
网信办 100000 n
国家互联网信息办公室 100000 n
中央网络安全和信息化委员会办公室 100000 n
中国网信网 100000 n
省委网信办 100000 n
省网信办 100000 n
市网信办 100000 n
省委宣传部 100000 n
中央宣传部 100000 n
公安部 100000 n
国家安全部 100000 n
司法部 100000 n
外交部 100000 n
教育部 100000 n
民政部 100000 n
商务部 100000 n
国家发展改革委 100000 n
国家发改委 100000 n
工业和信息化部 100000 n
国家市场监督管理总局 100000 n
市场监管总局 100000 n
国家广播电视总局 100000 n
国家税务总局 100000 n
国家网信办 100000 n
文化和旅游部 100000 n
国家卫生健康委员会 100000 n
交通运输部 100000 n
住房和城乡建设部 100000 n
生态环境部 100000 n
中国人民银行 100000 n
国家金融监督管理总局 100000 n
全国妇联 100000 n
全国总工会 100000 n
共青团中央 100000 n
全国网信办 100000 n
清朗 100000 n
清朗网络专项行动 100000 n
净网 100000 n
网络谣言 100000 n
网络暴力 100000 n
水军 100000 n
有偿删帖 100000 n
算法推荐 100000 n
深度伪造 100000 n
生成式人工智能 100000 n
人工智能 100000 n
个人信息保护 100000 n
数据安全 100000 n
网络安全 100000 n
未成年人网络保护 100000 n
网络暴力防治 100000 n
账号主体管理规定 100000 n
互联网信息服务管理办法 100000 n
网络信息内容生态治理规定 100000 n
信息网络传播权保护条例 100000 n
网络安全法 100000 n
数据安全法 100000 n
个人信息保护法 100000 n
未成年人保护法 100000 n
民法典 100000 n
行政处罚法 100000 n
关键信息基础设施安全保护条例 100000 n
网络产品安全漏洞管理规定 100000 n
移动互联网应用程序信息服务管理规定 100000 n
互联网直播服务管理规定 100000 n
网络信息内容多渠道分发服务机构相关业务活动管理规定 100000 n
"""

# 机构/实体常见的构词后缀 —— 挖掘候选词时用
_ORG_SUFFIX = ("部", "委", "局", "办", "厅", "院", "署", "中心", "协会",
               "学会", "基金会", "集团", "公司", "大学", "研究所")
_STOP = {"关于", "的通知", "的意见", "的公告", "有关", "进一步", "有关情况"}
# 拼接时要剥掉的前后标点（jieba 会把「，」「、」单独切成 token）
_PUNCT = "，。；、！？,.;!?：:（）()《》“”\"'‘’【】[]—… \t"
_PUNCT_RE = re.compile(r"[，。；、！？,.;!?：:（）《》“”\"'‘’【】\[\]—…]")


def available() -> bool:
    """jieba 装了没"""
    return _get() is not None


def _get():
    global _jieba, _load_failed
    if _jieba is not None or _load_failed:
        return _jieba
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            import jieba
        jieba.setLogLevel(60)
        jieba.initialize()
        for line in _BASE_TERMS.strip().splitlines():
            parts = line.split()
            if len(parts) >= 2 and parts[1].isdigit():
                jieba.add_word(parts[0], freq=int(parts[1]))
        _jieba = jieba
    except Exception:
        # 没装 jieba 不是致命错误 —— 全文检索会退化，但程序照常跑
        _load_failed = True
        _jieba = None
    return _jieba


def add_word(word: str, freq: int = 100000):
    """运行时补充领域词（从语料挖到的机构名走这里）"""
    j = _get()
    if j and word:
        j.add_word(word, freq=freq)


def cut(text: str, hmm: bool = False) -> List[str]:
    """分词。没装 jieba 时退回按标点切（仍能用于检索）"""
    if not text:
        return []
    j = _get()
    if j is None:
        return [w for w in re.split(r"[\s，。；、！？,.;!?（）()《》“”\"']+", text)
                if w]
    return [w.strip() for w in j.cut(text, HMM=hmm) if w.strip()]


def keywords(text: str, topk: int = 20,
             stop: Optional[Iterable[str]] = None) -> List[str]:
    """抽取关键词：去掉单字与停用词，只留 2 字以上的中文词/英文词"""
    stop = set(stop) if stop else set()
    seen: dict = {}
    for w in cut(text):
        if w in stop or len(w) < 2:
            continue
        if not re.search(r"[一-鿿A-Za-z0-9]", w):
            continue
        # 纯数字或纯标点不要
        if re.fullmatch(r"[\d\W]+", w):
            continue
        seen[w] = seen.get(w, 0) + 1
    return [w for w, _ in sorted(seen.items(), key=lambda kv: -kv[1])[:topk]]


_ORG_RE = None


# n-gram 拼接会把相邻标点 token 一起吞进来（「、工业和信息化部」）。
# 以及机构后缀本身也是常用字，「成功举办」以「办」结尾、「召开会议研究部署」
# 也在候选里 —— 只按后缀判断，垃圾会淹没真机构。
# 「以机构后缀结尾」不等于机构：「基层干部」以「部」结尾、「调研部」
# 也以「部」结尾，但都不是发文机关。用高频动词/介词开头或结尾来挡。
_BAD_HEAD = ("在", "看", "慰", "召", "举", "成", "召", "赴", "赴京",
             "会见", "出席", "推动", "推进", "开展")
_BAD_TAIL = ("举办", "召开", "部署", "研究", "推进", "活动", "行动",
             "慰问", "签署", "出席", "强调", "要求", "考察", "巡视",
             "述职", "讲话", "报告", "会议", "座谈", "论坛", "会见")


def org_like(word: str) -> bool:
    """像不像一个机构名 —— 粗判，供筛选候选用，最终仍要人工复核"""
    w = word.strip(_PUNCT)
    if not (3 <= len(w) <= 20):
        return False
    if w in _STOP:
        return False
    if w.startswith(_BAD_HEAD) or w.endswith(_BAD_TAIL):
        return False
    # 「？法院」这类：中间夹了标点
    if _PUNCT_RE.search(w):
        return False
    return w.endswith(_ORG_SUFFIX)


def mine_orgs(titles: Iterable[str], min_count: int = 3,
              min_len: int = 3, topn: int = 200) -> List[dict]:
    """
    从标题里挖掘机构候选词。

    这是**辅助**：它只给出「哪些串像机构名、出现多少次」，
    不保证正确。真正的判断（这是不是发文机关）仍然要人来做。

    做法：把每个标题切分成 n-gram（2~8 字），挑出以机构后缀结尾的，
    按出现次数排序 —— 高频 + 带机构后缀，是发文机关最典型的特征。
    """
    j = _get()
    if j is None:
        return []
    counts: dict = {}
    surface: dict = {}
    for t in titles:
        if not t:
            continue
        # 先切，再在相邻词之间拼 n-gram —— 这样不会跨过标点乱拼
        toks = cut(t)
        for i in range(len(toks)):
            acc = ""
            for jx in range(i, min(i + 4, len(toks))):
                acc += toks[jx]
                if not (min_len <= len(acc) <= 10):
                    continue
                if org_like(acc):
                    # ⚠ 必须存**剥掉标点**的形态：标题里「公安部、」和
                    # 「、公安部」是同一个机构，若原样入库，归并时它们
                    # 会变成两个不相等的 key，主流名甚至会被「、公安部」抢走
                    key = acc.strip(_PUNCT)
                    counts[key] = counts.get(key, 0) + 1
                    surface.setdefault(key, t)
    out = [{"org": w, "count": c, "example_title": surface[w]}
           for w, c in counts.items() if c >= min_count]
    out.sort(key=lambda x: (-x["count"], -len(x["org"])))
    return out[:topn]


# 机构名归一化规则，做**等值改写**而非删除。
# 「国家市场监督管理总局」与简称「市场监管总局」不是连续子串
# （中间多一个「监督管理」），LCP 和子串都判不出来。
# 但把「监督管理」改写成「监管」再去掉「国家」前缀，两边就完全一致了 ——
# 所以是「监督管理→监管」而不是「监督管理→空」。
# ⚠ 顺序敏感：长模式必须排在短模式前面，否则先删掉「管理」就再也匹配不到了。
_ORG_RULES = (
    ("中华人民共和国", ""),
    ("股份有限公司", "公司"),
    ("有限责任公司", "公司"),
    ("有限公司", "公司"),
    ("监督管理", "监管"),
    ("委员会", ""),
    ("总局", "局"),
)
# 政企机构常见层级前缀，去掉之后再比一次（简称的来源）
_ORG_PREFIX = ("中共中央", "中共", "中央", "中华", "中国", "国家", "全国",
               "省市自治区", "省", "市")


def _skeletons(s: str) -> Set[str]:
    """一个机构名归一后的可比形式集合（可能不止一种）"""
    a = s
    for pat, rep in _ORG_RULES:
        a = a.replace(pat, rep)
    out = {a}
    for p in _ORG_PREFIX:
        if a.startswith(p) and len(a) - len(p) >= 3:
            out.add(a[len(p):])
    return {x for x in out if x}


def _same_org(a: str, b: str, min_lcp: int = 3) -> bool:
    """两家机构是不是同一家：LCP≥min_lcp 或 一方是另一方的子串（短的≥min_lcp）
    或 去噪骨架相同/互为子串"""
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    if i >= min_lcp:
        return True
    if (a in b or b in a) and min(len(a), len(b)) >= min_lcp:
        return True
    sa, sb = _skeletons(a), _skeletons(b)
    if not sa or not sb:
        return False
    for x in sa:
        for y in sb:
            if x == y:
                return True
            if (x in y or y in x) and min(len(x), len(y)) >= min_lcp:
                return True
    return False


def normalize_orgs(orgs: List[dict]) -> List[dict]:
    """
    把同一家机构的多种写法归并成一个。

    实测挖出来是这样的：
        中央网信办网络局 31 / 中央网信办举报中心 12 / 通报中心 9
        国家市场监督管理总局 17 / 市场监管总局 18

    ⚠ 两种关系都要覆盖，缺一就会漏：
      ① **兄弟**：中央网信办网络局 与 中央网信办举报中心 互不包含，
         但共享「中央网信办」这个前缀。按子串判断会各自成孤岛。
      ② **简称/全称**：市场监管总局 ⊂ 国家市场监督管理总局。
         这两者**前缀完全不同**，只按 LCP 又会漏。

    所以用并查集，三条判据任一成立就并到一起（见 `_same_org`）：
      ① LCP≥3 字            → 兄弟节点
      ② 一方是另一方子串     → 简称全称（连续的）
      ③ 去噪骨架相同/互含    → 简称全称（不连续的，如「监督管理」）

    主名优先取「本身也是候选词的最长者」，否则取组内最长词。
    """
    if not orgs:
        return []
    # 同样先剥标点：调用方直接手写候选词时也可能带「、公安部」这种形态
    clean = {}
    for o in orgs:
        k = o["org"].strip(_PUNCT)
        if not k:
            continue
        prev = clean.get(k)
        if prev is None or o["count"] > prev["count"]:
            clean[k] = o
    if not clean:
        return []
    orgs = list(clean.values())
    names = list(clean)
    cnt = {k: o["count"] for k, o in clean.items()}
    ex = {k: o.get("example_title", "") for k, o in clean.items()}

    parent = {x: x for x in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            if _same_org(a, b):
                union(a, b)

    groups: dict = {}
    for x in names:
        groups.setdefault(find(x), []).append(x)

    out = []
    for members in groups.values():
        # 主名：本身就是候选词的最长者 → 取最长；否则也取最长
        main = max(members, key=len)
        variants = sorted(set(m for m in members if m != main))
        out.append({
            "org": main,
            "count": max(cnt[m] for m in members),
            "aliases": variants,
            "variants": sorted(set([main] + variants)),
            "example_title": ex.get(main, ""),
            "merged_from": sorted(members),
        })
    out.sort(key=lambda x: (-x["count"], -len(x["org"])))
    return out


def tokenize_query(q: str) -> List[str]:
    """
    把用户查询切成检索词。

    中文没有空格，FTS5 默认按空格切，所以「清朗网络谣言」整串进去
    基本搜不到。切成词组后用 OR / 组合，命中率会明显不同。
    """
    return [w for w in keywords(q, topk=8) if len(w) >= 2]