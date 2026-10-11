# -*- coding: utf-8 -*-
"""
列表条目分类：哪些是真文章，哪些压根不是
================================================================
为什么需要这个模块

实测一次 CAC 全量同步：列表 25828 条，报「正文抓不到」135 条，
耗时约 6.5 分钟，人工核实后的真相是：

    85 条  本站专题/栏目首页（2019zt/.../index.htm 这类聚合页）
    35 条  站外链接（25 个域名：scio.gov.cn、mp.weixin.qq.com、各省网信官网…）
     2 条  源站脏数据（空 URL、javascript:void(0);）
    13 条  本站文章但真失败（多是 2015/2019 年已下线的旧稿）

也就是说 **135 条里只有 13 条是真 bug**，其余 122 条被错误地
记成了「抓取失败」。这个误分类有三个具体代价：

    1. 同步日志里全是刺眼的失败行，真问题被淹没 —— 用户以为程序坏了
    2. 每条失败都要跑满 retry=3 次退避重试，122 条白烧约 3 分钟
    3. `sync_runs.failed` 字段失真，数据健康顾问据此判断「接口坏了」

所以正确的做法是：**在列表阶段就分流**，而不是等抓正文失败才反应。
分类器只回答「这条 URL 是不是文章」，不判断文章质量 ——
后者要等真抓到正文才知道。

分类规则是启发式的，所以每一类都带 `reason`，界面上要能看见，
让人知道「为什么这条被跳过」，而不是黑箱地少了 122 条。
"""

import re
from typing import Dict
from urllib.parse import urlparse

KIND_ARTICLE = "article"        # 正常文章，去抓正文
KIND_INDEX = "index_page"       # 专题/栏目/分页首页，不是文章
KIND_EXTERNAL = "external"      # 站外链接，域名不属于本站
KIND_WECHAT = "wechat"          # 微信公众号文章（站外但另有处理路径）
KIND_DIRTY = "dirty"            # 源站脏数据：空值、javascript:、#

SKIP_KINDS = (KIND_INDEX, KIND_EXTERNAL, KIND_WECHAT, KIND_DIRTY)

KIND_LABEL = {
    KIND_ARTICLE: "文章",
    KIND_INDEX: "专题/栏目首页",
    KIND_EXTERNAL: "站外链接",
    KIND_WECHAT: "微信公众号",
    KIND_DIRTY: "脏数据",
    # 历史数据里已有的旧取值（导入器写的），别显示成天书
    "EMPTY": "旧版·非本站",
    "FAIL": "旧版·抓取失败",
}

_INDEX_NAME = re.compile(r"^index(_?\d*)?\.(s?html?|shtml|jsp|asp|aspx)$", re.I)
# 纯数字文件名 = 栏目翻页页码（/ncwx/10.htm）。
# 必须限长：CAC 的真文章也有纯数字文件名，
#   /yaowen/gdtt/webinfo/2019/12/1578463627194187.htm 是 16 位数字的文章 id。
# 写成 \d+ 会把这类真文章判成页码 —— 误判方向是「静默丢掉真文章」，
# 比误报「正文抓不到」严重得多，所以宁可漏判也不能错判。
_NUM_PAGE = re.compile(r"^\d{1,3}\.(s?html?|shtml|jsp|asp|aspx)$", re.I)
_ART_ID = re.compile(r"^c_\d+\.s?html?$", re.I)
_BAD_SCHEME = re.compile(r"^(javascript:|mailto:|tel:|about:|#)", re.I)
_DOC_EXT = re.compile(r"\.(s?html?|shtml|jsp|asp|aspx)$", re.I)

# 「像文章 id」的特征：长数字串，或 c_/art_/id_ 前缀。
# 国内政务站的文章 URL 几乎必带唯一标识（c_1578810501771691.htm 这种），
# 没有标识的更像专题落地页。
_ARTICLE_ID_HINT = re.compile(r"\d{5,}|^(c|art|id|content|info)[_-]", re.I)
# 附件下载：正文永远不在 pdf/doc/xls 里
_ATTACHMENT = re.compile(r"\.(pdf|docx?|xlsx?|pptx?|zip|rar|7z|mp[34]|"
                         r"jpg|jpeg|png|gif|txt)(\?|$)", re.I)


def _host(u: str) -> str:
    try:
        h = (urlparse(u).netloc or "").lower()
    except Exception:
        return ""
    return h[4:] if h.startswith("www.") else h


def classify(url: str, base_url: str, topic_prefixes=()) -> tuple:
    """
    返回 (kind, reason)。reason 是**给人看的中文说明**，
    会一路带到界面与日志里 —— 「被跳过」必须可解释。

    topic_prefixes: 站点显式声明的专题目录前缀（如 CAC 的 2018zt / 2019zt），
    命中的直接判 index_page。启发式总有覆盖不到的情况，
    让站点能自己补，比不断往启发式里堆特例更可控。
    """
    u = (url or "").strip()
    if not u:
        return KIND_DIRTY, "源站列表里的链接为空"
    if _BAD_SCHEME.match(u):
        return KIND_DIRTY, f"伪链接（{u[:30]}）"

    try:
        p = urlparse(u)
    except Exception:
        return KIND_DIRTY, "URL 格式无法解析"
    if p.scheme not in ("http", "https"):
        return KIND_DIRTY, f"非 http(s) 协议（{p.scheme}）"

    host, base_host = _host(u), _host(base_url)

    # 微信单列：它是站外链接，但有独立的抓取策略与失败原因，
    # 混在「站外」里会让统计失真，也没法针对性优化。
    if host == "mp.weixin.qq.com":
        return KIND_WECHAT, "微信公众号文章（有反爬，需专门处理）"
    if base_host and host and host != base_host:
        return KIND_EXTERNAL, f"域名不属于本站（{host}）"

    path = (p.path or "/").rstrip("/")
    segs = [s for s in path.split("/") if s]
    name = segs[-1] if segs else ""

    if not segs:
        return KIND_INDEX, "站点根路径，不是文章"
    if _ATTACHMENT.search(p.path):
        return KIND_INDEX, "附件下载链接，不是文章页"
    for pref in (topic_prefixes or ()):
        if pref and segs[0].lower() == str(pref).lower():
            return KIND_INDEX, f"站点声明的专题目录（{pref}）"
    if _INDEX_NAME.match(name):
        return KIND_INDEX, f"目录索引页（{name}）"
    if _NUM_PAGE.match(name) and len(segs) <= 2:
        return KIND_INDEX, f"栏目翻页页码（{name}）"
    if _ART_ID.match(name):
        return KIND_ARTICLE, "符合文章 URL 形态"
    # 单层路径 + 文档扩展名：多是一站一专题的落地页（/2015fzdm.htm）
    if len(segs) == 1 and _DOC_EXT.search(name):
        return KIND_INDEX, f"单层专题页（{name}）"
    # 两段路径但文件名没有文章标识：典型如 /2018zt/wxgzhyaqpz.htm。
    # 真文章几乎必带唯一 id（c_1578810501771691.htm），没有的多是专题子页。
    if len(segs) == 2 and _DOC_EXT.search(name) and \
            not _ARTICLE_ID_HINT.search(name):
        return KIND_INDEX, f"两段路径且文件名无文章标识（{name}）"
    return KIND_ARTICLE, "符合文章 URL 形态"


def summarize(kinds) -> Dict[str, int]:
    """把一批 kind 汇总成 {kind: 条数}，给同步摘要用"""
    out: Dict[str, int] = {}
    for k in kinds:
        out[k] = out.get(k, 0) + 1
    return out


def describe_line(counts: Dict[str, int]) -> str:
    """一行人类可读的分类摘要"""
    if not counts:
        return "无"
    parts = [f"{KIND_LABEL.get(k, k)} {v}" for k, v in counts.items()]
    return "，".join(parts)
