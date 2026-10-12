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
# ⚠ 「会」必须在内：全国人大常委会、最高人民法院、国资委等都以它结尾。
#   漏了它，这些高频真机构会被静默丢掉（第一版就漏了，测试里补上）。
_ORG_SUFFIX = ("部", "委", "局", "办", "厅", "院", "署", "会", "中心",
               "协会", "学会", "基金会", "集团", "公司", "大学", "研究所")
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
# ⚠ 实测「时政要闻」栏目（多领导人活动报道）漏得最多：
#   「习近平给中国农业大学」「北京看望慰问基层干部」「前瞻布局」
#   都是以机构后缀结尾却根本不是机构。所以这里按**任意位置**含动词就挡，
#   而不是只挡首尾 —— 宁可漏掉真机构，也不要让垃圾混进候选清单让人去误判。
_BAD_HEAD = ("在", "看", "慰", "召", "举", "成", "召", "赴", "赴京",
             "会见", "出席", "推动", "推进", "开展")
_BAD_TAIL = ("举办", "召开", "部署", "研究", "推进", "活动", "行动",
             "慰问", "签署", "出席", "强调", "要求", "考察", "巡视",
             "述职", "讲话", "报告", "会议", "座谈", "论坛", "会见")
# 出现在**任意位置**就判定不是机构名的成分
_BAD_ANY = ("给", "看望", "慰问", "讲话", "出席", "会见", "调研",
            "布局", "举行", "莅临", "赴任", "任免", "提名",
            "同志", "主席", "总理", "书记", "秘书长", "领导",
            "考察", "巡视", "座谈", "论坛", "宣讲", "辅导")


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
    if any(b in w for b in _BAD_ANY):
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
#
# ⚠ 「委员会」**必须用带$锚定的正则**，不能用 replace。
#   实测踩过：replace("委员会","") 把「第二十届中央委员会」变成「第二十届中央」，
#   骨架恰好等于「中央军委」的骨架 → 中央军委、三中全会、
#   中共中央政治局 全被并成一个主体。
#   只在**结尾**去掉才对：中央委员会 → 中央（这是真简称）。
_ORG_RULES = (
    (re.compile(r"中华人民共和国"), ""),
    (re.compile(r"股份有限公司"), "公司"),
    (re.compile(r"有限责任公司"), "公司"),
    (re.compile(r"有限公司"), "公司"),
    (re.compile(r"监督管理"), "监管"),
    # 结尾的「委员会」才去掉（中央委员会→中央）；
    # 中间的必须留着（第二十届中央委员会 去掉就撞上「中央」了）
    (re.compile(r"委员会$"), ""),
    (re.compile(r"总局"), "局"),
)
# 政企机构常见层级前缀，去掉之后再比一次（简称的来源）
_ORG_PREFIX = ("中共中央", "中共", "中央", "中华", "中国", "国家", "全国",
               "省市自治区", "省", "市")


def _skeletons(s: str) -> Set[str]:
    """一个机构名归一后的可比形式集合（可能不止一种）"""
    a = s
    for pat, rep in _ORG_RULES:
        a = pat.sub(rep, a)
    out = {a}
    for p in _ORG_PREFIX:
        if a.startswith(p) and len(a) - len(p) >= 3:
            out.add(a[len(p):])
    return {x for x in out if x}


# 机构层级标记。⚠ **这是本文件最容易出错的地方**，实测踩过两次：
#
#   ① 只按 LCP / 子串 / 骨架归并时，「省网信办」去掉「省」前缀后
#      骨架与「网信办」相同 → 与「中央网信办」并成一组 →
#      再经并查集传递，**中央和全国所有省级网信办被合成一个主体**。
#      两两判定每一步都「合理」，错的是传递闭包。
#   ② 反过来判得太粗也不行：「市场监管总局」以「市」开头，
#      但那个「市」是动词不是层级，判成市级会把它和
#      「国家市场监督管理总局」（中央级）挡开，简称全称就合并不了。
#
# 所以规则是：**层级词必须构成完整地名或以行政通名结尾才算层级**，
# 孤立的「市X局 / 省X委」不算。
_TIER_CENTRAL = ("中共中央政治局", "中共中央", "中央", "国务院", "国家",
                 "全国人大", "全国政协", "军委")
# 省/自治区/直辖市这类**完整行政地名**
_PROV_NAMES = ("河北", "山西", "辽宁", "吉林", "黑龙江", "江苏", "浙江",
               "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
               "广东", "广西", "海南", "四川", "贵州", "云南", "陕西",
               "甘肃", "青海", "台湾", "内蒙古", "西藏", "宁夏", "新疆",
               "北京", "上海", "天津", "重庆")
# 以行政通名结尾的市/县
_CITY_TAIL = ("市委", "市政府", "市委网信办", "县党委", "区政府", "市委网信")


def _tier(s: str) -> Optional[str]:
    """
    判断机构层级：central / prov / city / None（未定级）。

    「未定级」是**正常且重要**的返回值：公安部、教育部这类
    没有地域前缀的中央部委返回 None，此时不参与层级互斥，
    该合并的照样合并。
    """
    for p in _TIER_CENTRAL:
        if s.startswith(p):
            return "central"
    # 省级：完整地名 + 通名。
    # ⚠ 通名清单要够全：漏一个就会让「浙江省网信办」掉到后面那条末尾正则，
    #   然后被里面的「省」误判成市级。
    #   「省」本身也要算通名：「浙江省网信办」= 地名「浙江」+ 通名「省网信办」，
    #   rest 是「省网信办」而不是「网信办」。
    _PROV_TAIL = ("省", "省委", "自治区", "政府", "人大", "政协", "厅", "局",
                  "网信办", "党委")
    for name in _PROV_NAMES:
        if s.startswith(name) and len(s) > len(name) + 1:
            rest = s[len(name):]
            if rest.startswith(_PROV_TAIL):
                return "prov"
    if s.endswith(("省委", "自治区")):
        return "prov"
    for p in ("省", "自治区", "直辖市"):
        if s.startswith(p) and len(s) > len(p) + 1:
            rest = s[len(p):]
            # 「省网信办」这类通名写法仍算省级；但「市场…」不算
            if rest.startswith(("委", "政府", "网信办", "厅", "局", "人大")):
                return "prov"
    for tail in _CITY_TAIL:
        if tail in s:
            return "city"
    # ⚠ 末尾通名兜底**必须排除已被完整地名认领的**：
    #   「浙江省网信办」里有「省」也有「网信办」，若这条正则先跑，
    #   会把浙江省误判成市级（因为「省…网信办」的省落在了「浙江」后面）。
    #   完整地名是更强的证据，所以放在它之后、且要求前缀没有地名。
    if not any(s.startswith(n) for n in _PROV_NAMES):
        if re.search(r"[市区](党委|政府|人大|政协|网信办)$", s):
            return "city"
    return None


#: 骨架参与判同所需的最小长度。
#: 「网信办」「管理局」「秘书局」这类**通用词骨架不携带区分信息**，
#: 必须排除，否则中央/省/市网信办会被吸成一个主体。
_MIN_DISTINCT_SKEL = 4


#: 公共前缀要参与判同，除了够长，还得**有区分度**。
#: ⚠ 实测踩过两次，都是同一类错误 —— 公共部分不携带身份信息：
#:   · LCP=「中共中央」→ 把「中共中央国务院」和「中共中央政治局」当成兄弟，
#:     于是国务院、政治局、中央军委全部连成一个主体；
#:   · LCP=「浙江省」  → 把「浙江省网信办」和「浙江省通信管理局」当成兄弟；
#:   · 骨架=「网信办」  → 把中央/省/市网信办全部吸成一体。
#: 三条的共同点：重合的那部分（中共中央 / 浙江省 / 网信办）
#: 是**行政前缀或通用词**，两家真正的区别在它后面那几个字。
#: 所以：把前缀剥掉之后，各自剩下的**区分部分**必须有实质差异。
_MIN_TAIL = 4


#: 「下辖机构」类后缀 —— 出现它说明这是某家的内设单位而非独立主体。
#: 「中央网信办网络局」与「中央网信办举报中心」是同一家，
#: 但「中共中央国务院」与「中共中央政治局」是两个并列表语，不能合成一家。
#:
#: ⚠ 但**通名本身不算区分度**：把「局 / 办 / 部」当判据会让
#:   「浙江省网信办」与「浙江省通信管理局」被判同类（都以「局」结尾），
#:   而它们是两个完全独立的厅局级单位。
#:   所以真正的判据是「**去掉通名之后还有没有实质内容**」：
#:   网络局→网络 / 举报中心→举报，都是有内容的下辖机构名；
#:   网信办→(空) / 通信管理局→通信，「通信」只是行业通名。
_SUBORG_TAIL = ("局", "处", "司", "中心", "厅", "室", "部", "办",
                "组", "科", "大队", "总队", "支队")
#: 去掉通名后就没了 —— 说明整个区分部分只是通名，不构成下辖机构名
_EMPTY_TAIL = ("网信办", "管理局", "秘书局", "办公厅", "监管局", "政府",
               "党委", "委员会", "中心")


def _is_suborg(t: str) -> bool:
    """像不像「XX家的某个内设机构」——要有实质内容，不只是通名"""
    if not t or not t.endswith(_SUBORG_TAIL):
        return False
    if t in _EMPTY_TAIL:
        return False
    # 剥掉通名后还剩实质内容才算（网络局→网络 / 举报中心→举报）
    core = t[:-1] if len(t) > 1 else t
    return len(core) >= 2


def _distinct_tail(s: str) -> str:
    """剥掉行政前缀后剩下的「区分部分」"""
    t = s
    for p in sorted(_ORG_PREFIX + _TIER_CENTRAL, key=len, reverse=True):
        if t.startswith(p):
            t = t[len(p):]
            break
    for name in sorted(_PROV_NAMES, key=len, reverse=True):
        if t.startswith(name):
            t = t[len(name):]
            break
    for p in ("省委", "自治区", "市", "省", "区", "县"):
        if t.startswith(p) and len(t) - len(p) >= _MIN_TAIL:
            t = t[len(p):]
            break
    return t


def _same_org(a: str, b: str, min_lcp: int = 3) -> bool:
    """
    两家机构是不是同一家。判据：层级必须一致，然后
      ① LCP≥min_lcp          → 兄弟节点
      ② 一方是另一方的子串     → 简称全称（连续的）
      ③ 去噪骨架相同/互含      → 简称全称（不连续的，如「监督管理」）
    """
    # ⚠ 层级互斥先于一切。中央网信办 ≠ 省网信办 ≠ 市网信办，
    #   哪怕骨架一模一样 —— 它们是三个不同层级的机构。
    #   少了这一道，省网信办会被并进中央网信办，再经并查集传递
    #   把全国所有省级网信办连成一个主体。
    ta, tb = _tier(a), _tier(b)
    if ta and tb and ta != tb:
        return False
    # ⚠ **通用词骨架不可用于判同**。
    #   「中央网信办 / 省网信办 / 市网信办 / 全国网信办」去掉地域前缀后
    #   骨架**全都是「网信办」**，同理还有「管理局 / 秘书局 / 办公厅」。
    #   用它判同会把几十个互不相干的机构吸成一个巨无霸，
    #   而且因为并查集有传递性，一旦错了就再也拆不开。
    #   只有「有区分度的骨架」才可信 —— 也就是剥离后**两边都还剩
    #   至少 4 个字**，说明这个骨架真的携带了区分信息
    #   （如「市场监管总局」vs「国家市场监督管理总局」都剩 6 字）。
    n = min(len(a), len(b))
    i = 0
    while i < n and a[i] == b[i]:
        i += 1
    lcp = i
    if lcp >= min_lcp:
        # ⚠ 公共前缀必须**有区分度**才算兄弟关系。
        #   LCP 只是「中共中央」或「浙江省」时，两边真正的区别（国务院 /
        #   政治局、网信办 / 通信管理局）还没出现，不足以判定是同一家。
        #   正确判据：剥掉公共前缀后，剩下的区分部分要**基本相同**。
        ta_, tb_ = _distinct_tail(a[lcp:]), _distinct_tail(b[lcp:])
        if ta_ and tb_:
            if ta_ == tb_ or (ta_ in tb_ or tb_ in ta_):
                return True
            # 区分部分不同 —— 但有两种例外仍算同一家：
            #  ① 两边都是「下辖机构」类后缀：中央网信办网络局 / 举报中心，
            #     它们是同一家的内设单位，不是两个主体。
            #  ② 尾部本身就短到没有区分度。
            # 除此之外一律否掉：「中共中央国务院 / 中共中央政治局」是
            # 两个**并列表语**，合成一个会让国务院、政治局、军委全连成一组。
            if _is_suborg(ta_) and _is_suborg(tb_):
                return True
            if len(ta_) < _MIN_TAIL and len(tb_) < _MIN_TAIL:
                return True
            if len(ta_) >= _MIN_TAIL and len(tb_) >= _MIN_TAIL:
                return False
    if (a in b or b in a) and min(len(a), len(b)) >= min_lcp:
        return True
    sa = {x for x in _skeletons(a) if len(x) >= _MIN_DISTINCT_SKEL}
    sb = {x for x in _skeletons(b) if len(x) >= _MIN_DISTINCT_SKEL}
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
        # ⚠ 归并**不能当成定论**。并查集有传递性：
        #   a~b、b~c 都成立不代表 a~c 合理（中央/省/市就是这么被搅在一起的）。
        #   所以标出每组的合并依据与风险，供人复核 ——
        #   组里出现不同层级，或靠「骨架相同」而非「前缀/子串」连接，
        #   都标 needs_review。宁可多标，不可让错误静默通过。
        tiers = {_tier(m) for m in members} - {None}
        by_prefix = any(
            _lcp_len(m1, m2) >= 3 or (m1 in m2 or m2 in m1)
            for m1 in members for m2 in members if m1 != m2)
        risky = len(tiers) > 1 or not by_prefix
        out.append({
            "org": main,
            "count": max(cnt[m] for m in members),
            "aliases": variants,
            "variants": sorted(set([main] + variants)),
            "example_title": ex.get(main, ""),
            "merged_from": sorted(members),
            "needs_review": risky,
            "merge_basis": ("前缀/包含" if by_prefix else "仅骨架相同"),
        })
    out.sort(key=lambda x: (-x["count"], -len(x["org"])))
    return out


def _lcp_len(a: str, b: str) -> int:
    i = 0
    while i < min(len(a), len(b)) and a[i] == b[i]:
        i += 1
    return i


def tokenize_query(q: str) -> List[str]:
    """
    把用户查询切成检索词。

    中文没有空格，FTS5 默认按空格切，所以「清朗网络谣言」整串进去
    基本搜不到。切成词组后用 OR / 组合，命中率会明显不同。
    """
    return [w for w in keywords(q, topk=8) if len(w) >= 2]