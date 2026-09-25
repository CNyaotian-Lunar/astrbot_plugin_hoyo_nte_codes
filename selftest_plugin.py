"""main.py 的桩自测：本机没有 AstrBot，用桩模块把它加载起来，验证核心流程。

重点验证：
  1. 首轮只建基线、不推送
  2. 出现新码 → 推送一次并记账
  3. 同样候选再来 → 不重复推
  4. 推送失败 → 不记账 → 下轮重试（不会静默丢码）
  5. 没有订阅者 → 不推送但记账（视为已处理）
  6. 命令/钩子注册齐全

    python selftest_plugin.py
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
import logging
import os
import shutil
import sys
import tempfile
import time
import types

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

HERE = os.path.dirname(os.path.abspath(__file__))
PASS = 0
FAIL = 0
FAILED: list[str] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ✅ {name}")
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  ❌ {name}  {detail}")


# ── 造 astrbot 桩模块 ───────────────────────────────────────────────────────
class _Filter:
    def __init__(self) -> None:
        self.commands: dict[str, tuple] = {}
        self.loaded_hooks: list = []
        self.regexes: list = []
        self.llm_tools: list = []

    def command(self, name, alias=None, **kw):
        def deco(fn):
            self.commands[name] = (fn, alias)
            return fn
        return deco

    def on_astrbot_loaded(self, **kw):
        def deco(fn):
            self.loaded_hooks.append(fn)
            return fn
        return deco

    def regex(self, pattern, **kw):
        def deco(fn):
            self.regexes.append((pattern, fn))
            return fn
        return deco

    def llm_tool(self, name, **kw):
        def deco(fn):
            self.llm_tools.append((name, fn))
            return fn
        return deco

    def event_message_type(self, *a, **k):
        def deco(fn):
            return fn
        return deco


FILTER = _Filter()


class _FakeEvent:
    def __init__(self, umo: str = "aiocqhttp:GroupMessage:12345", message: str = "") -> None:
        self.unified_msg_origin = umo
        self.message_str = message  # 命令参数靠它捞全部 token（框架只传第一个）
        self.replies: list[str] = []
        self.chains: list = []

    def plain_result(self, text: str):
        self.replies.append(text)
        return text

    def chain_result(self, chain):
        self.chains.append(chain)
        return chain


class _Star:
    def __init__(self, context=None, config=None) -> None:
        self.context = context
        self.logger = logging.getLogger("plugin-test")


def _build_stub() -> None:
    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api.logger = logging.getLogger("astrbot")

    event_mod = types.ModuleType("astrbot.api.event")
    event_mod.AstrMessageEvent = _FakeEvent
    event_mod.filter = FILTER
    event_mod.MessageChain = lambda components=None: {"components": components or []}

    components_mod = types.ModuleType("astrbot.api.message_components")
    components_mod.Plain = lambda text: {"type": "plain", "text": text}
    # 前瞻提醒改成「直播间封面大图 + 文字链接」后新增（2026-09-16）
    components_mod.Image = lambda file=None, **_: {"type": "image", "file": file}

    class _Node:  # 合并转发卡片里的一条消息
        def __init__(self, uin="0", name="", content=None) -> None:
            self.uin = uin
            self.name = name
            self.content = content or []

    class _Nodes:  # 整个合并转发
        def __init__(self, nodes=None) -> None:
            self.nodes = nodes or []

    components_mod.Node = _Node
    components_mod.Nodes = _Nodes

    star_mod = types.ModuleType("astrbot.api.star")
    star_mod.Context = object
    star_mod.Star = _Star
    star_mod.register = lambda *a, **k: (lambda cls: cls)

    for name, mod in {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.event": event_mod,
        "astrbot.api.message_components": components_mod,
        "astrbot.api.star": star_mod,
    }.items():
        sys.modules[name] = mod

    astrbot.api = api

    # 本机没装 httpx（AstrBot 容器里才有）—— 打个桩，测试中被 FakeSource 替换掉
    if "httpx" not in sys.modules:
        try:
            import httpx  # noqa: F401
        except ImportError:
            httpx_stub = types.ModuleType("httpx")

            class _StubAsyncClient:
                def __init__(self, *a, **kw) -> None:
                    pass

                async def get(self, *a, **kw):
                    raise RuntimeError("httpx stub：测试不应发起真实请求")

                async def aclose(self) -> None:
                    pass

            httpx_stub.AsyncClient = _StubAsyncClient
            sys.modules["httpx"] = httpx_stub


_build_stub()
spec = importlib.util.spec_from_file_location("hoyo_plugin_main", os.path.join(HERE, "main.py"))
main = importlib.util.module_from_spec(spec)
sys.modules["hoyo_plugin_main"] = main
spec.loader.exec_module(main)  # type: ignore[union-attr]


# ── 假数据源 ────────────────────────────────────────────────────────────────
class FakeSource:
    """按「本轮该返回什么」可编排的假米游社。"""

    def __init__(self, payload: dict[str, list[tuple[str, str]]], live_meta=None,
                 live_rooms=None, probe_posts=None, by_act=None) -> None:
        # payload: {game_key: [(subject, text), ...]}
        self.payload = payload
        self.live_meta = live_meta
        self.live_rooms = live_rooms or {}
        # 前瞻当晚的 act_id 探测：`search("act_id")` 返回这批帖子；命中后按 act_id 取码
        self.probe_posts = list(probe_posts or [])
        self.by_act = dict(by_act or {})
        self.probe_calls = 0  # 只数「前瞻当晚探测」这一件事，别和正常抓取混在一起
        self.closed = False
        self.calls = 0
        self.order_types: list[int | None] = []  # 记录用过的排序，供「双排序」测试断言
        # 模拟「上游改版把 game_id 去掉了」：用于验证归属过滤的降级路径
        self.strip_game_id = False

    async def search(self, keyword: str, size: int = 10, *, order_type: int | None = None):
        self.calls += 1
        self.order_types.append(order_type)
        if keyword == "act_id":  # 前瞻当晚探测走这个关键词
            return list(self.probe_posts)
        out = []
        for game, posts in self.payload.items():
            # ⭐ **真机同构**：真实搜索响应的每条帖都带 `post.game_id`（米游社的权威归属字段），
            # 生产代码靠它做跨游戏硬过滤。夹具必须同样带上 —— 否则会掩盖归属过滤的回归
            # （本项目已有过"夹具不同构导致假绿"的教训）。
            gids = main.core.GAMES.get(game, {}).get("gids")
            for idx, item in enumerate(posts):
                # 支持 `(subject, text)` 与 `(subject, text, age_days)` 两种写法 ——
                # 后者用于构造「几天前的老帖」（有效期不得压早的回压场景需要）。
                subject, text = item[0], item[1]
                age_days = float(item[2]) if len(item) > 2 else 0.0
                out.append(
                    {
                        "post_id": f"{game}-{idx}",
                        "subject": subject,
                        "created_at": time.time() - age_days * 86400,
                        "text": text,  # 真实接口的搜索响应就带正文
                        "uid": f"uid-{game}-{idx}",  # 每帖不同作者 ⇒ 码能进 A 级
                        "game_id": None if self.strip_game_id else gids,
                    }
                )
        return out

    async def post_text(self, post_id: str):
        game, idx = post_id.rsplit("-", 1)
        subject, text = self.payload[game][int(idx)]
        return {
            "post_id": post_id,
            "subject": subject,
            "text": text,
            "author_uid": "280612327",
            "created_at": time.time(),
        }

    async def close(self) -> None:
        self.closed = True

    async def user_posts(self, uid: str, size: int = 10):
        """备源 1（盯人）—— 测试里返回空。"""
        self.calls += 1
        return []

    async def miyolive_codes(self, uid: int):
        """备源 2（官方）—— 返回编排好的直播信息（默认没有）。"""
        self.calls += 1
        return [], None, self.live_meta

    async def miyolive_codes_by_act(self, act_id: str):
        """已知 act_id 时取码（前瞻当晚探测命中后走这条）。"""
        self.calls += 1
        return list(self.by_act.get(act_id, [])), None, None

    async def probe_act_id(self, game: str) -> str:
        """桩 —— **复用真实现的判据**（`MiyousheSource.match_act_id`），不另抄一份。"""
        self.calls += 1
        self.probe_calls += 1
        biz = str(main.core.GAMES.get(game, {}).get("game_biz") or "")
        return main.MiyousheSource.match_act_id(
            [str(p.get("text") or "") for p in self.probe_posts], biz)

    async def live_room_status(self, room_id: str) -> dict:
        """B站直播间状态 —— 默认查不到；测试要「正在直播」时传 live_rooms。"""
        self.calls += 1
        return dict(self.live_rooms.get(str(room_id), {}))


class FakeContext:
    def __init__(self, fail: bool = False) -> None:
        self.sent: list[tuple[str, str]] = []
        self.forward_nodes: list = []  # 最近一次「合并转发」的 Node 列表
        self.fail = fail

    async def send_message(self, session, chain) -> None:
        if self.fail:
            raise RuntimeError("平台不可用（模拟）")
        parts: list[str] = []
        for comp in chain.get("components") or []:
            if hasattr(comp, "nodes"):  # Nodes（合并转发）
                self.forward_nodes = list(comp.nodes)
                parts.append(f"[合并转发 {len(comp.nodes)} 条]")
            elif isinstance(comp, dict):
                parts.append(str(comp.get("text", "")))
        self.sent.append((session, "".join(parts)))


TMP = tempfile.mkdtemp(prefix="hoyo_selftest_")
main.STATE_FILE = os.path.join(TMP, "state.json")

CONF = {
    "enabled_games": ["zzz"],
    "check_interval_minutes": 60,
    "startup_delay_seconds": 5,
    "request_interval_seconds": 0.2,
    "post_max_age_days": 30,
    "max_posts_per_game": 4,
    "retention_days": 180,
}


def new_plugin(payload, *, fail_push=False, subscribers=None, live_meta=None, live_rooms=None,
               probe_posts=None, by_act=None):
    ctx = FakeContext(fail=fail_push)
    plugin = main.HoyoCodesPlugin(ctx, dict(CONF))
    plugin.source = FakeSource(payload, live_meta=live_meta, live_rooms=live_rooms,
                               probe_posts=probe_posts, by_act=by_act)
    plugin.context = ctx
    if subscribers is not None:
        plugin._state = plugin._empty_state()
        plugin._state["subscribers"] = list(subscribers)
    return plugin, ctx


async def main_async() -> None:
    print("=== [1] 模块加载与注册 ===")
    check("插件类可实例化", hasattr(main, "HoyoCodesPlugin"))
    check("命令已注册（兑换码/订阅/退订/状态/自检）",
          {"兑换码", "订阅兑换码", "退订兑换码", "兑换码状态", "兑换码自检"} <= set(FILTER.commands),
          str(sorted(FILTER.commands)))
    check("已注册启动钩子", len(FILTER.loaded_hooks) == 1, str(len(FILTER.loaded_hooks)))
    check("有 initialize()（热重载后靠它重建后台任务）", asyncio.iscoroutinefunction(main.HoyoCodesPlugin.initialize))
    check("有 terminate()（取消后台任务）", asyncio.iscoroutinefunction(main.HoyoCodesPlugin.terminate))
    check("没有定义 __del__（否则 terminate 永不被调用）", "__del__" not in main.HoyoCodesPlugin.__dict__)
    check("没有注册 regex 自动触发（设计要求：显式触发）", FILTER.regexes == [], str(FILTER.regexes))
    check("没有注册 LLM 工具（避免模型自行调用）", FILTER.llm_tools == [], str(FILTER.llm_tools))

    print("\n=== [2] 首轮：只建基线，不推送 ===")
    payload = {"zzz": [
        ("绝区零3.2前瞻兑换码", "CLARET0909\n上半卡池：克拉蕾"),
        ("【绝区零3.2】前瞻兑换码", "兑换码\nCLARET0909"),
    ]}
    plugin, ctx = new_plugin(payload, subscribers=["aiocqhttp:GroupMessage:999"])
    r1 = await plugin.check_once()
    check("首轮标记 baseline=True", r1["baseline"] is True)
    check("首轮不推送", ctx.sent == [], str(ctx.sent))
    check("首轮已记账", len(plugin._state["seen"].get("zzz", {})) == 1, json.dumps(plugin._state["seen"], ensure_ascii=False))

    print("\n=== [3] 出现新码 → 推送一次并记账 ===")
    payload["zzz"] += [
        ("绝区零3.3前瞻兑换码", "兑换码\nZZZNEW032\n有效期至明日"),
        ("【绝区零3.3】前瞻兑换码", "兑换码\nZZZNEW032"),
    ]
    r2 = await plugin.check_once()
    check("检出 1 个新码", len(r2["new"]) == 1 and r2["new"][0].code == "ZZZNEW032", str(r2["new"]))
    check("推送了一次", len(ctx.sent) == 1, str(ctx.sent))
    check("推送内容含新码", any("ZZZNEW032" in str(n.content) for n in ctx.forward_nodes), str(ctx.sent))
    check("推送目标是订阅会话", ctx.sent[0][0] == "aiocqhttp:GroupMessage:999")
    check("新码已记账", "zzznew032" in plugin._state["seen"]["zzz"])

    print("\n=== [3b] 合并转发推送：每个码独占一条消息（方便长按复制）===")
    check("确实走了合并转发卡片", bool(ctx.forward_nodes), str(ctx.sent))
    _texts = [
        "".join(str(c.get("text", "")) for c in n.content if isinstance(c, dict))
        for n in ctx.forward_nodes
    ]
    check("条数 = 标题 + 码 + 来源", len(_texts) == 3, str(_texts))
    check("兑换码独占一条消息", _texts[1] == "ZZZNEW032", str(_texts))
    check("标题含「前瞻兑换码」", "前瞻兑换码" in _texts[0], _texts[0])
    check("末尾是来源", _texts[-1].startswith("来源："), _texts[-1])
    check("Node 带上了发送者信息", all(n.uin for n in ctx.forward_nodes), str([n.uin for n in ctx.forward_nodes]))

    print("\n=== [3c] 前瞻直播提醒（只发一条，含直播间）===")
    import datetime as _dtm

    _now = _dtm.datetime.now(main.core.TZ).replace(second=0, microsecond=0)
    _soon = _now + _dtm.timedelta(minutes=30)
    _live_payload = {"zzz": [
        ("绝区零3.3版本前瞻特别节目预告",
         f"{_soon:%m月%d日 %H:%M} 前瞻特别节目开播\n直播间：https://live.bilibili.com/32805602"),
    ]}
    plugin_live, ctx_live = new_plugin(
        _live_payload,
        subscribers=["aiocqhttp:GroupMessage:999"],
        live_meta={"title": "绝区零3.3前瞻", "start": _soon},
    )
    await plugin_live.check_once()
    _live_sent = [t for _u, t in ctx_live.sent if "前瞻直播提醒" in t]
    check("推送了前瞻直播提醒", len(_live_sent) == 1, str(ctx_live.sent))
    check("提醒里含 B站直播间链接", any("live.bilibili.com" in t for t in _live_sent), str(_live_sent))
    check("提醒写明「分钟后开播」", any("分钟后开播" in t for t in _live_sent), str(_live_sent))
    await plugin_live.check_once()
    check("同一个前瞻只提醒一次",
          sum(1 for _u, t in ctx_live.sent if "前瞻直播提醒" in t) == 1, str(ctx_live.sent))
    check("提醒已记账（state.live_seen）", bool(plugin_live._state.get("live_seen")), str(plugin_live._state.get("live_seen")))

    # ⚠️ 2026-09-16 实测发现的坑：星铁/绝区零**没有精确开播时间**（只能拿常设直播间的标题当信号），
    # 而那个标题会**滞后一整个版本**（星铁 4.5 早已上线，标题仍是「4.5版本前瞻特别节目」）。
    # 修复前：start=None 就跳过全部时间过滤 ⇒ 插件一启用就会误报一条"过期"的前瞻提醒。
    plugin_notlive, ctx_notlive = new_plugin(
        {"zzz": []}, subscribers=["aiocqhttp:GroupMessage:999"],
        live_meta={"title": "绝区零3.2版本前瞻特别节目", "start": None},
        live_rooms={"32805602": {"title": "绝区零3.2版本前瞻特别节目", "living": False}},
    )
    await plugin_notlive.check_once()
    check("【回归】只有滞后的标题、并未开播 ⇒ 不误报前瞻提醒",
          not [t for _u, t in ctx_notlive.sent if "前瞻直播提醒" in t], str(ctx_notlive.sent))

    plugin_living, ctx_living = new_plugin(
        {"zzz": []}, subscribers=["aiocqhttp:GroupMessage:999"],
        live_meta={"title": "绝区零3.3版本前瞻特别节目", "start": None},
        live_rooms={"32805602": {"title": "绝区零3.3版本前瞻特别节目", "living": True}},
    )
    await plugin_living.check_once()
    check("【回归】没有开播时间但「正在直播」⇒ 照样提醒",
          len([t for _u, t in ctx_living.sent if "前瞻直播提醒" in t]) == 1, str(ctx_living.sent))

    print("\n=== [3d] 跨游戏归属过滤 + 双排序（2026-09-20 星铁 4.6 前瞻事故回归）===")
    # 事故原貌：2026-09-20 星铁 4.6 前瞻当晚，兑换码 19:48 就出现在米游社了，插件整晚一个都没抓到。
    # 两个根因（都已实测取证）：
    #   ① 默认**相关度排序**把新帖挤出前 N 名 —— size=10 时"7 天内的帖 = 0 条"，加到 30 也只 1 条；
    #   ② 改成**时间倒序**（order_type=2）能拿到新帖了，但米游社搜索是**分词宽松匹配**，
    #      时间序会把"今天所有含『兑换码』的帖"全倒出来：实测用「原神兑换码」搜出的 20 条
    #      **全是星铁的**（当晚星铁前瞻还公布了《绝区零》4.8 联动，所以连"正文里有没有游戏名"
    #      这种文本判据都被击穿）⇒ 若不设闸，原神/绝区零订阅群会收到星铁的码。
    # 根治：搜索响应里每条帖自带 `post.game_id`（平台权威归属），抓取侧按它硬过滤。
    _mix = {
        "starrail": [
            ("星穹铁道4.6前瞻兑换码", "兑换码\nKXHN8W7FGB6U\n绝区零4.8联动开启"),
            ("【星铁4.6】前瞻兑换码", "兑换码\nKXHN8W7FGB6U"),
        ],
        "zzz": [
            ("绝区零3.2前瞻兑换码", "兑换码\nZZZAAA111\nZZZBBB222"),
            ("【绝区零3.2】前瞻兑换码", "兑换码\nZZZAAA111\nZZZBBB222"),
        ],
    }
    _p_mix, _ = new_plugin(_mix)
    _p_mix.conf["enabled_games"] = ["zzz"]
    await _p_mix.check_once()
    _seen_mix = _p_mix._state["seen"].get("zzz", {})
    check("归属过滤：绝区零的账本里**不含**星铁的码（即使星铁帖正文写着「绝区零」）",
          "kxhn8w7fgb6u" not in _seen_mix, json.dumps(sorted(_seen_mix), ensure_ascii=False))
    check("归属过滤：本游戏（绝区零）自己的码照常识别",
          "zzzaaa111" in _seen_mix, json.dumps(sorted(_seen_mix), ensure_ascii=False))
    check("双排序：时间倒序(order_type=2)与相关度序**都跑过**（前者保新鲜、后者保召回）",
          {2, None} <= set(_p_mix.source.order_types), str(_p_mix.source.order_types))

    # 降级路径：上游若把 game_id 字段去掉，必须「跳过过滤继续跑」而不是整条链路失效
    # （宁可偶发串味，也不要静默抓不到 —— 后者正是这次事故最难查的地方）。
    _p_nogid, _ = new_plugin(_mix)
    _p_nogid.conf["enabled_games"] = ["zzz"]
    _p_nogid.source.strip_game_id = True
    await _p_nogid.check_once()
    _seen_nogid = _p_nogid._state["seen"].get("zzz", {})
    check("降级：缺 game_id 时跳过过滤（不崩、不静默失效）",
          "kxhn8w7fgb6u" in _seen_nogid, json.dumps(sorted(_seen_nogid), ensure_ascii=False))

    print("\n=== [3e] 存量账本刷新（create=False 的集成路径）===")
    # 2026-09-20：修好「有效期写在标题里也能提到」之后，**存量账本不会自动变** ——
    # 星铁 4.6 那 3 个码是旧逻辑记的（expire 为空），查询里会一直显示「有效期未知」。
    # 这条断言盯的就是 check_once 会不会**顺手刷新**它们（并且不新增条目）。
    _p_stale, _p_stale_ctx = new_plugin({"zzz": [
        ("绝区零3.2前瞻兑换码", "兑换码\nZZZOLD999\n截止时间明天晚上23:59"),
        ("【绝区零3.2】前瞻兑换码", "兑换码\nZZZOLD999"),
    ]})
    _p_stale.conf["enabled_games"] = ["zzz"]
    _p_stale._state = _p_stale._empty_state()
    _p_stale._state["baseline_games"] = ["zzz"]
    _stale_entry = {
        "code": "ZZZOLD999", "game": "zzz", "tier": "A",
        "first_seen": "2026-09-01 00:00:00", "last_seen": "2026-09-01 00:00:00",
        "uids": ["old"], "posts": ["old"], "expire_at": "",
    }
    _p_stale._state["seen"] = {"zzz": {"zzzold999": dict(_stale_entry)},
                               "_global": {"zzzold999": dict(_stale_entry)}}
    await _p_stale.check_once()
    _ent = _p_stale._state["seen"]["zzz"]["zzzold999"]
    check("存量码的有效期被刷新（否则修好提取逻辑也是白修）",
          bool(_ent.get("expire_at")), repr(_ent.get("expire_at")))
    check("刷新**不新增**条目（账本条目集合不变）",
          set(_p_stale._state["seen"]["zzz"]) == {"zzzold999"},
          str(sorted(_p_stale._state["seen"]["zzz"])))
    check("刷新走的不是「推送」路径（这个码早已记账，不该再推一次）",
          not [t for _u, t in _p_stale_ctx.sent if "ZZZOLD999" in t], str(_p_stale_ctx.sent))

    print("\n=== [3f] 有效期补充：写有效期的帖被 max_posts 挤出也要能补上 ===")
    # 2026-09-20 实测场景：星铁 4.6 那 3 个码，写有效期的那条帖排在第 5 位（max_posts=4），
    # 被 pick 掉了 ⇒ expire 一直是空。修法：额外从「含有效期线索」的候选帖里补，
    # 但**只补有效期、绝不引入新码**（否则等于绕开 max_posts 的噪音控制）。
    _p_exp, _ = new_plugin({"zzz": [
        ("绝区零3.3前瞻兑换码 ①", "兑换码\nLATEINFO01"),
        ("绝区零3.3前瞻兑换码 ②", "兑换码\nLATEINFO01"),
        ("绝区零3.3前瞻兑换码 ③", "兑换码\nLATEINFO01"),
        ("绝区零3.3前瞻兑换码 ④", "兑换码\nLATEINFO01"),
        ("绝区零3.3前瞻兑换码 截止时间明天晚上23:59", "兑换码\nLATEINFO01"),
        # 同样含有效期线索、但讲的是**另一个码** —— 它的码必须被挡在门外
        ("绝区零3.3前瞻兑换码 另一个 截止时间明天晚上23:59", "兑换码\nNOTINTENDED\n截止时间明天晚上23:59"),
    ]})
    _p_exp.conf["enabled_games"] = ["zzz"]
    await _p_exp.check_once()
    _e2 = _p_exp._state["seen"].get("zzz", {})
    check("被挤出 picked 的那条帖，有效期仍然补进来了",
          bool((_e2.get("lateinfo01") or {}).get("expire_at")),
          repr((_e2.get("lateinfo01") or {}).get("expire_at")))
    check("补充路径**不引入新码**（只认已经找到的码）",
          "notintended" not in _e2, str(sorted(_e2)))

    print("\n=== [3g] 有效期补充**不得压早**（回归：一条老帖能让真码永久漏推）===")
    # 机理：`_extra` 会从"没进 picked 的候选帖"里补有效期。一条 **3 天前**的老帖写着
    # 「截止时间明天晚上23:59」——按**发帖时间**解析出来是**过去**；若拿它去填真码的有效期
    # ⇒ 判"已过期" ⇒ 不推 ⇒ 不入账 ⇒ 每轮重判为新、每轮又被过滤 = **永久漏推**（且账本被污染）。
    _p_bd, _ = new_plugin({"zzz": [
        ("绝区零3.3前瞻兑换码 ①", "兑换码\nBACKDATE99"),
        ("绝区零3.3前瞻兑换码 ②", "兑换码\nBACKDATE99"),
        ("绝区零3.3前瞻兑换码 ③", "兑换码\nBACKDATE99"),
        ("绝区零3.3前瞻兑换码 ④", "兑换码\nBACKDATE99"),
        # 第 5 帖（3 天前发的）—— 被 max_posts 挤出 picked，只会进"有效期补充"那条路。
        # 它说的"明天23:59"是**它发帖时**的明天，早已过去 ⇒ 必须被忽略。
        ("绝区零3.3前瞻兑换码（3 天前的老帖）", "兑换码\nBACKDATE99\n截止时间明天晚上23:59", 3.0),
    ]})
    _p_bd.conf["enabled_games"] = ["zzz"]
    await _p_bd.check_once()
    _e3 = _p_bd._state["seen"].get("zzz", {})
    check("老帖的『过去的有效期』不该把真码压成过期（否则永久漏推）",
          "backdate99" in _e3, str(sorted(_e3)))
    check("该码的有效期保持为空，而不是被老帖填成过去",
          not (_e3.get("backdate99") or {}).get("expire_at"),
          repr((_e3.get("backdate99") or {}).get("expire_at")))

    print("\n=== [3h] 社区**单作者**（B 档）一律不推送（硬闸）===")
    # 背景：2026-09-21 01:35:43 群里真的收到了两条**说明句**被当码推送（B 档、单作者）：
    # `冬季与绝区零进行冬季联动` / `正常抽需要古老月华来兑换抽卡卷`。
    # 需求：「社区来源的必须 ≥2 个」⇒ 推送侧加硬闸 `official or authors >= 2`。
    _p_b, _p_b_ctx = new_plugin({"zzz": [
        ("绝区零3.3版本前瞻兑换码", "兑换码\nSINGLE0001\nSINGLE0002"),
    ]}, subscribers=["aiocqhttp:GroupMessage:999"])
    _p_b.conf["enabled_games"] = ["zzz"]
    _p_b._state = _p_b._empty_state()
    _p_b._state["baseline_games"] = ["zzz"]
    await _p_b.check_once()
    check("单作者（B 档）**不推送**", not _p_b_ctx.sent, str(_p_b_ctx.sent))
    check("单作者（B 档）**不入账**（下轮样本凑齐成 A 级时仍能推）",
          not (_p_b._state["seen"].get("zzz") or {}),
          str(_p_b._state["seen"].get("zzz")))

    print("\n=== [3d] 前瞻当晚 act_id 探测（星铁/绝区零专用，A+ 档源）===")
    # 触发信号 = 官方 B站直播间「正在直播」或「标题变了」；同一个信号只探 1 次。
    _room = {"27263119": {"title": "《崩坏：星穹铁道》4.6版本前瞻特别节目", "living": True}}
    _good = [{
        "post_id": "pr1", "subject": "米游社直播间开了", "created_at": time.time(), "uid": "u9",
        "text": "快看 https://webstatic.mihoyo.com/bbs/event/live/index.html"
                "?act_id=ea209901010000000001&game_biz=hkrpg_cn 直播间在发码",
    }]
    p_probe, _ = new_plugin({"starrail": []}, subscribers=["aiocqhttp:GroupMessage:999"],
                            live_rooms=_room, probe_posts=_good,
                            by_act={"ea209901010000000001": ["KSPROBE01"]})
    _hits, _ok, _live = await p_probe._collect("starrail")
    check("探测命中 ⇒ 官方码进了候选",
          any(h.code == "KSPROBE01" for h in _hits), str([h.code for h in _hits]))
    check("探测到的码被标成官方源（tier A）",
          any(h.code == "KSPROBE01" and h.tier == "A" and h.official for h in _hits),
          str([(h.code, h.tier, h.official) for h in _hits]))
    check("信号指纹记进了 state.probe",
          bool((p_probe._state.get("probe") or {}).get("starrail", {}).get("act_id")),
          str(p_probe._state.get("probe")))
    _before = p_probe.source.probe_calls
    _hits2, _o3, _l3 = await p_probe._collect("starrail")
    check("同一个信号不重复探测（省请求）", p_probe.source.probe_calls == _before,
          f"探测次数 {_before} → {p_probe.source.probe_calls}")
    check("命中过的 act_id 会被复用（直播期间每轮继续取码）",
          any(h.code == "KSPROBE01" for h in _hits2), str([h.code for h in _hits2]))

    # ⚠️ 最要紧的一条：**别的游戏**的 act_id 绝不能被认下来（否则会推错游戏的码）
    _wrong = [{
        "post_id": "pr2", "subject": "原神直播间", "created_at": time.time(), "uid": "u10",
        "text": "https://webstatic.mihoyo.com/bbs/event/live/index.html"
                "?act_id=ea202609041755176692&game_biz=hk4e",
    }]
    p_wrong, _ = new_plugin({"starrail": []}, live_rooms=_room, probe_posts=_wrong,
                            by_act={"ea202609041755176692": ["WRONGCODE1"]})
    _h2, _o2, _l2 = await p_wrong._collect("starrail")
    check("护栏：只有原神（hk4e）的链接时**不认** ⇒ 不会推错游戏的码",
          not any(h.code == "WRONGCODE1" for h in _h2), str([h.code for h in _h2]))
    check("未命中时 act_id 记为空（下一轮换信号还会再探）",
          (p_wrong._state.get("probe") or {}).get("starrail", {}).get("act_id") == "",
          str(p_wrong._state.get("probe")))

    print("\n=== [3e] 动态轮询（1 小时窗口 → 缩到 5 分钟 → 提醒后恢复）===")
    _now2 = _dtm.datetime.now(main.core.TZ)
    _soon2 = _now2 + _dtm.timedelta(minutes=30)
    _ttl = "《绝区零》3.3版本前瞻特别节目"
    _lives = {"zzz": {"title": _ttl, "start": _soon2, "living": False}}
    p_fast, _ = new_plugin({"zzz": []}, subscribers=["aiocqhttp:GroupMessage:999"])
    check("临近开播（30 分钟后）⇒ 下一轮缩到 5 分钟",
          p_fast._next_interval(3600, {"lives": _lives}) == 300,
          str(p_fast._next_interval(3600, {"lives": _lives})))
    p_fast._state["live_seen"] = {"zzz": {"fp": f"{_ttl}|{_soon2.isoformat()}", "noticed": True}}
    check("【回归】提醒已发出 ⇒ 恢复常规间隔（不会一直高频查）",
          p_fast._next_interval(3600, {"lives": _lives}) == 3600,
          str(p_fast._next_interval(3600, {"lives": _lives})))
    _far = _now2 + _dtm.timedelta(hours=5)
    check("开播还在 5 小时后 ⇒ 不加密",
          p_fast._next_interval(3600, {"lives": {"zzz": {"title": _ttl, "start": _far}}}) == 3600)
    check("没有精确开播时间（星铁/绝区零常态）⇒ 不加密",
          p_fast._next_interval(3600, {"lives": {"starrail": {"title": "x", "start": None}}}) == 3600)
    check("没有 live 信息 / 空结果 ⇒ 常规间隔",
          p_fast._next_interval(3600, {}) == 3600
          and p_fast._next_interval(3600, {"lives": {}}) == 3600)
    check("check_once 的返回里带上了 lives（供动态轮询判断）",
          "lives" in (await p_fast.check_once()), str(sorted((await p_fast.check_once()).keys())))

    print("\n=== [4] 同样候选再来 → 不重复推 ===")
    r3 = await plugin.check_once()
    check("无新码", r3["new"] == [], str(r3["new"]))
    check("没有重复推送", len(ctx.sent) == 1, str(len(ctx.sent)))

    print("\n=== [5] 推送失败 → 不记账 → 下轮重试 ===")
    payload2 = {"zzz": [
        ("绝区零3.4前瞻兑换码", "兑换码\nFAILCASE88"),
        ("【绝区零3.4】前瞻兑换码", "兑换码\nFAILCASE88"),
    ]}
    plugin2, ctx2 = new_plugin(payload2, fail_push=True, subscribers=["aiocqhttp:GroupMessage:999"])
    plugin2._state["baseline_games"] = list(CONF["enabled_games"])  # 跳过基线（保留订阅者）
    r5 = await plugin2.check_once()
    check("推送失败：本轮未记账", plugin2._state["seen"].get("zzz", {}) == {}, json.dumps(plugin2._state["seen"]))
    check("推送失败：本轮仍报出该码", len(r5["new"]) == 1, str(r5["new"]))
    ctx2.fail = False  # 平台恢复
    r5b = await plugin2.check_once()
    check("恢复后重试成功并记账", len(ctx2.sent) == 1 and "failcase88" in plugin2._state["seen"].get("zzz", {}), str(ctx2.sent))

    print("\n=== [6] 没有订阅者 → 不推送但记账 ===")
    payload3 = {"zzz": [
        ("绝区零3.5前瞻兑换码", "兑换码\nNOSUB777"),
        ("【绝区零3.5】前瞻兑换码", "兑换码\nNOSUB777"),
    ]}
    plugin3, ctx3 = new_plugin(payload3, subscribers=[])
    plugin3._state["baseline_games"] = list(CONF["enabled_games"])
    await plugin3.check_once()
    check("无订阅者：不推送", ctx3.sent == [], str(ctx3.sent))
    check("无订阅者：仍记账（不会攒一堆历史码）", "nosub777" in plugin3._state["seen"]["zzz"])

    print("\n=== [7] 命令行为 ===")
    ev = _FakeEvent()
    out = [x async for x in main.HoyoCodesPlugin.cmd_codes(plugin, ev, "绝区零")]
    check("/兑换码 绝区零 有返回", bool(out), str(out)[:120])
    # 现在查询走合并转发 ⇒ 返回的是消息链；降级时才是纯文本。两条路都要断言到"含游戏名"。
    if ev.chains:
        _ct = [c["text"] if isinstance(c, dict) else str(c)
               for n in ev.chains[0][0].nodes for c in n.content]
        check("/兑换码 返回内容含游戏名（合并转发头部）",
              any("【绝区零】" in t for t in _ct), str(_ct)[:140])
    else:
        check("/兑换码 返回内容含游戏名（降级纯文本）",
              "【绝区零】" in str(out[0]), str(out)[:140])
    ev2 = _FakeEvent()
    out2 = [x async for x in main.HoyoCodesPlugin.cmd_codes(plugin, ev2, "塞尔达")]
    check("未知游戏名有友好提示", out2 and "只认识" in out2[0], str(out2)[:80])

    # 设计要求：查询结果也走**合并转发**，而且**每个码独占一条**方便长按复制。
    # Node/Nodes 在测试桩里默认是 None（降级纯文本），所以这里打桩才能测到合并转发那条路。
    class _Node:
        def __init__(self, uin="", name="", content=None):
            self.uin, self.name, self.content = uin, name, content or []

    class _Nodes:
        def __init__(self, nodes):
            self.nodes = list(nodes)

    _saved = (main.Node, main.Nodes)
    main.Node, main.Nodes = _Node, _Nodes
    try:
        ev4 = _FakeEvent()
        out4 = [x async for x in main.HoyoCodesPlugin.cmd_codes(plugin, ev4, "绝区零")]
        check("/兑换码 走合并转发（返回消息链而不是纯文本）", len(ev4.chains) == 1, str(out4)[:80])
        if ev4.chains:
            _nodes = ev4.chains[0][0].nodes
            _texts = [c["text"] if isinstance(c, dict) else str(c)
                      for n in _nodes for c in n.content]
            check("每个兑换码独占一条消息",
                  any(t.strip() == "CLARET0909" for t in _texts), str(_texts)[:200])
            check("第一条是头部（含游戏名）", "【绝区零】" in _texts[0], str(_texts[:2]))
            check("码的条数 = 换行分不开的码数（不是把整段塞一条）",
                  len([t for t in _texts if t.strip() == "CLARET0909"]) == 1, str(_texts)[:200])
    finally:
        main.Node, main.Nodes = _saved
    ev3 = _FakeEvent("aiocqhttp:GroupMessage:888")
    out3 = [x async for x in main.HoyoCodesPlugin.cmd_subscribe(plugin, ev3)]
    _subs = plugin._state["subscribers"]
    check("/订阅兑换码（不带游戏名）= 全订",
          any(isinstance(s, dict) and s.get("umo") == "aiocqhttp:GroupMessage:888" and s.get("games") == [] for s in _subs)
          and "全部游戏" in out3[0], str(_subs))
    out3b = [x async for x in main.HoyoCodesPlugin.cmd_subscribe(plugin, ev3, "原神")]
    check("/订阅兑换码 原神 ⇒ 只订原神",
          any(isinstance(s, dict) and s.get("umo") == "aiocqhttp:GroupMessage:888" and s.get("games") == ["genshin"]
              for s in plugin._state["subscribers"]), str(plugin._state["subscribers"]))
    check("订阅回执写明范围", "原神" in out3b[0], out3b[0])
    out3c = [x async for x in main.HoyoCodesPlugin.cmd_subscribe(plugin, ev3, "塞尔达")]
    check("订阅未知游戏名有提示", "只认识" in out3c[0], str(out3c)[:80])
    _tg = await plugin._targets(plugin._state)
    check("_targets 返回 {umo: games}（旧格式字符串也读得懂）",
          isinstance(_tg, dict)
          and _tg.get("aiocqhttp:GroupMessage:888") == ["genshin"]
          and _tg.get("aiocqhttp:GroupMessage:999") == [],
          str(_tg))
    out3d = [x async for x in main.HoyoCodesPlugin.cmd_unsubscribe(plugin, ev3, "原神")]
    check("/退订兑换码 原神（只订了它）⇒ 全退",
          "已退订" in out3d[0]
          and not any((s.get("umo") if isinstance(s, dict) else s) == "aiocqhttp:GroupMessage:888"
                      for s in plugin._state["subscribers"]), str(out3d))
    [x async for x in main.HoyoCodesPlugin.cmd_subscribe(plugin, ev3)]
    out3e = [x async for x in main.HoyoCodesPlugin.cmd_unsubscribe(plugin, ev3, "原神")]
    _left = [s for s in plugin._state["subscribers"]
             if isinstance(s, dict) and s.get("umo") == "aiocqhttp:GroupMessage:888"]
    check("全订后只退一个游戏 ⇒ 其余继续推",
          bool(_left) and "genshin" not in (_left[0].get("games") or [])
          # ⚠️ 别写死数量（曾经是 2 = 三个游戏减去原神）：加游戏后它会假红。
          # 用「游戏表减去原神」来断言，以后加游戏不用改测试。
          and set(_left[0].get("games") or []) == set(main.core.GAMES) - {"genshin"}, str(_left))
    out4 = [x async for x in main.HoyoCodesPlugin.cmd_unsubscribe(plugin, ev3)]
    check("/退订兑换码（不带游戏名）移除 umo",
          not any((s.get("umo") if isinstance(s, dict) else s) == "aiocqhttp:GroupMessage:888"
                  for s in plugin._state["subscribers"]) and "已退订" in out4[0],
          str(plugin._state["subscribers"]))

    # 需求：①「现在命令只有订阅兑换码欸 没有订阅前瞻」⇒ 拆成两类独立订阅
    #                ②「/订阅前瞻 原神 星穹铁道 崩坏三 绝区零 类似这种的泛匹配」⇒ 支持多游戏
    ev5 = _FakeEvent(message="/订阅前瞻 原神 星穹铁道 崩坏三 绝区零")
    out5 = [x async for x in main.HoyoCodesPlugin.cmd_subscribe_live(plugin, ev5, "原神")]
    _s5 = [s for s in plugin._state["subscribers"] if s.get("umo") == ev5.unified_msg_origin]
    print(f"    多游戏订阅 → {_s5}")
    check("多游戏泛匹配：一次认下 4 个游戏",
          bool(_s5) and set(_s5[0].get("games") or []) == {"genshin", "starrail", "hi3", "zzz"}, str(_s5))
    check("只开了「前瞻」，没顺手把码推送也开上",
          bool(_s5) and _s5[0].get("codes") is False and _s5[0].get("live") is True, str(_s5))
    check("提示里列出了四个游戏名", "原神" in out5[0] and "绝区零" in out5[0], str(out5)[:120])

    ev6 = _FakeEvent("aiocqhttp:GroupMessage:777")  # 换个独立会话，免得和上面那条订阅记录互相影响
    [x async for x in main.HoyoCodesPlugin.cmd_subscribe(plugin, ev6, "")]
    _s6 = [s for s in plugin._state["subscribers"] if s.get("umo") == ev6.unified_msg_origin]
    check("「订阅兑换码」只开码推送（前瞻要另外订）",
          bool(_s6) and _s6[0].get("codes") is True and _s6[0].get("live") is False, str(_s6))
    _out6 = [x async for x in main.HoyoCodesPlugin.cmd_subscribe_live(plugin, ev6, "")]
    _s6b = [s for s in plugin._state["subscribers"] if s.get("umo") == ev6.unified_msg_origin]
    check("再补一条「订阅前瞻」⇒ 两样都开，且游戏偏好没被放大成全部",
          bool(_s6b) and _s6b[0].get("codes") is True and _s6b[0].get("live") is True, str(_s6b))
    _out6c = [x async for x in main.HoyoCodesPlugin.cmd_unsubscribe_live(plugin, ev6, "")]
    _s6c = [s for s in plugin._state["subscribers"] if s.get("umo") == ev6.unified_msg_origin]
    check("「退订前瞻」只关前瞻、码推送留着",
          bool(_s6c) and _s6c[0].get("live") is False and _s6c[0].get("codes") is True, str(_s6c))
    _ = _out6, _out6c
    out5 = [x async for x in main.HoyoCodesPlugin.cmd_status(plugin, _FakeEvent())]
    check("/兑换码状态 有输出", "兑换码插件状态" in out5[0] and "上次检查" in out5[0])
    out6 = [x async for x in main.HoyoCodesPlugin.cmd_selftest(plugin, _FakeEvent())]
    check("/兑换码自检 只读（不改状态）", "自检" in out6[0] and "CLARET0909" in out6[0], str(out6)[:120])

    print("\n=== [8] 清理与卸载 ===")
    plugin._state["seen"]["zzz"]["oldcode1"] = {
        "code": "OLDCODE1", "first_seen": "2020-01-01 00:00:00", "last_seen": "2020-01-01 00:00:00", "sources": ["x"],
    }
    await plugin.check_once()
    check("超期码被清理", "oldcode1" not in plugin._state["seen"]["zzz"])
    check("清理留下墓碑指纹", bool(plugin._state["tombstones"].get("zzz")))
    await plugin.terminate()
    check("terminate 关闭了数据源", plugin.source.closed is True)

    print("\n=== [8b] 生命周期：initialize 幂等 / terminate 收干净 ===")
    p = main.HoyoCodesPlugin(FakeContext(), dict(CONF))
    await p.initialize()
    t1 = p._task
    await p.initialize()  # 模拟热重载后又走一次
    check("initialize 幂等（不会起两个循环）", p._task is t1, f"{p._task} vs {t1}")
    check("任务确实在跑", t1 is not None and not t1.done())
    await p.terminate()
    check("terminate 后任务结束", t1.done(), str(t1))

    print("\n=== [9] 状态文件可持久化/重载 ===")
    raw = json.load(open(main.STATE_FILE, encoding="utf-8"))
    check("state.json 结构完整", {"seen", "tombstones", "subscribers", "baseline_games"} <= set(raw), str(list(raw)))
    plugin4 = main.HoyoCodesPlugin(FakeContext(), dict(CONF))
    st = await plugin4._load_state()
    check("新实例能读回已记账的码", "claret0909" in st.get("seen", {}).get("zzz", {}), json.dumps(st.get("seen", {}), ensure_ascii=False)[:120])


asyncio.run(main_async())
shutil.rmtree(TMP, ignore_errors=True)

print(f"\n{'=' * 46}\n通过 {PASS} / 失败 {FAIL}")
if FAILED:
    print("失败项：")
    for f in FAILED:
        print("  -", f)
sys.exit(1 if FAIL else 0)
