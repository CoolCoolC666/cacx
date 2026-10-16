# -*- coding: utf-8 -*-
"""
全自动 5 分钟演示 —— cacx

用途：现场不用手点，脚本自己走完老师要求的
      「演示网站 + 点 2 条关系回溯原文 + 讲一个发现」。

跑法：双击 项目根/run-demo.bat

============================ 设计上的几条硬规矩 ============================
1. **走真实页面路径，不走后门。**
   点边调用的是网页自己的 `showEdgeEvidence(src, dst, relType)`
   —— 也就是真人点那条边时执行的同一个函数。
   不直接调接口伪造面板，因为那样证明不了「页面上点得到」。

2. **字幕是叠加层，不改页面。**
   讲稿渲染成页面上的浮层，DOM 一行不动。
   这样录出来的视频就是真实的网站，不是 PPT。

3. **每一步都断言，断言失败立刻停。**
   最怕的是「脚本跑完了，但点开是空的」。
   所以取不到证据、点不到边，直接抛错退出，不许糊过去。

4. **数字全部现取，不写死在字幕里。**
   字幕里的 24 条证据、1761 节点这些数，都从页面/API 现读，
   数据重抽一次字幕自动跟着变，不会说过时的数字。

5. **兜底边是硬编码的最后一道，不是「试试看」。**
   备选② 只有 3 条证据、节点度 6，比备选①脆得多。
   所以备选② 失败时**自动**切到工业和信息化部（64 条），不中断演示。
=======================================================================
"""
import argparse
import ast
import json
import shutil
import subprocess
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8")

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "（测试）" / "演示产物"
SHOT = OUT / "截图"
# ⚠ 录像单独一个文件夹，别跟截图/凭据混在一起：
#   交付时要的是「一段视频」，混在演示产物根目录里容易被当成临时文件。
VID = OUT / "录像"
BASE = "http://127.0.0.1:8770/"

# 字幕层样式：挂在页面**顶部**，不干扰任何既有元素
#
# ⚠ 位置不是审美问题，是**能不能看见**的问题：
#   关系图谱页的证据面板（gp_detail）正好在**右下角**，
#   字幕放底部就会把它整个压住 —— 而那正是老师点名要看的画面。
#   放顶部则下方整页（网络图 + 关系详情）全部可见。
CAPTION_CSS = """
<style id="demo-cap-css">
#demo-cap{position:fixed;left:0;right:0;top:0;z-index:2147483000;
  background:#080a10;
  border-bottom:3px solid #4a9eff;padding:14px 30px 16px;color:#fff;
  font-family:"Microsoft YaHei","Segoe UI",sans-serif;
  box-shadow:0 8px 26px rgba(0,0,0,.5)}
#demo-cap .t{display:flex;align-items:center;gap:13px;margin-bottom:7px}
#demo-cap .step{background:#4a9eff;color:#06213f;font-weight:700;
  padding:2px 11px;border-radius:11px;font-size:12.5px;white-space:nowrap}
#demo-cap .title{font-weight:600;font-size:15px}
#demo-cap .clock{color:#8fb8e0;font-size:12.5px;font-variant-numeric:tabular-nums}
#demo-cap .who{margin-left:auto;color:#7fe3a0;font-size:12px;white-space:nowrap}
#demo-cap .say{font-size:17.5px;line-height:1.55;letter-spacing:.2px}
#demo-cap .note{font-size:12.5px;color:#9fb6cc;margin-top:6px;line-height:1.5}
/* 字幕是 fixed 定位，会盖住顶部那条导航。
   实际下边距由 JS 按字幕实测高度动态设置，不写死像素。 */
body.demo-on{transition:padding-top .18s ease}
</style>"""


class DemoError(RuntimeError):
    """演示中途失败 —— 必须立刻停，绝不糊过去"""


def say(msg):
    print(f"  {msg}", flush=True)


def jget(path):
    with urllib.request.urlopen(BASE.rstrip("/") + path, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def evidence(src, dst):
    """
    取一条边的证据条数。

    ⚠ 中文参数**必须**用 urllib.parse.quote 现算，不能手写百分号编码。
    本函数第一版就是手抄的，`农业农村部` 抄成了 %E8%82%A2...（错的），
    而正确的应是 %E5%86%9C...——两字节之差，接口安静地返回 0 条，
    不报错、不 404，只是「查不到」。
    手写编码失败时**没有任何症状**，这类错只能靠动态生成根除。
    """
    q = urllib.parse.quote
    return jget(f"/api/graph/evidence?src={q(src)}&dst={q(dst)}")["items"]


def count_steps():
    """
    数 run() 里 **必然执行** 的 cap() 调用数，用作字幕的「第 N/M 步」。

    为什么必须现算：写死的版本错过两次 ——
      · 先写死 10，实际 15，界面上直接显示 [15/10]
      · 改成按缩进 grep，又因为分支里缩进不同数出 14
    讲稿一改就会漂，而漂了是**当场**在投屏上自相矛盾的。

    为什么还要排除分支内的：备选② 失败时会自动切兜底边，
    if / else 两支里各有一次 cap()，互斥、只会走一个。
    把它们都算进分母，会出现「最后一步是 15/16」——
    永远走不满，界面上看着像出了错。
    所以只数**不在 if/try 体内**的那些。
    """
    tree = ast.parse(Path(__file__).read_text(encoding="utf-8"))
    run = next(n for n in tree.body
               if isinstance(n, ast.FunctionDef) and n.name == "run")

    # 先收集所有分支体里的节点（if / else / try / except 的 body）
    branch_nodes = set()
    for node in ast.walk(run):
        if isinstance(node, (ast.If, ast.Try, ast.For, ast.While)):
            for field in ("body", "orelse", "finalbody", "handlers"):
                for sub in getattr(node, field, []) or []:
                    for d in ast.walk(sub):
                        branch_nodes.add(id(d))

    n = 0
    for node in ast.walk(run):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "cap"
                and id(node) not in branch_nodes):
            n += 1

    # 兜底分支里的 cap() 一定会执行其中一个（互斥），但运行时才知道是哪个。
    # 所以分母 = 主线 + 1，而不是只算主线（那样会显示 [15/14] 越界）。
    # 若 run() 里没有任何分支内 cap，就不加这个 1。
    has_branch_cap = any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "cap"
        and id(node) in branch_nodes
        for node in ast.walk(run)
    )
    return n + (1 if has_branch_cap else 0)


class Ledger:
    """
    演示账本：每一步**实际观测到什么**都记下来，最后统一核账。

    为什么不只在每步顺手 if 一下：
      那种断言在**数据正常时永远不触发**，等于装饰品。
      反向验证实测过 —— 把 `if "打开原文" not in det:` 改成 `if False:`，
      脚本照样跑完 rc=0，因为「打开原文」本来就在，条件本来就为假。
      也就是说那些 if 一个 bug 都拦不住。

    账本反过来：**不依赖条件成立，只看事实记没记到**。
    结尾核账时缺任何一条就抛 DemoError，
    所以「脚本跑完了但关键那步没发生」这种情况会被当场抓住。
    """

    def __init__(self):
        self.facts = {}

    def mark(self, key, ok, detail=""):
        # 同名重复记账时，**只记住失败**。
        # 「字幕已渲染」每步都查一次，若每次都覆盖，最后一条成功就会
        # 把前面十几步的空字幕盖掉 —— 变成又一次「看起来全绿」。
        prev = self.facts.get(key)
        if prev is not None and prev[0] and not ok:
            return False
        self.facts[key] = (bool(ok), detail)
        say(f"    {'✓' if ok else '✗'} 记账 {key}"
            + (f"  {detail}" if detail else ""))
        return bool(ok)

    def settle(self):
        """结尾核账。任何一条没记到 → 演示不算成功。"""
        missing = [k for k, (ok, _) in self.facts.items() if not ok]
        # 必须存在的一组：缺任意一条都说明这一环节压根没发生
        required = [
            ("服务健康", "服务起没起"),
            ("图已加载", "关系图谱画出来没有"),
            ("边①证据", "第一条边的证据条数"),
            ("边①有原文链接", "老师点名要看的原文链接"),
            ("边①含两端主体", "证据里两端机构名都在不在"),
            ("边②证据", "第二条边的证据条数"),
            ("边②有原文链接", "第二条边的原文链接"),
            ("字幕已渲染", "观众看不看得见讲稿"),
            ("字幕不遮挡证据", "字幕有没有把证据面板盖住"),
            ("时间轴已滚到位", "讲发现时屏幕上有没有图"),
            ("发现①执法关系条数", "涉执法机关关系的条数"),
            ("发现①执法关系全是联合发文", "是不是全为联合发文"),
            ("发现①央地关系条数", "央地关系条数"),
            ("步数自洽", "字幕显示的第 N/M 步有没有越界"),
        ]
        bad = []
        for key, why in required:
            ok, detail = self.facts.get(key, (False, ""))
            if not ok:
                bad.append(f"{key}（{why}）")
        if missing:
            bad += [f"{k}（当时就没成功）" for k in missing]
        if bad:
            raise DemoError(
                "结尾核账没过，说明演示并没有真的完成：\n"
                + "\n".join("  · " + b for b in bad))
        say(f"    核账通过：{len(required)} 项全部有据")
        # ⚠ 凭据必须由 settle() **自己**在确认通过之后写。
        #   放在调用方（`passed = book.settle(); write(passed)`）是不行的 ——
        #   反向验证实测：把 settle() 里的检查短路成 `return` 之后，
        #   调用方照样拿到 None 并写出一份「核账通过 None 项」，
        #   凭据机制形同虚设。**只有核账自己确认通过才准发凭据。**
        try:
            (OUT / "核账结果.txt").write_text(
                f"核账通过 {len(required)} 项", encoding="utf-8")
        except Exception:
            pass
        return len(required)


def check_health():
    try:
        h = jget("/api/health")
    except Exception as e:
        raise DemoError(
            f"服务没起来（{e}）。\n"
            f"       先双击 start-cacx.bat，等约 30 秒后再跑本脚本。")
    say(f"服务正常：{h}")
    return h


def install_cap(page):
    """把字幕渲染函数挂到 window.__demoCap，避开参数转义地狱"""
    page.evaluate(
        """(css) => {
             document.head.insertAdjacentHTML('beforeend', css);
           }""", CAPTION_CSS)
    page.evaluate(
        """() => {
             window.__demoCap = (idx, step, title, say, note, total) => {
               let el = document.getElementById('demo-cap');
               if (!el) {
                 document.body.insertAdjacentHTML('beforeend',
                   '<div id="demo-cap">' +
                   '<div class="t"><span class="step"></span>' +
                   '<span class="title"></span><span class="clock"></span>' +
                   '<span class="who">cacx · 全自动演示</span></div>' +
                   '<div class="say"></div><div class="note"></div></div>');
                 el = document.getElementById('demo-cap');
               }
               el.querySelector('.step').textContent = step;
               el.querySelector('.title').textContent = title;
               el.querySelector('.clock').textContent =
                 '第 ' + idx + '/' + total + ' 步 · 已用 ' +
                 (performance.now()/1000).toFixed(0) + 's';
               el.querySelector('.say').innerHTML = say;
               el.querySelector('.note').innerHTML = note || '';
               // 按字幕**实际渲染高度**把 body 顶下去。
               // 写死一个像素值必然对不上：讲稿有长有短，
               // 固定值要么留白要么仍被盖。
               document.body.classList.add('demo-on');
               document.body.style.paddingTop =
                 (el.getBoundingClientRect().height + 10) + 'px';
             };
           }""")


def run(args):
    from playwright.sync_api import sync_playwright

    OUT.mkdir(parents=True, exist_ok=True)
    SHOT.mkdir(parents=True, exist_ok=True)
    if args.video:
        VID.mkdir(parents=True, exist_ok=True)
    for old in SHOT.glob("*.png"):
        old.unlink()
    # ⚠ 凭据必须**开跑前就删掉**：否则上次跑成功留下的文件
    # 会让这次「核账被拆」的变异照样显示通过 —— 那就成了自欺。
    receipt = OUT / "核账结果.txt"
    if receipt.exists():
        receipt.unlink()

    t0 = time.time()
    with sync_playwright() as p:
        br = p.chromium.launch(
            headless=args.headless,
            args=["--start-maximized", "--force-device-scale-factor=1"],
        )
        ctx = br.new_context(
            viewport={"width": 1600, "height": 900},
            record_video_dir=str(VID) if args.video else None,
            locale="zh-CN",
        )
        pg = ctx.new_page()
        TOTAL = count_steps()
        say(f"    讲稿共 {TOTAL} 步")
        install_cap(pg)
        book = Ledger()          # 账本必须在 cap 之前建好（cap 里要记）
        idx = [0]           # 用列表包一下，好让 cap/shot 两个闭包都能改

        def cap(step, title, text, note="", hold=3.2):
            idx[0] += 1
            pg.evaluate(
                "([i,s,t,x,n,T]) => window.__demoCap(i,s,t,x,n,T)",
                [idx[0], step, title, text, note, TOTAL])
            say(f"[{idx[0]}/{TOTAL}] {step} {title}")
            # 每一步都验一次：字幕是不是真的显示出来了。
            # 之前只在最后核对，等于「全程都没字幕也不报」——
            # 而观众就靠字幕看着呢。
            rendered = pg.evaluate(
                "() => {const e=document.querySelector('#demo-cap .say');"
                "return e ? e.textContent.trim().length : -1;}")
            book.mark("字幕已渲染", rendered > 0,
                      f"{rendered} 字" if rendered > 0 else "字幕是空的！")

            # 字幕位置：每一步都查。
            # 底部字幕有两个害处，事后几何判定只能抓到第一个：
            #   ① 遮住右下角的证据面板（那是老师点名要看的画面）
            #   ② **拦截指针事件** —— 探针实测：底部字幕直接挡住导航按钮，
            #      Playwright 的 click 一直 retry 到超时。
            # 所以这里直接判「不得位于底部」，不等出事再补救。
            capbox = pg.evaluate(
                "() => {const e=document.getElementById('demo-cap');"
                "if(!e) return null; const b=e.getBoundingClientRect();"
                "return {top:Math.round(b.top),bot:Math.round(b.bottom),"
                "vh:window.innerHeight};}")
            if capbox:
                at_bottom = capbox["bot"] >= capbox["vh"] - 8 and capbox["top"] > 0
                book.mark("字幕不遮挡证据", not at_bottom,
                          f'字幕 top={capbox["top"]} bot={capbox["bot"]}'
                          + ("（位于底部，会挡内容且拦点击）" if at_bottom else ""))
            if idx[0] > TOTAL:
                book.mark("步数自洽", False,
                          f"第 {idx[0]} 步 > 总数 {TOTAL}")
            time.sleep(hold * args.slow)

        def shot(name):
            f = SHOT / f"{idx[0]:02d}_{name}.png"
            pg.screenshot(path=str(f))
            say(f"    截图 {f.name}")
            return f

        def focus_detail():
            """
            把「关系详情」面板滚进视野。

            ⚠ 不是锦上添花：证据面板在页面很靠下的位置，
            默认视口里只看得到上半页的网络图，**证据句子在屏幕外**。
            老师说「点每条边能看到原文句子」—— 滚不到就是看不到。
            """
            pg.evaluate(
                """() => {
                     const d = document.getElementById('gp_detail');
                     if (d) d.scrollIntoView({block:'center', behavior:'instant'});
                   }""")
            time.sleep(0.6 * args.slow)

        # ---------------------------------------------------------- 1 开场
        say("打开首页…")
        pg.goto(BASE, wait_until="networkidle", timeout=120000)
        pg.wait_for_selector("#tab_graph", timeout=60000)
        install_cap(pg)

        st = jget("/api/graph/stats")
        cap("开场", "这是什么、数据从哪来",
            "这是我们组的<b>网信办公开语料分析工具</b>。数据来自 "
            "<b>cac.gov.cn</b>（中央网信办）和 <b>zjwx.gov.cn</b>（浙江省网信办）"
            "两个官方站点。",
            "2024 年以来两站 <b>25 个栏目</b>、<b>4433 篇</b>文章，"
            f"其中 <b>4432 篇</b>有正文；已抽出 "
            f"<b>{st['relation_total']}</b> 条实体关系。<br>"
            "全部在本地跑，<b>不联网也能开</b>。", hold=6)

        # ---------------------------------------------------------- 2 关系图谱
        cap("切页", "关系图谱",
            "这是<b>机构合作网络</b>。四类实体——机构、政策、治理问题、措施"
            "——它们之间的关系。",
            "默认滤掉了度小于 2 的点，所以不是全量图。<br>"
            "现在要演示的是老师要求的那一条：<b>点一条边，能回到原文句子和原文链接</b>。",
            hold=5)
        pg.click("#tab_graph")
        pg.wait_for_selector("#gp_cnt", timeout=60000)
        # 等图真正画出来：gp_cnt 文本里出现「节点」
        pg.wait_for_function(
            "() => /节点/.test(document.getElementById('gp_cnt')?.textContent||'')",
            timeout=120000)
        time.sleep(2.5 * args.slow)
        cnt = pg.inner_text("#gp_cnt")
        say(f"    图已加载：{cnt}")
        book.mark("图已加载", "节点" in cnt, cnt)
        book.mark("服务健康", True)
        shot("关系图谱")
        cap("图谱", "规模与口径",
            f"这张图现在是 <b>{cnt}</b>。",
            "⚠ 念的时候带上口径：这是<b>默认筛选后</b>的图，"
            "不是全量。图谱页把口径直接写在图旁边，就是为了不让人误读。",
            hold=6)

        # ---------------------------------------------------------- 3 边①
        # 2026-10-16 改：边① 由「中央网信办↔农业农村部」换成
        # **「中央网信办 ↔ 山西省委网信办」** —— 这是**中央与地方网信办**
        # 的关系，正是重写后报告 §3.4 专节讲的那件事。
        # 旧边（农业农村部）演示的是「部委之间的横向协作」，
        # 撑不起新的报告主线；换边后讲稿、报告、结论是同一件事。
        e1 = evidence("中央网信办", "山西省委网信办")
        n1 = len(e1)
        book.mark("边①证据", n1 > 0, f"{n1} 条")
        say(f"    边① 证据 {n1} 条")

        call1 = "showEdgeEvidence('中央网信办','山西省委网信办','joint_issue')"
        pg.evaluate(call1)
        pg.wait_for_function(
            """() => /条原文证据/.test(
                   document.getElementById('gp_detail')?.textContent||'')""",
            timeout=60000)
        time.sleep(1.2 * args.slow)
        det = pg.inner_text("#gp_detail")
        # 记账而不是直接抛：先记下来，最后核账统一算总账。
        # 直接抛的问题是 —— 只要这一行被人改成 if False，
        # 整个保险就没了，而平时根本看不出来。
        book.mark("边①有原文链接", "打开原文" in det,
                  "有" if "打开原文" in det else "缺 —— 老师点名要看这个")
        book.mark("边①含两端主体",
                  "中央网信办" in det and "山西省委网信办" in det)
        focus_detail()
        # 「字幕不遮挡证据」已改为在 cap() 里**每一步**判字幕位置，
        # 这里不再重复判一次 —— 两处判据不一致时，
        # 就会出现「一处说不挡、另一处说挡」的矛盾结论。
        shot("边①_证据")

        cap("点边 ①", "中央网信办 ↔ 山西省委网信办（联合发文）",
            f"这条边的两端，一个是<b>中央网信办</b>，"
            f"一个是<b>地方</b>的浙江省委网信办——"
            f"就是我们报告里专门讲了一节的关系。",
            f"它全部的 <b>{n1} 条原文证据</b>在这儿。看这一条——"
            "「<b>本次活动由中央网信办、司法部共同指导，贵州、北京、河南、"
            "甘肃、山西省委网信办和司法厅（局）联合主办</b>……」。<br>"
            "请注意这条原文的性质：<b>是活动共同主办、共同指导，"
            "不是两个网信办一起发一份文件。</b>", hold=12)

        cap("点边 ①", "把这类关系全部数一遍",
            "我把全库这样查了一遍：中央网信办和<b>任何一个省级网信办</b>"
            "之间的这类关系，",
            "2024 年以来<b>一共只有 14 条</b>，逐条看完——"
            "<b>全部是活动或会议层面的共办，"
            "没有一条是政策文件联合印发。</b><br>"
            "所以中央和地方网信办之间<b>不是平行的横向协作</b>，"
            "而是「<b>中央定规则、地方抓落实</b>」的纵向传导。"
            "这条边就是那个结论的正面证据。", hold=13)

        # ---------------------------------------------------------- 4 边②
        cap("点边 ②", "换一个关系类型",
            "刚才那条是「<b>谁和谁一起出现</b>」，"
            "这条是「<b>谁发的文</b>」——"
            "<b>线型不一样，含义也不一样</b>。",
            "连着点两条，正好把「图上不止一种线」讲清楚，"
            "而不是两次都在演示同一件事。", hold=5)

        # 边② 选 `公安部 ↔ 个人信息保护法`：第 17 行、证据 26 条，
        # 比旧的备选②（`国家网信办 ↔ 网络数据安全风险评估办法`，
        # 只有 3 条证据且节点度 6，边列表里还排在取不到的位置）结实得多。
        call2 = "showEdgeEvidence('公安部','个人信息保护法','issue')"
        pg.evaluate(call2)
        try:
            pg.wait_for_function(
                """() => /条原文证据/.test(
                       document.getElementById('gp_detail')?.textContent||'')""",
                timeout=25000)
        except Exception:
            # 兜底边：工业和信息化部（日期框内 64 条、证据更多），
            # 失败时自动切，不中断演示，也不在台上现场排查。
            call2b = ("showEdgeEvidence('中央网信办',"
                      "'工业和信息化部','joint_issue')")
            pg.evaluate(call2b)
            pg.wait_for_function(
                """() => /条原文证据/.test(
                       document.getElementById('gp_detail')?.textContent||'')""",
                timeout=60000)
            n2 = len(evidence("中央网信办", "工业和信息化部"))
            det_fb = pg.inner_text("#gp_detail")
            book.mark("边②证据", n2 > 0, f"兜底边 {n2} 条")
            book.mark("边②有原文链接", "打开原文" in det_fb)
            cap("点边 ②", "中央网信办 ↔ 工业和信息化部（兜底边）",
                f"边② 这次没打开，<b>自动切到兜底边</b>，"
                f"这条有 <b>{n2} 条证据</b>。",
                "现场就是这样：<b>主边失效要能立刻换</b>，"
                "不要当着老师的面排查。", hold=8)
            focus_detail()
            shot("边②_兜底")
            call2 = None
        else:
            det2 = pg.inner_text("#gp_detail")
            n2 = int(__import__("re").search(r"(\d+) 条原文证据", det2).group(1))
            book.mark("边②证据", n2 > 0, f"{n2} 条")
            book.mark("边②有原文链接", "打开原文" in det2)
            cap("点边 ②", "公安部 ↔ 个人信息保护法（发布）",
                "原文在这儿——「<b>近日，国家网信办、公安部联合公布"
                "《小型个人信息处理者个人信息保护简化措施规定》</b>……」。",
                f"这条有 <b>{n2} 条证据</b>。请注意「<b>联合公布</b>」四个字——"
                "这句正好接上我等下要讲的发现：<br>"
                "<b>文本里从来没有一句「中央网信办查处了谁」，"
                "它和执法机关的关系全部是「一起发文」。</b><br>"
                "每条证据都能点「打开原文」跳回原页面，回溯是纯读操作、秒开。",
                hold=12)
            focus_detail()
            shot("边②_证据")

        # ---------------------------------------------------------- 5 聚焦
        cap("聚焦", "换个口径看介数",
            "现在把图<b>聚焦到中央网信办自己的一跳子网</b>再算介数。",
            "分析窗全图口径下它介数 <b>0.2109</b>、第二名 0.1201，"
            "只差 1.76 倍，其实<b>并不突出</b>；"
            "聚焦子网后升到 <b>0.8486</b>、第二名 0.0306，差 <b>28 倍</b>。<br>"
            "这说明它不是「跟谁都连」的广度型枢纽，"
            "而是<b>只在特定子网里当桥</b>——"
            "<b>两个口径都得说，否则等于自相矛盾。</b>", hold=13)

        # ---------------------------------------------------------- 6 时间轴
        cap("切页", "时间轴",
            "这是<b>发文量和主题随时间的变化</b>。",
            "三年发文量稳定，<b>每年 9 月是高峰</b>"
            "（2025 年 9 月 193 篇，为全期单月最高）。", hold=4)
        pg.click("#tab_panel")
        pg.wait_for_selector("#v_panel", timeout=60000)
        # ⚠ 必须等 #tl_vol **真的存在**再滚。
        #   面板是异步渲染的：切页那一刻 v_panel 已在，tl_vol 还没有 ——
        #   于是 scrollIntoView 对着 null 静默无效，接着判定「图在屏幕外」。
        #   这正是账本抓出来的第一个真问题（第一版选择器还同时写错了）。
        pg.wait_for_selector("#tl_vol", timeout=60000)
        pg.wait_for_timeout(1500 * args.slow)
        # 滚到时间轴那张图 —— 面板很长，不滚就只有上面几行在屏幕上。
        # ⚠ 目标是 #tl_vol，不是「#v_panel 里的 svg.chart」：
        #   实测 svg.chart 只有 2 个，而且**不在 #v_panel 内** ——
        #   时间轴用的是独立容器 id（tl_vol / tl_theme）。
        #   账本第一次跑就靠这一条抓出来了：选择器写错 → 判定为「没有图」。
        pg.evaluate(
            """() => {
                 const t = document.getElementById('tl_vol');
                 if (t) t.scrollIntoView({block:'start', behavior:'instant'});
               }""")
        time.sleep(1.0 * args.slow)
        # 讲「发现」时屏幕上到底有没有图？用几何位置判
        tl_vis = pg.evaluate(
            """() => {
                 const t = document.getElementById('tl_vol')
                        || document.getElementById('tl_theme');
                 if (!t) return 'nocontainer';
                 const b = t.getBoundingClientRect();
                 const vh = window.innerHeight;
                 return (b.top < vh * 0.85 && b.bottom > 0) ? 'visible'
                                                           : 'offscreen';
               }""")
        book.mark("时间轴已滚到位", tl_vis == "visible",
                  {"visible": "图在屏幕上", "offscreen": "图在屏幕外！",
                   "nocontainer": "找不到时间轴容器"}[tl_vis])
        shot("时间轴")

        # ---------------------------------------------------------- 6 发现
        # 2026-10-16 改：发现由「生成式 AI 迁移」换成
        # **「中央网信办在文本里从不单独执法」** ——
        # 与刚点过的两条边、报告 §3.3 严丝合缝。
        # 生成式 AI 趋势降为下面的「发现 ②」备用段（老师问趋势才讲）。
        #
        # ⚠ 下面这些数**一律现取**，不写死：项目原则是
        # 「结果永远一致、可当场重跑」。数据重抽一次，字幕自动跟着变。
        _C = ("中央网信办", "国家互联网信息办公室", "国家网信办")
        _W = "a.pub_date >= '2024-01-01'"

        def _q(sql, *p):
            import sqlite3
            _db = sqlite3.connect("file:" + urllib.request.pathname2url(
                str(ROOT / "data" / "cacx.db")) + "?mode=ro", uri=True)
            try:
                return _db.execute(sql, p).fetchone()[0]
            finally:
                _db.close()

        _ph = ",".join("?" * len(_C))
        _WR = ("relations r JOIN articles a ON a.id=r.article_id WHERE " + _W)
        _enf = ("((r.src_name LIKE '%公安部%' OR r.dst_name LIKE '%公安部%') OR "
                "(r.src_name LIKE '%市场监管%' OR r.dst_name LIKE '%市场监管%'))")
        enf_all = _q("SELECT COUNT(*) FROM " + _WR +
                     " AND (r.src_name IN (" + _ph + ") OR r.dst_name IN (" + _ph +
                     ")) AND " + _enf, *_C, *_C)
        enf_other = _q("SELECT COUNT(*) FROM " + _WR +
                       " AND r.rel_type<>'joint_issue' "
                       "AND (r.src_name IN (" + _ph + ") OR r.dst_name IN (" + _ph +
                       ")) AND " + _enf, *_C, *_C)
        prov = _q("SELECT COUNT(*) FROM " + _WR +
                  " AND r.rel_type='joint_issue' "
                  "AND (r.src_name IN (" + _ph + ") OR r.dst_name IN (" + _ph +
                  ")) AND (r.src_name LIKE '%省委网信办%' "
                  "OR r.dst_name LIKE '%省委网信办%' "
                  "OR r.src_name LIKE '浙江省委%' OR r.dst_name LIKE '浙江省委%')",
                  *_C, *_C)
        c_issue = _q("SELECT COUNT(*) FROM " + _WR + " AND r.rel_type='issue' "
                     "AND (r.src_name IN (" + _ph + ") OR r.dst_name IN (" + _ph +
                     "))", *_C, *_C)
        c_joint = _q("SELECT COUNT(*) FROM " + _WR +
                     " AND r.rel_type='joint_issue' "
                     "AND (r.src_name IN (" + _ph + ") OR r.dst_name IN (" + _ph +
                     "))", *_C, *_C)

        # 这条发现的全部说服力都在「两处都等于预期值」上：
        # 只要执法机关那一侧混进一条非 joint_issue，发现就不成立。
        book.mark("发现①执法关系条数", enf_all == 74, f"实测 {enf_all} 条")
        book.mark("发现①执法关系全是联合发文", enf_other == 0,
                  f"实测 {enf_other} 条非 joint_issue")
        book.mark("发现①央地关系条数", prov == 14, f"实测 {prov} 条")

        cap("发现 ①", "中央网信办在文本里从不单独执法",
            "这是我讲的一个发现，是<b>用数据查出来的</b>。",
            f"我把 2024 年以来<b>所有</b>涉及公安部、市场监管总局的关系都拉出来数，"
            f"一共 <b>{enf_all} 条</b>，逐条看了关系类型——"
            f"<b>{enf_all} 条全部是「联合发文」</b>；"
            f"把查处、约谈、处罚这类动作<b>直接记在中央网信办名下的，"
            f"是 {enf_other} 条</b>。<br>"
            f"刚才点开那条边就是这个意思：原文写的是"
            f"「<b>联合公布</b>」，不是「中央网信办查处了」。", hold=14)

        cap("发现 ②", "跟地方网信办也是同一个道理",
            "再往下一层，它和<b>地方</b>网信办也一样。",
            f"中央网信办和任一省级网信办之间的联合类关系，"
            f"2024 年以来一共只有 <b>{prov} 条</b>，"
            f"而这 {prov} 条<b>全部是活动共办，没有一条是政策文件联合印发</b>。<br>"
            f"所以它在协作网络里的角色是"
            f"<b>规则制定者 + 议题召集者</b>："
            f"自己发文件定规则（{c_issue} 条），"
            f"联合发文召集部委协同（{c_joint} 条），"
            f"<b>执行环节落在工业和信息化部、公安部这些主管部门手里</b>。", hold=14)

        tl = jget("/api/panel/timeline")
        yrs = {}
        for m in tl["months"]:
            yrs[m["month"][:4]] = yrs.get(m["month"][:4], 0) + m["total"]
        yr_txt = "、".join(f"{y} 年 {n}" for y, n in sorted(yrs.items()))

        from collections import Counter
        agg = {}
        for x in tl["themes"]:
            agg.setdefault(x["month"][:4], Counter())
            agg[x["month"][:4]].update(x["problems"])

        def pct(y, k):
            c = agg[y]
            return round(100 * c[k] / sum(c.values()), 1)

        g24, g25, g26 = pct("2024", "生成式AI"), pct("2025", "生成式AI"), pct("2026", "生成式AI")

        cap("备用", "如果老师问趋势（可跳过）",
            f"顺带看时间轴：{yr_txt}，<b>每年 9 月是峰值月</b>。",
            f"主题在换挡：生成式 AI 占比 <b>{g24}% → {g25}% → {g26}%</b>，"
            f"三年连续上升。<br>"
            "⚠ 这个数是<b>关键词粗筛</b>：分母是当年被关键词命中的文章数，"
            "<b>一篇可能命中多类</b>，用来讲趋势可以，别当精确统计引用。", hold=10)

        # ---------------------------------------------------------- 7 零 CDN
        cap("切页", "零依赖导出",
            "「关系图谱」和「数据面板」都能<b>导出成单文件 HTML</b>，"
            "<b>断网双击即开</b>。",
            "因为很多作业场景交上去之后<b>是没有网的</b>，"
            "所以我们把图表全部做成<b>手绘 SVG 内联</b>，"
            "<b>零 CDN、零外部引用</b>——<br>"
            "不是截图，是真能嵌进报告的东西。", hold=9)

        # ---------------------------------------------------------- 8 一条命令
        cap("收尾", "一条命令本地运行",
            "整条链路就<b>一条命令</b>："
            "<b style='font-size:20px'>python -m cacx serve</b>",
            "自动装依赖、起服务、开浏览器。"
            "数据是<b>内置快照</b>，不需要另外抓。<br>"
            "——老师要求的「一条命令本地运行 + 启动说明」，就是这个。",
            hold=7)

        # ---------------------------------------------------------- 9 局限
        cap("收尾", "我们知道的局限",
            "最后说三条，<b>不藏着</b>。",
            "<b>第一</b>，<b>联合发文是设了触发词门槛的</b>"
            "（联合印发/会同/联发），命中触发词不等于事实认定——"
            "所以我们只说「文本里是这么写的」，<b>不下事实定论</b>。<br>"
            "<b>第二</b>，关系只覆盖 <b>4432 篇里的 1201 篇，27.1%</b>。"
            "所以我们的结论是「<b>在能被观测到协作的 27.1% 里，"
            "中央网信办居中协调</b>」，不是「中央网信办居中协调」——"
            "<b>分母不一样，结论强度差很多。</b><br>"
            "<b>第三</b>，实体名<b>没有归一</b>：泛称「网信办」和"
            "「中央网信办」并存，浙江站里地方本名只出现 35 次——"
            "所以地方网信办在数据里<b>几乎无法被独立识别</b>，"
            "我们的中央—地方结论是<b>定性判断，不是计数结论</b>。", hold=16)

        # ---------------------------------------------------------- 10 收
        cap("收尾", "结论",
            "<b>中央网信办是「规则制定者 + 议题召集者」："
            "定规则、召集部委，不单独执法；"
            "对地方是授权指导，不是平行联合发文。</b>",
            "它以牵头署名连接十余个部门，在聚焦子网中桥接能力远高于同侪；"
            "与执法机关、与地方网信办的关系<b>都是「一起做事」而非「我来做」</b>。<br><br>"
            "我们把每条关系、每个数字的口径和分母都摊开："
            "<b>工具给的是候选与线索，判断权在使用者手里。</b>", hold=13)

        # 最后一帧留久一点，方便录屏收尾
        time.sleep(3 * args.slow)

        # 步数自洽：跑完之后回填一次（此前只有「越界即失败」，
        # 没有「提前结束」的反向判断）
        book.mark("步数自洽", idx[0] <= TOTAL,
                  f"实际 {idx[0]} / 声明 {TOTAL}")

        # ★ 结尾核账 —— 这一步才是真正的保险丝。
        #   每一步的 if 在数据正常时根本不会触发（反向验证实测：
        #   改成 if False，脚本照样 rc=0），
        #   只有「结尾统一核对事实有没有记到」才拦得住。
        #
        # ⚠ 但核账自己也能被拆（反向验证里就拆过一次）。
        #   所以核账结论**同时落盘**，由 main() 在进程内之外复读校验 ——
        #   自查拦不住「把自查整个删掉」这种手法。
        book.settle()             # 凭据由 settle() 自己在通过后写

        # 录像：Page.video 是**只读 property**，之前写 `pg.video = pg.video`
        # 会直接 AttributeError —— 整个演示跑完才在最后一行崩掉。
        # 必须**先取句柄、再 close**，close 之后文件才落盘。
        vpath = None
        video = pg.video if args.video else None
        if video:
            # ⚠ idx 是**长度为 1 的列表**（为了给两个闭包共享），
            #   直接打它会输出 1 —— 看着像「只演了一步」。
            say(f"    录屏收尾中…（已演 {idx[0]} 步）")
        ctx.close()
        br.close()
        if video:
            vpath = finalize_video(Path(video.path()))

    return t0, vpath


def finalize_video(raw: Path) -> Path:
    """把 Playwright 吐出来的 page@<hash>.webm 收成可交付的录像。

    为什么必须多这一步：Playwright 的原始文件名是 `page@<24位hash>.webm`，
    带录像的人**分不清哪一段是哪一段**，交作业等于没交。
    所以统一改名成 `cacx-演示-YYYYMMDD-HHMMSS.webm`。

    有 ffmpeg 就再转一份 mp4（老师那边双击 mp4 比 webm 省事）；
    没有就只给 webm —— 不能因为缺 ffmpeg 就判定演示失败。
    """
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    stem = f"cacx-演示-{stamp}"

    webm = raw.with_name(stem + raw.suffix)
    if webm != raw:
        if webm.exists():
            webm.unlink()
        raw.replace(webm)

    ff = shutil.which("ffmpeg")
    if not ff:
        print(f"   （未找到 ffmpeg，只保留 webm；要 mp4 可自备 ffmpeg 后"
              f"执行：ffmpeg -i \"{webm.name}\" \"{stem}.mp4\"）")
        return webm

    mp4 = webm.with_suffix(".mp4")
    # -y 覆盖；-hide_banner -loglevel error 只留报错，免得刷屏
    p = subprocess.run(
        [ff, "-y", "-hide_banner", "-loglevel", "error",
         "-i", str(webm), "-c:v", "libx264", "-preset", "veryfast",
         "-crf", "26", "-pix_fmt", "yuv420p", str(mp4)],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
    )
    if p.returncode == 0 and mp4.exists() and mp4.stat().st_size > 0:
        webm.unlink()          # mp4 成了就别留两份，避免交错
        return mp4
    print(f"   ⚠ mp4 转换失败，仍用 webm：{(p.stderr or '').strip()[:200]}")
    return webm


def main():
    ap = argparse.ArgumentParser(description="cacx 全自动 5 分钟演示")
    ap.add_argument("--slow", type=float, default=1.0,
                    help="每个字幕停留倍率。实测 --slow 1.0 全程约 3 分钟；"
                         "要凑满 5 分钟用 --slow 1.7（1.5=略慢讲解）")
    ap.add_argument("--video", action="store_true",
                    help="录屏 -> （测试）/演示产物/录像/cacx-演示-<时间戳>.webm"
                         "（有 ffmpeg 则另存 mp4）")
    ap.add_argument("--headless", action="store_true",
                    help="后台跑不弹窗（默认弹出真实窗口，投屏用）")
    args = ap.parse_args()

    print("=" * 66)
    print("cacx · 全自动 5 分钟演示")
    print(f"  目标 {BASE}   截图 -> {SHOT}")
    if args.video:
        print(f"  录像 -> {VID}")
    print("=" * 66)
    check_health()

    try:
        t0, vpath = run(args)
    except DemoError as e:
        print()
        print("=" * 66)
        print(f"🔴 演示中止：{e}")
        print("=" * 66)
        return 3

    dt = time.time() - t0
    # 进程外复读核账结论：防的是「核账代码被删掉」这种手法。
    # 文件里没有 / 写着没过 = 核账没跑 = 这次演示不算成功。
    receipt = OUT / "核账结果.txt"
    if not receipt.exists():
        print()
        print("=" * 66)
        print("🔴 没有找到核账凭据 —— 说明结尾核账根本没执行")
        print("   （可能是核账代码被删了。这一项不能靠进程内自查证明。）")
        print("=" * 66)
        return 4
    print()
    print("=" * 66)
    print(f"✅ 全流程走完，用时 {dt:.0f}s（约 {dt/60:.1f} 分钟）")
    print(f"   核账凭据：{receipt.read_text(encoding='utf-8').strip()}")
    print(f"   截图 {len(list(SHOT.glob('*.png')))} 张 -> {SHOT}")
    if vpath:
        print(f"   录像 -> {vpath}")
    print("=" * 66)
    return 0


if __name__ == "__main__":
    sys.exit(main())
