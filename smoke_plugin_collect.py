"""端到端验证**插件自己的真实抓取路径**（main.HoyoCodesPlugin._collect）。

做法：用 urllib 真发请求，冒充 httpx.AsyncClient；其余（插件源码、提码规则、
时效窗口、交叉验证）全部走真代码。这样能在部署前就确认「重构后不再调 getPostFull」
也依然拿得到码。

    python smoke_plugin_collect.py        # 只读；三游戏共 6 次请求
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import sys
import types
import urllib.error
import urllib.parse
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
REQUEST_COUNT = 0


class _Resp:
    def __init__(self, status: int, text: str) -> None:
        self.status_code = status
        self._text = text

    def json(self):
        return json.loads(self._text)


class RealClient:
    """冒充 httpx.AsyncClient，实际用 urllib 发真请求。"""

    def __init__(self, *a, **kw) -> None:
        pass

    async def get(self, url, params=None, headers=None):
        global REQUEST_COUNT
        if params:
            url = url + ("&" if "?" in url else "?") + urllib.parse.urlencode(params)
        REQUEST_COUNT += 1
        req = urllib.request.Request(url, headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
                return _Resp(resp.status, resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return _Resp(exc.code, exc.read().decode("utf-8", "replace"))

    async def aclose(self) -> None:
        pass


# ── astrbot 桩（最小版）────────────────────────────────────────────────────
class _Filter:
    def command(self, *a, **k):
        return lambda fn: fn

    def on_astrbot_loaded(self, **k):
        return lambda fn: fn

    def regex(self, *a, **k):
        return lambda fn: fn

    def llm_tool(self, *a, **k):
        return lambda fn: fn


class _Star:
    def __init__(self, context=None, config=None) -> None:
        self.context = context
        self.logger = logging.getLogger("smoke")


def build_stub() -> None:
    mods = {}
    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = object
    event_mod.filter = _Filter()
    event_mod.MessageChain = lambda c=None: c
    comp = types.ModuleType("astrbot.api.message_components")
    comp.Plain = lambda t: t
    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    star_mod.Star = _Star
    star_mod.register = lambda *a, **k: (lambda cls: cls)
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot")
    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    mods.update({
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_mod,
        "astrbot.api.message_components": comp,
        "astrbot.api.star": star_mod,
    })
    hx = types.ModuleType("httpx")
    hx.AsyncClient = RealClient
    mods["httpx"] = hx
    sys.modules.update(mods)


build_stub()
spec = importlib.util.spec_from_file_location("hoyo_main_smoke", os.path.join(HERE, "main.py"))
main = importlib.util.module_from_spec(spec)
sys.modules["hoyo_main_smoke"] = main
spec.loader.exec_module(main)  # type: ignore[union-attr]


async def run() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    plugin = main.HoyoCodesPlugin(None, {
        # 动态跟着游戏表走：加了新游戏这个冒烟脚本自动覆盖，不用改
        "enabled_games": list(main.core.GAMES),
        "post_max_age_days": 7,
        "max_posts_per_game": 4,
        "min_cn_group": 2,
        "request_interval_seconds": 1.3,
    })
    print("=" * 66)
    for game in main.core.GAMES:
        try:
            hits, _ok, _live = await plugin._collect(game)
        except Exception as exc:  # noqa: BLE001
            print(f"{game}: ❌ {exc}")
            continue
        name = main.core.GAMES[game]["name"]
        if not hits:
            print(f"{game:<10} {name:<12} → （7 天内没有候选码）")
            continue
        print(f"{game:<10} {name:<12} →")
        for h in hits:
            print(f"    {h.code:<22} 来源数={h.confidence}  {h.titles[:1]}")
    await plugin.source.close()
    print("=" * 66)
    print(f"总请求数：{REQUEST_COUNT}（三游戏 2 关键词 × 3 = 6 为正常；多出来说明还在调详情接口）")


asyncio.run(run())
