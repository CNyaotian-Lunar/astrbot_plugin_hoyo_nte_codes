"""容器内真机验收：直接走「命令路径」跑一遍 /兑换码。

为什么要有它：真机抓到 `/兑换码` 返回「查询失败：too many values to unpack」——
调用点没跟着 `_collect` 的签名改。QQ 群里发命令是**最后一公里**，所以这里在**容器内、真网络**下
把命令路径（`_query`）完整跑一遍，看它到底输出什么。

安全边界：
  · `STATE_FILE` 被指到系统临时目录 ⇒ **绝不碰线上状态**；
  · 只调 `_query`（只读抓取），不推送、不记账；
  · 请求数 = 3 游戏 × (2 关键词 + 直播间 1) + 原神 miyolive(3 段) ≈ 13 次，间隔 1.2s。

用法（AstrBot 跑在 Docker 里时，在宿主机上）：
    docker exec <容器名> python <AstrBot>/data/tmp_verify/verify_query.py
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

# ⚠️ 别碰线上状态文件
P.STATE_FILE = os.path.join(tempfile.gettempdir(), "verify_state.json")

CONF = {
    # 动态跟着游戏表走 —— 以后加游戏，这个验收脚本自动覆盖到，不用改
    "enabled_games": list(C.GAMES),
    "post_max_age_days": 7,
    "max_posts_per_game": 4,
    "min_cn_group": 2,
    "request_interval_seconds": 1.2,
    "watch_authors_per_game": 0,     # 验收只测主源，别顺带盯人
    "enable_miyolive": True,
    "bili_live_rooms": dict(P.DEFAULTS.get("bili_live_rooms") or {}),
}


async def run() -> int:
    plugin = P.HoyoCodesPlugin(None, dict(CONF))
    rc = 0
    for game, label in (("genshin", "/兑换码 原神"), (None, "/兑换码（全部）")):
        print("=" * 66)
        print(f"$ {label}")
        try:
            text = await plugin._query([game] if game else None)
        except Exception as exc:  # noqa: BLE001
            print(f"❌ 抛异常：{type(exc).__name__}: {exc}")
            rc = 1
            continue
        print(text)
        if "查询失败" in text:
            print("❌ 输出里出现「查询失败」⇒ 命令路径坏了")
            rc = 1
        elif game == "genshin" and ("兑换码" not in text):
            print("⚠️ 输出里没有「兑换码」字样，人工看一眼")
    print("=" * 66)
    try:
        await plugin.source.close()
    except Exception:  # noqa: BLE001
        pass
    print(f"总请求数：{P.REQUEST_COUNT if hasattr(P, 'REQUEST_COUNT') else '（未统计）'}")
    print("结论：" + ("✅ 命令路径 OK" if rc == 0 else "❌ 命令路径有问题"))
    return rc


sys.exit(asyncio.run(run()))
