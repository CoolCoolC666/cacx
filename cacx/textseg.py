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
from typing import Iterable, List, Optional, Set, Tuple

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
# T5：这两个固定建制名此前被构词法/活动尾巴误杀（见 ORG_WHITELIST），
# 这里强制整切，否则 n-gram 拼不出完整机构名。
中共中央 100000 n
中国人民政治协商会议 100000 n
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

#: T5 固定建制名白名单 —— 只按**精确全名**放行。
#:
#: 为什么必须单独列：
#:   ·「中共中央」尾字「央」、「中国人民银行」尾字「行」都不在 _ORG_SUFFIX，
#:     构词法直接判 False，但它们是真机构；
#:   ·「中国人民政治协商会议」尾巴「会议」撞上 _BAD_TAIL；
#:   ·「全国人民代表大会」尾巴「大会/代表大会」撞上 extract._NOT_ORG_TAIL。
#:
#: ⚠ 绝不能整类放开「会议/大会」——那会把「新闻发布会」「高峰论坛」
#:   「乌镇峰会」「第三届会员代表大会」「四中全会」这些**活动名**全放回图谱，
#:   污染机构节点。所以这里只列全名、不列简称（全国人大/人民政协辨识度低，
#:   放开会引入噪声），extract._plausible_org 也复用这一份白名单。
ORG_WHITELIST = frozenset({
    "中共中央",
    "中国人民银行",
    "中国人民政治协商会议",
    "全国人民代表大会",
})

#: T7 活动/会议名的**精确全名**排除表（整串命中即否）。
#:
#: 为什么需要：
#:   机构候选挖掘（`mine_orgs`）只过 `org_like` 这一道粗筛，而活动/会议名的
#:   尾巴常常是「会 / 院」，恰好落在 `_ORG_SUFFIX` 里 → 粗筛放行 → 它们挤进
#:   「高频机构 Top」（实测：互联网大会乌镇峰会 153、大数据产业博览会 68…）。
#:   `org_like` 自己的 `_BAD_TAIL` 只拦了「会议 / 论坛 / 座谈」，
#:   拦不住「大会 / 峰会 / 博览会 / 研讨会 / 理事会 / 全会」这一族。
#:
#: 怎么做：
#:   只列**具体全名**（本次现场点名的活动名 + 它们的变体 + 拼接残片），
#:   整串相等才否。**绝不整类放开「会议 / 大会」**（R5 §9）—— 那会把
#:   「全国人大常委会」「中国人民政治协商会议」这类真机构一起误杀。
#:   变体（带年份前缀、不同掐头去尾）由下面的 `ORG_BANNED_TAILS` 兜。
ORG_BANNED_TERMS = frozenset({
    # —— 现场点名的活动/会议名（完整全名，见 R5 汇报）——
    "互联网大会乌镇峰会", "大数据产业博览会", "中国网络文明大会",
    "组织成员国元首理事会", "重要思想理论研讨会", "回归祖国25周年大会",
    "的二十届四中全会",
    # —— 上述活动名的常见变体（不带年份的裸串）——
    "元首理事会", "二十届四中全会", "四中全会",
    # —— 两个真机构**连写**成的拼接残片：不是单一主体，按整串排除 ——
    #    （不去动拼接框架，只把已经污染榜单的残片逐个拉黑）
    "全国人大常委会国务院", "中共中央国务院", "国务院和中央军委",
})

#: T7 活动/会议名的**无歧义活动尾巴**（后缀命中即否）。
#:
#: 为什么用后缀而不是把每个变体写进 `ORG_BANNED_TERMS`：
#:   `mine_orgs` 会拼出大量 n-gram 变体（实测以活动尾巴结尾的高频候选有
#:   95 个：「2024年乌镇峰会 / 年世界互联网大会 / 的二十届三中全会 /
#:   两院院士大会 …」）。逐串枚举是**打地鼠**：拉黑主名后，同族的
#:   其它变体立刻顶上来当主名。所以对**没有真机构会以它结尾**的活动尾巴，
#:   用后缀统一拦。
#:
#: ⚠ 关键：这里**不含裸「会议 / 大会 / 全会 / 代表大会」**（R5 §9）——
#:   它们和「全国人大常委会」「中国人民政治协商会议」「全国人民代表大会」
#:   撞尾，整类否会把真机构一起杀掉。只收**多字的、无歧义的活动标记**。
#:   每条都已核过：没有任何真机构以它结尾。
ORG_BANNED_TAILS = (
    "峰会", "博览会", "研讨会", "交流会", "对话会", "恳谈会", "观摩会",
    "推进会", "报告会", "招待会", "发布会", "宴会", "开幕式", "闭幕式",
    "周年大会", "中全会", "互联网大会", "文明大会", "院士大会",
    "奖励大会", "分享会",
)

#: T8 通用形态闸门的**活动形态尾缀**表（元组）。精选模式（`mine_orgs_refine` /
#: `db.top_orgs_refine` / 图谱 mode=refine）用它判定「这串是不是活动/会议片段」。
#:
#: 与 `ORG_BANNED_TAILS` 的关系：有重叠没关系 —— 后者是**无歧义活动尾巴**，
#: 在粗筛 `org_like()` 里就拦（连粗选都不放行）；这里是形态判定用的**更宽**的表，
#: 单独一条尾巴不足以定案（见 `eventish_org` 的条件 b/c/d 要动词/序数/「的」配）。
#:
#: ⚠ 裸「会 / 委员会 / 法院 / 检察院 / 政府」**都不在表内** —— 它们是真建制的尾巴
#:   （全国人大常委会 / 政协第十四届全国委员会 / 杭州互联网法院…），
#:   放进来就是整类误杀（R5 §9 的老坑）。
#: ⚠ 表是「含」（至少这些），不是穷举：给定清单之外补了现场 Top50 残留里的
#:   同一族尾巴（见下方注释），否则「周年文艺晚会」这类照样漏。
ORG_EVENT_TAILS = (
    # —— 给定清单 ——
    "大会", "会议", "理事会", "论坛", "峰会", "博览会", "研讨会", "交流会",
    "年会", "运动会", "大赛", "庆典", "仪式", "典礼", "座谈会", "全会",
    "代表大会", "发布会", "推进会", "恳谈会", "观摩会", "对话会", "报告会",
    "招待会", "分享会", "奖励大会", "院士大会", "互联网大会", "文明大会",
    "周年大会", "中全会", "开幕式", "闭幕式", "宴会",
    # —— 补充：现场 Top50 残留里的同族活动尾巴 ——
    #   「周年文艺晚会」8 /「全国政协新年茶话会」6 /「春节团拜会」6 /
    #   「开幕会」5 /「闭幕会」5 /「发表70周年纪念大会」6 /
    #   「民族团结进步表彰大会」6 /「60周年庆祝大会」5
    #   给定清单没有「晚会 / 茶话会 / 团拜会 / 开幕会 / 闭幕会」这一族，
    #   漏了它们，「周年文艺晚会」就仍然留在榜上。
    "晚会", "茶话会", "团拜会", "开幕会", "闭幕会",
    "庆祝大会", "纪念大会", "表彰大会",
)

#: 活动形态尾缀里**真机构绝不会以它结尾**的那一批 —— 可以只凭尾巴定案
#: （`eventish_org` 条件 a′）。
#:
#: ⚠ 「理事会」**必须排除**：欧洲理事会 / 欧盟理事会 / 亚奥理事会 /
#:   APEC工商咨询理事会都是真建制（实测库内 35 个以「理事会」结尾的 org 名里
#:   有 4 个是真机构）。所以「理事会」只留给**带动词或序数**的重条件（b/c）：
#:   「协会召开第二届理事会」命中，「欧洲理事会」不命中。
#: ⚠ 「大会 / 会议」留在表内是安全的：实测库内以它们结尾的 org 名各只有 1 个 ——
#:   全国人民代表大会（白名单）与中国人民政治协商会议（白名单），都已先放行。
_EVENT_TAILS_PLAIN = tuple(t for t in ORG_EVENT_TAILS if t != "理事会")

#: 活动动词上下文（条件 b）。串里带这些动作词，说明它不是纯建制名。
_ORG_EVENT_VERBS = ("召开", "举行", "举办", "承办", "出席", "参加")

#: 序数（条件 c）。⚠ 必须同时收**中文数字**：「第九届」「第十一次」「第二届」
#: 都是中文数，只写 `第\d+[届次]` 会漏掉它们（实测这三例全漏）。
_ORDINAL_RE = re.compile(r"第[0-9〇零一二三四五六七八九十两]{1,4}[届次]")

#: 序数判定时承认的活动尾巴 —— 比 `ORG_EVENT_TAILS` 窄，只留「开一次会/办一场赛」的。
#: ⚠ 刻意**不含「委员会」**：「第二十届中央委员会」是真建制，不得因序数被滤。
_ORG_ORDINAL_TAILS = ("代表大会", "理事会", "大会", "全会", "会议", "年会",
                      "运动会", "大赛")

#: 建制词 / 行政司法机构后缀 —— 以它结尾且**串内无活动动词**者判为真建制，
#: 形态闸门放行（保护优先于一切命中条件）。
#:
#: ⚠ 这里都是**具体后缀**，不是裸「会 / 院 / 委」：裸后缀会连「周年文艺晚会」
#:   「闭幕会」一起保护掉，闸门当场失效。
_ESTABLISHED_TAILS = (
    "人民政府", "人大常委会", "常委会", "中央委员会", "委员会", "组委会",
    "人民法院", "人民检察院", "法院", "检察院", "网信办", "办公室", "办公厅",
    "人民政府办公厅", "总局", "央行", "政府", "党委", "人大", "政协",
    "中心", "部", "委", "署", "厅", "局", "院", "办",
)

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
        _load_userdict(jieba)
    except Exception:
        # 没装 jieba 不是致命错误 —— 全文检索会退化，但程序照常跑
        _load_failed = True
        _jieba = None
    return _jieba


def _load_userdict(j):
    """
    启动时把用户词典加载进来 —— **「自学习」闭环的关键一步**。

    ⚠ 缺这一步，自学习就是假的：采纳的新词只写进了磁盘文件，
      而 jieba 的 add_word 只对**当前进程**有效，
      下次重启词典就白写了。用户会以为采纳没生效。
    ⚠ 路径不能在 import 时算：那时 PROJECT_ROOT 未必就绪。
      这里在 _get() 里才算，是第一次真正要用分词器的时候。
    ⚠ 读失败不能让分词整体挂掉 —— 词典只是增强，不是必需。
    """
    try:
        import os
        from . import config as C
        from . import newwords as NW
        p = NW.userdict_path(str(C.PROJECT_ROOT / "data"))
        if os.path.exists(p):
            # ⚠ 必须自己开关文件：jieba.load_userdict 用 open() 读完就扔了句柄，
            #   不关的话 Windows 上会一直占着这个文件（测试里直接报
            #   ResourceWarning: unclosed file），分发到别人机器上还可能
            #   变成「词典文件被占用」。
            with open(p, "r", encoding="utf-8") as f:
                j.load_userdict(f)
    except Exception:
        pass


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
    # T5：固定建制名白名单优先放行（中共中央/中国人民银行等尾巴撞上活动词表）
    if w in ORG_WHITELIST:
        return True
    # T7：活动/会议名整串或活动尾巴命中即否（白名单已在上面先放行）。
    if w in ORG_BANNED_TERMS or w.endswith(ORG_BANNED_TAILS):
        return False
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


def _established(w: str) -> bool:
    """像不像**真建制**：以行政/司法后缀结尾，且串里没有活动动词。"""
    if any(v in w for v in _ORG_EVENT_VERBS):
        return False
    return w.endswith(_ESTABLISHED_TAILS)


def eventish_org(name: str) -> bool:
    """
    **通用形态闸门**（T8）：这串是不是「活动 / 会议片段」的形态。

    True  = 活动/会议片段 → 精选模式应当滤掉
    False = 保留（真建制，或形态上判不出是活动名）

    为什么要有它（而不是继续加长 `ORG_BANNED_TERMS`）：
      上一轮点名封堵 7 个活动名之后，同族的**未点名**变体照样进榜 ——
      实测「协会召开第二届理事会」45、「第十一次全国代表大会」11，
      Top50 里还有「周年文艺晚会 / IPv6创新发展大会 / 全球数字经济大会 /
      第九届亚洲冬季运动会」。逐条拉黑是打地鼠：拉黑主名，同族变体立刻顶上。
      所以这里判**形态**，不判具体名字。

    判定顺序（保护优先，命中任一即 True）：

      ⓪ 精确白名单 `ORG_WHITELIST` → 直接 False（中共中央 / 中国人民银行 /
         全国人民代表大会 / 中国人民政治协商会议）。
      ① 建制词保护 → False：以 `_ESTABLISHED_TAILS` 结尾且无活动动词
         （人民政府 / 人大常委会 / 中央委员会 / 委员会 / 组委会 / 部 / 署 /
         厅 / 局 / 总局 / 央行 / 法院 / 检察院 / 中心 / 网信办…）。
      ② (a) `endswith(ORG_BANNED_TAILS)` → True
         （峰会/博览会/研讨会/交流会/周年大会/中全会/互联网大会/文明大会…）
      ③ (a′) `endswith(_EVENT_TAILS_PLAIN)` → True
         （活动形态尾缀里真机构不用的那批；⚠ 不含「理事会」，见常量注释）
      ④ (b) 含活动动词（召开/举行/举办/承办/出席/参加）
             且 `endswith(ORG_EVENT_TAILS)` → True
      ⑤ (c) 含「第…届 / 第…次」序数（阿拉伯数字与中文数字都认）
             且 `endswith(_ORG_ORDINAL_TAILS)` → True
      ⑥ (d) 含「的」且 `endswith(ORG_EVENT_TAILS)` → True（偏正残片）

    ⚠ 只判**形态**，不看上下文、不查抽取库 —— 这是「读取/统计展示」层的事，
      与抽取库里的数据无关（切档不重跑抽取）。
    ⚠ 已知边界（如实记）：判据是形态，不是语义。「安全治理新格局」这类
      **主题残片**（专家解读标题被切出来的）不含活动词、也不以活动后缀结尾，
      本闸门判不出（False），仍在精选榜上 —— 它属于「主题名」问题，
      不归形态闸门管。
    """
    w = (name or "").strip(_PUNCT)
    if not w:
        return False
    # ⓪ 精确全名白名单：真建制，任何形态规则都不得动它
    if w in ORG_WHITELIST:
        return False
    # ① 建制词保护
    if _established(w):
        return False
    # ② (a) 无歧义活动尾巴
    if w.endswith(ORG_BANNED_TAILS):
        return True
    # ③ (a′) 活动形态尾缀里真机构不用的那批
    if w.endswith(_EVENT_TAILS_PLAIN):
        return True
    # ④ (b) 活动动词 + 活动形态尾缀
    if any(v in w for v in _ORG_EVENT_VERBS) and w.endswith(ORG_EVENT_TAILS):
        return True
    # ⑤ (c) 序数 + 活动形态尾缀
    if _ORDINAL_RE.search(w) and w.endswith(_ORG_ORDINAL_TAILS):
        return True
    # ⑥ (d)「的」偏正残片 + 活动形态尾缀
    if "的" in w and w.endswith(ORG_EVENT_TAILS):
        return True
    return False


def _mine_orgs_split(titles: Iterable[str], min_count: int = 3,
                     min_len: int = 3,
                     topn: int = 200) -> Tuple[List[dict], List[dict]]:
    """
    粗选/精选**共用**的同一段机构候选挖掘循环。

    返回 `(all_rows, filtered)`：
      all_rows —— 过 `min_count` 的**全部**候选（次数降序、同次数长者在前），
                  **未按 topn 截断**，由调用方切片。**粗选口径就是它。**
      filtered —— all_rows 里被 `eventish_org` 判为活动/会议形态的那些，
                  顺序与 all_rows 一致（同一趟排序的投影）

    ⚠ 为什么必须是「一段循环、两个投影」：粗选与精选若各写一遍，
      同一份标题迟早给出两套不同基数的候选 —— 那时「切档只影响读取/统计
      展示、库数据不动」这条铁律就只剩一句口号，两档的数字也对不上了。
    ⚠ 形态只判一次（`evflag`），精选 = all_rows 减去 filtered，**同一个集合
      的补集**，不可能出现「滤掉的 + 剩下的 ≠ 全部」。

    ⚠ 粗选口径（all_rows）必须与历史行为**逐字一致**：本函数只把原先内联在
      `mine_orgs` 里的那段循环原样搬过来，`org_like` 前置条件、`min_count`
      位置、排序键都没动。实测校核见 `（测试）/tests_org_refine.py`
      与本轮交付说明里的 HEAD 对照（同标题集下候选逐项相同）。
    """
    j = _get()
    if j is None:
        return [], []
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
    all_rows: List[dict] = []
    ev_rows: dict = {}
    for w, c in counts.items():
        if c < min_count:
            continue
        row = {"org": w, "count": c, "example_title": surface[w]}
        all_rows.append(row)
        if eventish_org(w):
            ev_rows[w] = row
    all_rows.sort(key=lambda x: (-x["count"], -len(x["org"])))
    filtered = [ev_rows[r["org"]] for r in all_rows if r["org"] in ev_rows]
    return all_rows, filtered


def mine_orgs(titles: Iterable[str], min_count: int = 3,
              min_len: int = 3, topn: int = 200) -> List[dict]:
    """
    从标题里挖掘机构候选词 —— **粗选口径（默认，与历史行为逐字一致）**。

    这是**辅助**：它只给出「哪些串像机构名、出现多少次」，
    不保证正确。真正的判断（这是不是发文机关）仍然要人来做。

    做法：把每个标题切分成 n-gram（2~8 字），挑出以机构后缀结尾的，
    按出现次数排序 —— 高频 + 带机构后缀，是发文机关最典型的特征。

    ⚠ T8 起，粗选 = **不加通用形态闸门**（只走 `org_like` 粗筛，召回高）。
      「活动名挤占机构榜」是**精选档**才处理的事（见 `mine_orgs_refine`）——
      这里绝不能顺手把活动名滤掉：粗选是默认档，也是 MCP
      (`cacx_mine_orgs`) 走的那条路，行为必须与加档位之前完全一致。
    ⚠ 返回类型**不变**（List[dict]）—— `db.top_orgs` 与 MCP 都直接消费。
    """
    return _mine_orgs_split(titles, min_count=min_count,
                            min_len=min_len, topn=topn)[0][:topn]


def mine_orgs_refine(titles: Iterable[str], min_count: int = 3,
                     min_len: int = 3,
                     topn: int = 200) -> Tuple[List[dict], List[dict]]:
    """
    机构候选挖掘的**精选口径**：在 `org_like` 粗筛之后，再过一道
    `eventish_org` 通用形态闸门。

    返回 `(orgs, filtered)`：
      orgs     —— 与 `mine_orgs` **同一套候选结构**（未被闸门滤掉的那些）
      filtered —— 被闸门滤掉的候选 `[{org, count, example_title}]`，
                  与 orgs 同一套排序（出现次数降序、同次数长者在前），
                  供「精选模式缺省说明」用

    ⚠ 为什么另开一个函数而不是给 `mine_orgs` 加参数：`mine_orgs` 的返回值
      被 `db.top_orgs` 与 MCP 直接消费，**返回类型不能变**，且它的口径
      必须是**粗选**（默认档）。两档共用 `_mine_orgs_split` 那一段循环，
      所以 `orgs + filtered` 恰好等于粗选的全部候选（同一个集合的一刀两半）。
    ⚠ `org_like` 本身**不动**：粗选行为必须保持现状。
    """
    all_rows, filtered = _mine_orgs_split(titles, min_count=min_count,
                                          min_len=min_len, topn=topn)
    bad = {r["org"] for r in filtered}
    surv = [r for r in all_rows if r["org"] not in bad]
    return surv[:topn], filtered[:topn]


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