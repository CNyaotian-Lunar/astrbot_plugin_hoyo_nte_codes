"""容器内真机验收：跑一遍 **`/兑换码` 命令路径**（含合并转发的构造）。

设计形态：查询结果不要一整段纯文本，而要**合并转发卡片**、
**每个兑换码独占一条消息**（长按单条只复制那个码）。

这里在容器里造一个假 event，把 `cmd_codes` 真跑一遍，把它**实际产出**的东西打印出来
（`chain_result` 的分支会不会真的构造出 Node 列表、每条消息是什么）。

安全边界：`STATE_FILE` 指到系统临时目录，**不推送、不记账、不碰线上状态**；只读抓取。

用法（AstrBot 跑在 Docker 里时，在宿主机上）：
    docker exec <容器名> python <AstrBot>/data/tmp_verify/verify_cmd.py
"""

from __future__ import annotations

import asyncio
import os
import pathlib
import sys
import tempfile

# 插件目录：优先用环境变量 `HOYO_PLUGIN_DIR`，其次取本脚本所在目录（把本脚本放进插件目录直接跑）。
# 两者都不是插件目录时给出明确报错，而不是让 import 静默失败。
PLUGIN_DIR = os.environ.get("HOYO_PLUGIN_DIR") or str(pathlib.Path(__file__).resolve().parent)
if not os.path.isfile(os.path.join(PLUGIN_DIR, "hoyo_core.py")):
    print(
        f"找不到插件目录：{PLUGIN_DIR} 下没有 hoyo_core.py。"
        "请把本脚本放进插件目录，或用环境变量 HOYO_PLUGIN_DIR=<插件目录> 指定。"
    )
    sys.exit(2)
sys.path.insert(0, PLUGIN_DIR)

import main as P  # noqa: E402
import hoyo_core as C  # noqa: E402

P.STATE_FILE = os.path.join(tempfile.gettempdir(), "verify_cmd_state.json")


class FakeEvent:
    """只实现命令路径用到的那两个方法。"""

    def __init__(self, umo: str = "aiocqhttp:GroupMessage:999999") -> None:
        self.unified_msg_origin = umo

    def plain_result(self, text: str):
        print("  ▶ 走的是【降级·纯文本】：")
        for line in str(text).splitlines():
            print(f"     | {line}")
        return text

    def chain_result(self, chain):
        nodes = []
        for comp in chain:
            nodes = list(getattr(comp, "nodes", None) or [])
        print(f"  ▶ 走的是【合并转发】：{len(chain)} 个组件、{len(nodes)} 条消息")
        for i, n in enumerate(nodes, 1):
            txt = "".join(getattr(c, "text", "") or "" for c in (getattr(n, "content", None) or []))
            first = txt.splitlines()[0] if txt.splitlines() else ""
            print(f"     [{i}] {first!r}" + ("" if len(txt.splitlines()) <= 1 else f"  （另 {len(txt.splitlines()) - 1} 行）"))
        return chain


async def run() -> int:
    conf = {
        "enabled_games": ["genshin"],
        "post_max_age_days": 7,
        "max_posts_per_game": 4,
        "min_cn_group": 2,
        "request_interval_seconds": 1.2,
        "watch_authors_per_game": 0,
        "enable_miyolive": True,
        "notify_live": False,
        "bili_live_rooms": dict(P.DEFAULTS.get("bili_live_rooms") or {}),
    }
    plugin = P.HoyoCodesPlugin(None, dict(conf))
    print("=" * 66)
    print("$ /兑换码 原神")
    got_any = False
    try:
        async for _ in plugin.cmd_codes(FakeEvent(), "原神"):
            got_any = True
    except Exception as exc:  # noqa: BLE001
        print(f"  ❌ 命令抛异常：{type(exc).__name__}: {exc}")
        return 1
    print(f"  （产出 {'有' if got_any else '无'}）")
    print("=" * 66)
    print("$ /兑换码 不存在游戏")
    async for _ in plugin.cmd_codes(FakeEvent(), "塞尔达"):
        got_any = True
    print("=" * 66)
    try:
        await plugin.source.close()
        if plugin._nte:
            await plugin._nte.close()
    except Exception:  # noqa: BLE001
        pass
    print("结论：" + ("✅ 命令路径能跑通（看上面走的是合并转发还是降级）" if got_any else "❌ 命令没有任何产出"))
    return 0 if got_any else 1


sys.exit(asyncio.run(run()))
