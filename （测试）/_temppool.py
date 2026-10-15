# -*- coding: utf-8 -*-
"""测试用临时目录的统一出口 —— 根治 TEMP 无限累积。

## 为什么要有这层

之前测试各自 `tempfile.mkdtemp(prefix="cacx_xxx")`，攒出 1814 个目录 / 4.30 GB
（%TEMP% 里全是空壳 t.db，外加 4 份 807 MB 的真库副本）。两个独立的成因：

1. **有的根本没删**：`tests_sample.py` 每个用例 `setUp` 建一个目录（849 个用例），
   `tearDown` 只还原 `WA.DB`，目录留着。
2. **有的删了但静默失败**：`shutil.rmtree(..., ignore_errors=True)` 在 Windows 上
   遇到 sqlite 还开着的 `-wal` / `-shm` 句柄会直接失败，而 `ignore_errors=True`
   把失败吞掉 —— 于是「看起来清理过了」，实际一个都没删掉。

## 这个模块解决两件事

**① 落点改道到项目内**，不再吃 C 盘。默认根是 `（测试）/.tmp`（E 盘）。
好处不止是不占 C 盘：泄漏会**看得见** —— 一个 `dir` 命令就能看到有几个，
而不是散在一个 4.87 GB 的 %TEMP% 里看不见。

**② 进程退出时兜底回收**。`atexit` 时机比每个 `tearDown` 都晚 ——
此时 sqlite 连接早已成为垃圾，`gc.collect()` 一收，句柄就放了，
`rmtree` 这次真能删掉。这正是 per-test 删除做不到的那一半。

## 用法

    tmp = Path(_temppool.mkdtemp(prefix="cacx_sample_"))   # 建（登记在册）
    _temppool.cleanup(tmp)                                 # 删（立即；删不掉也别静默）
    # 忘了删也没关系：atexit 会兜底，并在 stderr 留 CACX_TEMP_LEAK 标记

## 不要做的事

- ❌ 直接用 `tempfile.mkdtemp` —— 绕过了登记，兜底就兜不到它
- ❌ 用 `shutil.rmtree(..., ignore_errors=True)` —— 见上文成因 2，那正是静默失败的来源
- ❌ 把根指向 `config/` 或 `data/` —— 测试库一律走临时根，绝不碰真库
"""
import atexit
import gc
import os
import shutil
import stat
import sys
import tempfile
import time
from pathlib import Path

# ---------------------------------------------------------------- 落点

#: 临时根。默认项目内 `（测试）/.tmp`；可用环境变量 CACX_TEST_TMP 改道
#: （比如想落到别的大盘时）。必须是绝对路径。
ROOT = Path(os.environ.get("CACX_TEST_TMP") or
            (Path(__file__).resolve().parent / ".tmp")).resolve()

#: 已登记、待回收的目录。atexit 按登记顺序倒着删（后建的先删，通常还热着）。
_REGISTRY = []


def _ensure_root():
    ROOT.mkdir(parents=True, exist_ok=True)
    return ROOT


# ---------------------------------------------------------------- 删除

# ⚠ rmtree 的错误回调参数名 3.12 换过：3.12+ 是 `onexc`（且传的是**异常实例**），
# 旧的 `onerror` 传的是 **exc_info 元组** —— 对它 `raise exc` 会直接
# TypeError: exceptions must derive from BaseException。
# 这条是反向验证第 [2] 条当场抓出来的，不是读文档看出来的。
# 两条路径拿到的都是异常实例，所以回调体可以共用。
_ERR_KW = "onexc" if sys.version_info >= (3, 12) else "onerror"


def _on_rm_error(func, path, exc):
    """rmtree 中途报错时的补救：清只读位再试一次。

    ⚠⚠ **补救失败必须把原异常抛回去**。
    rmtree 的语义是「错误回调不抛异常 ⇒ 整次删除算成功」。
    在这里吞掉异常，等于把「文件还在」谎报成「删干净了」——
    正是本模块要根治的那个毛病（原来的 `ignore_errors=True`）。
    抛回去，外层 `_rmtree_retry` 才会重试，调用方也才拿得到 False。
    """
    try:
        os.chmod(path, stat.S_IWRITE)
        func(path)
    except OSError:
        raise exc


def _rmtree_retry(path, attempts=6, delay=0.05):
    """删一个目录树，失败就重试。

    重试期间调 `gc.collect()` 是关键：sqlite 连接对象只要还被引用，
    Windows 上文件句柄就不释放，rmtree 必然失败；把垃圾收掉才有下一次机会。

    每一轮都以「目录是否真的不存在」为准，**不看 rmtree 的返回值** ——
    它在某些失败路径上会返回 None 却什么都没删（见 `_on_rm_error`）。

    返回 True 表示「确实已经不在磁盘上了」——不存在也算成功。
    """
    path = str(path)
    for i in range(attempts):
        if not os.path.exists(path):
            return True
        try:
            shutil.rmtree(path, **{_ERR_KW: _on_rm_error})
        except OSError:
            pass
        if not os.path.exists(path):
            return True
        gc.collect()          # 也许是被引用的 sqlite 连接挡着
        if i < attempts - 1:
            time.sleep(delay)
    return not os.path.exists(path)


def cleanup(path):
    """删掉一个由本模块建出的目录。返回是否真的删干净。

    刻意**不**提供 `ignore_errors` 式静默：调用方（`_regress.py` 的闸门）
    要能看见「以为删了其实没删」。
    """
    path = str(path)
    ok = _rmtree_retry(path)
    if ok and path in _REGISTRY:
        _REGISTRY.remove(path)
    return ok


# ---------------------------------------------------------------- 建

def mkdtemp(prefix="cacx_", suffix="", _dir=None):
    """等价于 `tempfile.mkdtemp(prefix=...)`，但落点在项目内且登记在册。

    前缀默认保留 `cacx_` —— 部分用例断言 `assertIn("cacx_sample_", db.path)`，
    靠的就是这个前缀，所以**不要**改调用点的 prefix 参数。
    """
    base = Path(_dir) if _dir else _ensure_root()
    base.mkdir(parents=True, exist_ok=True)
    d = Path(tempfile.mkdtemp(prefix=prefix, suffix=suffix, dir=str(base)))
    _REGISTRY.append(str(d))
    return str(d)


def tempdir(prefix="cacx_", suffix=""):
    """`mkdtemp` 的 `Path` 版。"""
    return Path(mkdtemp(prefix=prefix, suffix=suffix))


# ---------------------------------------------------------------- 兜底

def leaks():
    """列出登记过、但此刻还赖在磁盘上的目录。"""
    return [p for p in list(_REGISTRY) if os.path.exists(p)]


def sweep(verbose=False):
    """把临时根底下所有东西清掉（含本进程没登记到的遗留）。

    返回清理前存在的条目数。给 `_regress.py` 的闸门用。
    """
    if not ROOT.exists():
        return 0
    entries = list(ROOT.iterdir())
    for e in entries:
        if e.is_dir():
            _rmtree_retry(e)
        else:
            try:
                e.unlink()
            except OSError:
                pass
    left = list(ROOT.iterdir()) if ROOT.exists() else []
    if verbose and left:
        for p in left:
            print(f"  未清掉: {p}", file=sys.stderr)
    return len(entries)


def _atexit_sweep():
    """进程退出时兜底。

    到这里 sqlite 连接基本都成垃圾了，`gc.collect()` 之后通常能删干净。
    真删不掉的（例如测试自己起的后台线程还握着句柄），把名字打到 stderr，
    带一个固定标记，好让外层闸门 grep 得到。
    """
    if not _REGISTRY:
        return
    gc.collect()
    time.sleep(0.05)          # 给 Windows 一个释放句柄的间隙
    gc.collect()
    stuck = [p for p in list(_REGISTRY) if not _rmtree_retry(p)]
    if stuck:
        print(f"CACX_TEMP_LEAK {len(stuck)} 个临时目录未回收：", file=sys.stderr)
        for p in stuck:
            print(f"  {p}", file=sys.stderr)


atexit.register(_atexit_sweep)
