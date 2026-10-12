# -*- coding: utf-8 -*-
"""
中文新词发现（无监督统计法）+ 词典回流。

── 为什么不能用「看哪几个词被切碎了」这种朴素办法 ──
直觉是「中央网信办」被切成 中央/网信/办 就该收进来。但这个信号有两个硬伤：
  ① 「北京大学」在**有词库时本来就不该被切**，朴素法会把它当新词收一遍，
     白占词典位；
  ② 切碎的诱因是词频低，而低频词恰恰是最容易判错的 ——
     实测「更新」「推动」「加强」这类高频常用词反而最该被切碎，
     它们被切碎是**正确的**分词，不是问题。
所以这里走**凝固度 + 邻字自由度**这条无监督的路：不需要标注，
也不用人工词表，靠语料自身的统计规律把「该粘在一起的串」筛出来。

── 四条判据，各管一件事 ──
① 凝固度（PMI 类，_cohesion）
   邻接度 P(w)·P(w') 与 P(ww) 的比值。切碎的必然左边低频右边高频，
   乘积小；成词的左右都常出现，乘积大。
   ⚠ 必须是**相对**判据（比值 > 阈值），不是绝对值 ——
     高频词绝对 PMI 天然高，低频新词绝对值可能很低却仍然很成词。

② 邻字自由度（邻字熵，_left_entropy / _right_entropy）
   一个真词，左边能接的字**多且杂**（「网络谣言」「手机谣言」「AI 谣言」…），
   而「都市的」这种碎片左边永远只有「市」。
   这一条专杀「看起来频率高、其实是固定搭配尾巴」的伪词。

③ 子串可切分性（_splittable）—— **不加这条，前面两条会一起失效**
   公文里「实现发展」「推进落实」「地区和国际」这类**动宾/偏正短语**重复率极高，
   凝固度和邻字熵**都能过**（实测它们的熵甚至有 2.1，比真词还高）。
   区分它们的唯一办法是问：它能不能被切成两个**本已成立**的词？
   「网络谣言」切不开；「实现|发展」两个都是常用词，切得开。
   → 所以必须知道当前分词器认哪些词，这里走 jieba 词典（可选依赖）。
     拿不到 jieba 时退化为「高频单字可切」弱判据，宁可多报也不漏。

④ 领域白名单（domain_hint）
   本项目是网信办语料，候选里若含已知治理术语
   （清朗 / 净网 / 深度伪造 / 算法推荐…）或机构后缀，优先保留。

── 与 jieba 的关系 ──
jieba 的 `add_word` 只在**当前进程**有效，重启即失效；
`jieba.load_userdict` 读的是磁盘文件。所以回流的落点必须是磁盘上的
用户词典文件（本模块 `userdict_path()`），由 `_get()` 在初始化时加载 ——
这才是「自学习」闭环：新词被采纳 → 写进 userdict → 重启后仍在。

⚠ jieba 是**可选依赖**：本模块不 import jieba 也能统计，
   只是回流产出的文件暂时无人消费。绝不能因为可选依赖让程序起不来。
"""
from __future__ import annotations

import math
import os
import re
from collections import Counter, defaultdict
from typing import Dict, Iterable, List, Optional, Tuple

# ── 判据阈值 ────────────────────────────────────────────────────────────
# 凝固度：低于此值说明这个串「只是碰巧相邻」，不是词
MIN_COHESION = 4.0
# 邻字熵（自然对数）：低于此值说明左右搭配很固定，是伪词
MIN_SIDE_ENTROPY = 0.6
# 候选串长度范围。2 字词本就是 jieba 的弱项（「清朗」「净网」），
# 但 1 字不成词，>6 字在公文里基本都是长句片段
MIN_LEN = 2
MAX_LEN = 6
# 每个候选至少要出现这么多次，否则可能是偶发串
MIN_COUNT = 5
# 单次最多挖多少个，避免长语料把内存吃满
TOPN = 300

_CJK = re.compile(r"[一-鿿]+")


def _sentences(texts: Iterable[str]) -> Iterable[str]:
    """产出可用于统计的连续中文片段（按标点/换行断开）。

    ⚠ 跨句子拼 n-gram 会造出「……出台」+「相关」这种跨句伪词，
      所以必须先断句。标点一律当断点处理。
    """
    for t in texts:
        if not t:
            continue
        for piece in re.split(r"[^一-鿿]+", t):
            if piece:
                yield piece


def _entropy(c: Counter) -> float:
    """邻字分布的香农熵（自然对数）。分布越均匀，熵越大。"""
    s = sum(c.values())
    if s <= 0:
        return 0.0
    h = 0.0
    for v in c.values():
        p = v / s
        h -= p * math.log(p)
    return h


# 判定「是否为已知词」时排除的功能词/虚词：
# 它们切不切得开都不影响判断，留着会让「的」「了」这类到处粘的串得分虚高。
# ⚠ 公文里虚词密度远高于日常汉语，"时/中/内/后/前/上/下" 这类**方位时间词**
#   夹在中间会让「在全党开展」「问题上始终」这种碎片跨过关卡，必须一起拦。
_DROP_CHARS = set("的了和与及或在为是对把被就都也很更最又而且但是所以因为"
                  "时中内后前上下里外从向给让使将会能可要该各此这那其"
                  "等等又则乃亦即若如虽但并或者等")


def _is_known(word: str) -> bool:
    """分词器认不认这个词（jieba 可选，认不到时返回 False）"""
    try:
        from . import textseg
    except Exception:
        return False
    j = textseg._get()
    if j is None:
        return False
    try:
        # freq > 0 且是整词，说明它在词典里
        return j.get_FREQ(word, 0) > 0
    except Exception:
        return False


def _splittable(word: str, uni: Counter, total: int) -> bool:
    """
    这个候选能不能被切成**两个本已成立的词**？

    能切 → 多半是短语（「实现|发展」「推进|落实」），不是新词。
    切不开 → 才是真正的复合词（「网络|谣言」其实切得开，
    但「网信办」「生成式」这类切不开）。

    ⚠ 判据用「两侧各自够不够独立成词」：
      对每个切点算两侧的凝固度，任一侧很低 → 说明那侧本身不是词 → 不可切。
      这比直接查词典更稳，因为候选词本来就不在词典里。

    ⚠ 高频虚字（的了和…）必须排除：
      「发展了」能被切成「发展|了」，但「了」根本不算词，
      这种切法没有意义。
    """
    L = len(word)
    if L < 3:
        # 2 字词没有合法的二段切法（切出来必有一个单字）
        return False
    for i in range(1, L):
        a, b = word[:i], word[i:]
        if not a or not b:
            return False
        # 任一侧含虚字头/尾 → 这种切分不算数
        if a[0] in _DROP_CHARS or b[0] in _DROP_CHARS:
            return False
        if a[-1] in _DROP_CHARS or b[-1] in _DROP_CHARS:
            continue
        # 两侧若本身都在词典里，明确判定为可切（最可靠）
        if _is_known(a) and _is_known(b):
            return True
        # 退一步：两侧都是高频单字组合，也算短语
        fa = uni.get(a[0], 0) + uni.get(a[-1], 0)
        fb = uni.get(b[0], 0) + uni.get(b[-1], 0)
        if fa >= total * 0.005 and fb >= total * 0.005:
            return True
    return False


def discover_new_words(texts: Iterable[str],
                      min_count: int = MIN_COUNT,
                      min_cohesion: float = MIN_COHESION,
                      min_entropy: float = MIN_SIDE_ENTROPY,
                      known: Optional[Iterable[str]] = None,
                      topn: int = TOPN) -> List[dict]:
    """
    从语料中发现候选新词。

    → [{"word":…, "count":…, "cohesion":…, "left_entropy":…,
        "right_entropy":…, "score":…, "hint":…}, …]，按 score 降序。

    ⚠ 结果是**候选**，不是结论。是否采纳由人决定（前端可勾选）——
      统计法能保证「大概率是词」，但保证不了「在这个领域里该收」。
    """
    uni: Counter = Counter()          # 单字频次
    span: Counter = Counter()         # 候选 n-gram 频次
    left: Dict[str, Counter] = defaultdict(Counter)    # 候选 → 左邻字分布
    right: Dict[str, Counter] = defaultdict(Counter)   # 候选 → 右邻字分布

    for seg in _sentences(texts):
        n = len(seg)
        for i, ch in enumerate(seg):
            uni[ch] += 1
        # n-gram：长度 MIN_LEN..MAX_LEN
        for L in range(MIN_LEN, MAX_LEN + 1):
            for i in range(n - L + 1):
                w = seg[i:i + L]
                span[w] += 1
                if i > 0:
                    left[w][seg[i - 1]] += 1
                if i + L < n:
                    right[w][seg[i + L]] += 1

    if not span:
        return []

    total_chars = sum(uni.values()) or 1
    known_set = set(known or ())

    out: List[dict] = []
    for w, c in span.items():
        if c < min_count:
            continue
        # ⚠ 首尾是虚字的一律不是词：「在主持会议时」「培育更多」
        #   「可以完全可以」这类串统计指标很漂亮（公文里重复率高），
        #   但它们是**短语被截断的碎片**，收进词典只会污染切分。
        #   这条必须在打分之前，否则碎片会靠高熵挤掉真词。
        if w[0] in _DROP_CHARS or w[-1] in _DROP_CHARS:
            continue
        # 凝固度：log( P(ww) / (P(w)·P(w')) )
        pb_left = (span.get(w[:-1], 0) / total_chars) if len(w) > 1 else 0
        pb_right = (span.get(w[1:], 0) / total_chars) if len(w) > 1 else 0
        p_ww = c / total_chars
        if pb_left <= 0 or pb_right <= 0:
            cohesion = 0.0
        else:
            cohesion = math.log(p_ww / (pb_left * pb_right))
        le = _entropy(left[w])
        re_ = _entropy(right[w])
        # ⚠ 能切成两个已知词 → 是短语不是词，直接淘汰。
        #   这条必须放在打分**之前**，否则短语的高熵会把它顶进结果里。
        if _splittable(w, uni, total_chars):
            continue
        # ⚠ **邻字熵必须独立生效**，不能和 score 混在一个条件里。
        #   原来写的是 `if score < min_cohesion and min(le,re_) < min_entropy`，
        #   看起来是两个条件与，实际等于**什么都不筛**：
        #     score = cohesion + 0.6*min(le,re_) + domain_weight
        #     而 cohesion 在公文里普遍 10+，阈值只有 4.0
        #   ⇒ score 永远大于 min_cohesion ⇒ 第一个条件恒为假 ⇒ 整条筛选是死代码。
        #   实测后果：300 个候选里 **55 个（18%）邻字熵为 0 照样放行**，
        #   而「江大数据交」「共同维护清朗」「央委员会的委」这类
        #   长短语被切出的内部窗口，熵全是 0 —— 它们本该是最该被淘汰的一批。
        #
        #   邻字熵 = 0 的含义很硬：这个串**只在一个固定上下文里出现过**。
        #   真正的词会被不同句子、不同搭配包围；只有被长词夹在中间的
        #   碎片才会永远只有同一个左邻和同一个右邻。
        #
        #   ⚠ 这里**故意不对机构名开后门**。曾经按「命中机构后缀就放行」
        #   处理低熵，实测反而放进来 7 个全是碎片：
        #     央委员会的委 ← 中央委员会委员
        #     全和信息化委 ← 全国信息化委员会
        #     央行征信中心 ← 中国人民银行征信中心
        #     市传媒中心 ← 湖州市传媒中心
        #   因为机构后缀本身也是长短语的尾巴。而真正的机构称谓
        #   （如「研究院副院长」熵=1.0）本来就够自由，不需要开后门。
        #   统计判据只看邻字分布，不看词类 —— 这是它比任何白名单可靠的地方。
        if min(le, re_) < min_entropy:
            continue
        # ⚠ **分词器已经认识的词不是「新词」**，必须直接剔除。
        #   只在结果里标一个 in_dict 字段是不够的 —— 实测里
        #   「中央网信办」这种内置词典早就收录的词照样排进了候选，
        #   等于让用户把已有的东西再采纳一遍，白占篇幅还容易被误当成
        #   「程序发现了新东西」来汇报。
        if known_set and w in known_set:
            continue
        score = cohesion + 0.6 * min(le, re_) + domain_weight(w)
        if score < min_cohesion:
            continue
        out.append({
            "word": w,
            "count": c,
            "cohesion": round(cohesion, 3),
            "left_entropy": round(le, 3),
            "right_entropy": round(re_, 3),
            "score": round(score, 3),
            "domain": round(domain_weight(w), 2),
            "in_dict": w in known_set,
        })
    out.sort(key=lambda x: (-x["score"], -x["count"]))
    return out[:topn]


# ── 领域提示词：候选里含这些，视为本领域术语，排序上浮 ──────────────────
_DOMAIN_HINT = (
    "清朗", "净网", "深度伪造", "算法推荐", "个人信息", "数据安全",
    "网络安全", "未成年人", "网络谣言", "水军", "有偿删帖", "生成式",
    "人工智能", "大数据", "信息化", "网络安全宣传周", "违法和不良信息",
)
_ORG_SUFFIX = ("部", "局", "委", "办", "中心", "协会", "研究院",
               "大会", "论坛", "专项行动", "行动计划", "实施方案")

# 单字后缀（部/局/委/办）信息量太小，必须靠更长的整体来证明它真是机构名。
# ⚠ 这条不是补丁，是「单字后缀弱信号、长后缀强信号」的通用判据。
#   实测反例：_ORG_LEAD 有「县」、_ORG_SUFFIX 有「部」，于是
#   「县各族干部」（来自「…县各族干部群众…」）被判成机构名直接加权，
#   一度排到候选表第一。它根本不是机构，是「县各族」+「干部」被拼起来的。
#   同样的坑还有「县工会」「乡卫生」（工/生 也在 _ORG_LEAD）。
#   加黑名单「干部」是治标——「干部」可以列，「教干」「片干」就列不完，
#   而且这是第二次栽在黑名单路线上（第一次是拦动词开头）。
#   改判据本身：单字后缀要求整体 ≥6 字，多字后缀（「中心」「专项行动」）
#   本身携带语义，仍按 ≥4 字算。
_SUFFIX_MIN_LEN = {1: 6, 2: 4}


def _org_suffix_hit(word: str) -> Optional[str]:
    """候选是否以机构/政策后缀结尾，返回命中的后缀（无则 None）。

    对应 domain_weight / domain_hint 共用的约束，**改一处必须两处都走它**，
    否则界面会显示「以机构后缀结尾」而排序却不加权，自相矛盾。
    """
    for s in _ORG_SUFFIX:
        if word.endswith(s):
            return s
    return None


def _org_boostable(word: str) -> bool:
    """是否够格享受「机构/政策后缀」加权：白名单起首 + 长度够长。"""
    if word[0] not in _ORG_LEAD or len(word) < 4:
        return False
    s = _org_suffix_hit(word)
    return bool(s) and len(word) >= _SUFFIX_MIN_LEN.get(len(s), 4)


def domain_hint(word: str) -> Optional[str]:
    """判断候选是否带本领域特征，返回提示类型（无则 None）。

    ⚠ 与 domain_weight 共用 _org_boostable()：提示和排序必须同判据，
      否则界面会显示「以机构后缀结尾」而排序却不加权，自相矛盾，
      用户会以为这是 bug。
    """
    for h in _DOMAIN_HINT:
        if h in word:
            return f"含领域词「{h}」"
    if not _org_boostable(word):
        return None
    return f"以机构/政策后缀「{_org_suffix_hit(word)}」结尾"


# 领域加权：命中领域特征的候选上浮排序。
# ⚠ 为什么需要它：纯统计在通用公文语料上会大量捞出
#   「阿富汗」「卢旺达」「历史文化街区」这类**对网信舆情分析无价值**的词
#   ——它们统计上确实是词，但本项目要的是治理术语。
#   加权不是「只保留领域词」（那样会漏掉新型治理术语），
#   而是让领域词**排在前面**，人先看最相关的那批。
DOMAIN_BOOST = 3.0
ORG_SUFFIX_BOOST = 1.5



# 机构/政策名的**行政与地域限定字**。
# ⚠ 这里刻意用**白名单**而不是「黑名单拦开头」——
#   黑名单路线实测失败了：先拦动词，「精神的开局」「装箱中心」
#   「征程党的中心」照样冒出来顶掉真词，因为任何有限的黑名单都列不完。
#   真机构名几乎都从行政/地域字起头，所以只认这些字更可靠。
_ORG_LEAD = set("中央国家中国省市县区州部委民政公检法军"
                "全国省县乡村工农商教科文体卫生")


def domain_weight(word: str) -> float:
    """
    领域相关性权重（0 = 与本项目无关）。

    ⚠ 后缀加权必须**配约束**，否则会被碎片刷爆：
      实测不加约束时，「精神的开局」「装箱中心」
      这些明显是碎片的串，只因结尾撞上「局」「中心」
      就被加权顶到列表最前面 —— 加权反而毁掉了结果质量。
      约束见 _org_boostable()：白名单起首 + 长度门槛，
      且单字后缀要更长的整体（_SUFFIX_MIN_LEN）才算数。
    """
    w = 0.0
    for h in _DOMAIN_HINT:
        if h in word:
            w += DOMAIN_BOOST
            break
    if _org_boostable(word):
        w += ORG_SUFFIX_BOOST
    return w


# ── 词典回流：落盘给 jieba 用 ──────────────────────────────────────────

def userdict_path(data_dir: str) -> str:
    """
    用户词典落盘路径。

    放 data 目录而不是包内：包目录在分发包里可能被只读部署，
    而 data 目录本来就是可写的、且和数据库放在一起便于整体备份/迁移。
    """
    return os.path.join(data_dir, "userdict.txt")


def write_userdict(path: str, words: Iterable[str],
                   freq: int = 100000) -> int:
    """
    把采纳的新词写进 jieba 用户词典。

    ⚠ 写成「词 词频 词性」三列，是 jieba 官方 userdict 格式。
      词频必须给大（默认十万），否则压不过内置通用词典 ——
      新挖出来的词多是领域词，默认词典里恰好不认识它们，
      给小了仍会被切碎，等于回流没生效。
    ⚠ **追加而非覆盖**：文件里可能有人手工加过词，
      全量重写会悄悄抹掉。已有行保持原样，只补缺失的。
    """
    existing: List[str] = []
    seen = set()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                s = line.rstrip("\n")
                if not s.strip():
                    continue
                existing.append(s)
                seen.add(s.split()[0])
    added = 0
    for w in words:
        w = (w or "").strip()
        if not w or w in seen:
            continue
        existing.append(f"{w} {freq} n")
        seen.add(w)
        added += 1
    if added:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("\n".join(existing) + "\n")
    return added


def read_userdict(path: str) -> List[str]:
    """读回已采纳的词（用于界面展示与再编辑）"""
    out: List[str] = []
    if not os.path.exists(path):
        return out
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if s:
                out.append(s.split()[0])
    return out


def known_words(path: Optional[str] = None) -> set:
    """当前分词器已经认识的词：内置领域词表 + jieba 词典 + 已回流词。

    ⚠ 三份都必须算，漏一份就会把「中央网信办」这种早就有的词
      当成新词报给用户 —— 用户以为程序发现了新东西，
      其实是它自己的词典里本来就有，白占篇幅还误导结论。

    ⚠⚠ 这个函数必须**只有一份实现**。先前 web 层和 MCP 层各写了一遍，
      两边靠不同的方式取 jieba 词典（一份走 j.dictionary，一份想读
      dict_file），迟早会漂移：某天只改一边，
      「网页上不报旧词」而「MCP 里照报」，用户完全无从察觉。
      口径统一到这个函数，两条入口才必然一致。
    """
    known: set = set()
    try:
        from . import textseg as TS
        for line in (TS._BASE_TERMS or "").strip().splitlines():
            parts = line.split()
            if parts:
                known.add(parts[0])
        j = TS._get()
        if j is not None:
            try:
                known |= set(j.dictionary.dictionary.keys())
            except Exception:
                pass
    except Exception:
        pass
    known |= set(read_userdict(path or userdict_path()))
    return known
