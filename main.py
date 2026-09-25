"""米哈游前瞻兑换码插件（国服）—— 查询 + 新码自动推送。

数据来源：米游社（miyoushe）官方社区的帖子。
  · 搜索接口 post/wapi/searchPosts（需 DS 签名）
  · 帖子详情 post/wapi/getPostFull
国服码 → 只走国服渠道；第三方公开 API（seria/Ennead）给的是**国际服码**，对国服玩家无效。

设计要点：
  1. 纯逻辑在 hoyo_core.py（零 AstrBot 依赖，可离线自测）；
  2. 状态落盘 + 原子写入；**推送成功才记录**（失败下轮重试，不会静默丢码）；
  3. 首次运行只建基线、不推送（避免上线即刷屏）；
  4. 明细默认保留 180 天，清理后留「指纹墓碑」，旧码重现不会被误报成新码。
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta
from typing import Any

import httpx

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain

try:  # 合并转发组件（QQ 个人号支持；老版本可能没有，降级为纯文本推送）
    from astrbot.api.message_components import Node, Nodes
except ImportError:  # pragma: no cover
    Node = None  # type: ignore[assignment]
    Nodes = None  # type: ignore[assignment]

from astrbot.api.star import Context, Star, register

try:  # 不同 AstrBot 版本导出位置略有差异，做个兼容
    from astrbot.api.event import MessageChain  # type: ignore
except Exception:  # pragma: no cover
    from astrbot.core.message.message_event_result import MessageChain  # type: ignore

# 同目录的纯逻辑核心（插件加载方式不保证目录在 sys.path 上，显式加一下）
_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import hoyo_core as core  # noqa: E402
import nte_core  # noqa: E402  # 异环（NTE）专用纯逻辑：TapTap HTML 解析 + 提码

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
BASE_HEADERS = {
    "User-Agent": UA,
    "Referer": "https://www.miyoushe.com/",
    "Accept": "application/json",
}

STATE_FILE = os.path.join(_PLUGIN_DIR, "data", "state.json")
STATE_VERSION = 1

DEFAULTS: dict[str, Any] = {
    "check_interval_minutes": 60,
    "startup_delay_seconds": 90,
    "retention_days": 180,
    "post_max_age_days": 7,
    "max_posts_per_game": 4,
    "min_cn_group": 2,
    "push_tiers": ["A", "B"],
    "watch_authors_per_game": 2,
    "enable_miyolive": True,
    # 合并转发推送：每个兑换码单独一条消息，方便长按复制到游戏里粘贴
    "push_as_forward": True,
    "forward_bot_name": "兑换码播报",
    "forward_bot_uin": "",
    "enabled_games": ["genshin", "starrail", "zzz", "hi3", "nte"],
    "push_targets": [],
    "request_interval_seconds": 1.2,
    "notify_on_empty_subscribers": False,
    # 前瞻直播提醒：每个前瞻事件只推一条，临近开播时才发
    "notify_live": True,
    "live_notice_minutes": 60,
    # 动态轮询：只要**已知开播时间且还在这个窗口内**，
    # 下一次轮询就缩短到 `live_fast_poll_minutes`，提醒发出后自动恢复
    "live_fast_window_minutes": 60,
    "live_fast_poll_minutes": 5,
    # 前瞻当晚的 act_id 探测（星铁/绝区零专用：官方从未挂过 miyolive 链接，
    # 但「将来会不会开」值得每场前瞻花 1 次请求盯着）
    "enable_act_id_probe": True,
    # 查询结果里是否列出**明确已过期**的码。
    # 需求：默认**不列**（「已经明确过期的话是不是能不展示了」）——
    # 列出来只占版面，复制了也没用。
    "show_expired_codes": False,
    "bili_live_rooms": {
        # 2026-09-16 实测（api.live.bilibili.com/room/v1/Room/getRoomInfoOld?mid=<官方B站号uid>）
        "genshin": "21987615",    # 原神官方号 uid=401742377
        "starrail": "27263119",   # 崩坏星穹铁道官方号 uid=1340190821
        "zzz": "32805602",        # 绝区零官方号 uid=1636034895
        "hi3": "1319882",         # 崩坏3第一偶像爱酱 uid=27534330（前瞻用；主号 room 7050189 是日常直播）
        # ⚠️ 异环的官方号「异环」(uid=3546636978489848) 的 `space/acc/info.live_room` 返回 null，
        # 官方 B站号查不到 ⇒ 靠 wbi 签名的 `search_type=live_room` 搜索 + 空间动态 `live_rcmd` 交叉验证。
        # 2026-09-16 之前这里漏配它，导致异环的前瞻提醒**整行没有直播间链接**（静默跳过）。
        "nte": "1890951177",      # 异环官方直播间
    },
}


def _render_query_text(
    data: dict[str, list[Any]], errs: list[str], *, show_expired: bool = False
) -> str:
    """把查询结果渲染成**纯文本**（合并转发不可用时的降级路径）。

    ⚠️ 别拿合并转发那套短消息用 `\\n` 拼起来顶替—— 那种"码一行、来源一行"的
    排版在纯文本里长按会一次选中相邻两行，反而更难复制；而且头部那句"长按下面每一条
    即可单独复制"在降级时是**假文案**。这里用 `render_codes` 的分段格式，一条消息一段。
    """
    parts = [
        core.render_codes(g, h, header="当前兑换码", show_tier_c=True, show_expired=show_expired)
        for g, h in data.items()
    ]
    parts.extend(errs)
    return "\n\n".join(parts) if parts else "没有启用任何游戏，或者查询失败。"


def _norm_sub(item: Any) -> dict[str, Any] | None:
    """把一条订阅记录归一化成 `{umo, games, codes, live}`。

    兼容三种历史格式，并丢弃「两样通知都关掉」的废条目：
      · 老：`"umo字符串"`（= 全订，码与前瞻都收）
      · 中：`{"umo": "...", "games": [...]}`（缺 codes/live 时**默认都为 True** —— 保持老用户行为不变）
      · 新：`{"umo": "...", "games": [...], "codes": true, "live": false}`
    """
    if isinstance(item, dict):
        umo = str(item.get("umo") or "").strip()
        games = [g for g in (item.get("games") or []) if g in core.GAMES]
        codes = bool(item.get("codes", True))
        live = bool(item.get("live", True))
    else:
        umo, games, codes, live = str(item or "").strip(), [], True, True
    if not umo or (not codes and not live):
        return None
    return {"umo": umo, "games": games, "codes": codes, "live": live}


def _sub_label(item: Any) -> str:
    """把一条订阅记录渲染成人话（「全部·码+前瞻」/「原神·仅前瞻」）。"""
    sub = _norm_sub(item)
    if sub is None:
        return ""
    games = [core.GAMES[g]["short"] for g in sub["games"] if g in core.GAMES]
    scope = "·".join(games) if games else "全部"
    kinds = ("码" if sub["codes"] else "") + ("前瞻" if sub["live"] else "")
    return f"{scope}·{kinds}" if kinds else scope


class MiyousheSource:
    """米游社只读数据源。"""

    def __init__(self, interval: float = 1.2, timeout: float = 20.0) -> None:
        self._client = httpx.AsyncClient(timeout=timeout, headers=BASE_HEADERS, follow_redirects=True)
        self._interval = max(0.2, float(interval))
        self._last_req = 0.0
        self._throttle_lock = asyncio.Lock()  # 并发安全：否则"最小间隔"实测会退化成 0

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001
            pass

    async def _throttle(self) -> None:
        async with self._throttle_lock:
            delta = time.monotonic() - self._last_req
            if delta < self._interval:
                await asyncio.sleep(self._interval - delta)
            self._last_req = time.monotonic()

    async def _get(self, url: str, params: dict | None = None, headers: dict | None = None) -> dict:
        last_err: Exception | None = None
        for attempt in (1, 2, 3):  # 实测米游社偶发 SSL EOF，多给两次机会
            await self._throttle()
            try:
                resp = await self._client.get(url, params=params, headers=headers)
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                return resp.json()
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < 3:
                    await asyncio.sleep(1.5 * attempt)
        raise RuntimeError(f"请求失败：{url} :: {last_err}")

    async def search(self, keyword: str, size: int = 10, *, order_type: int | None = None) -> list[dict]:
        """搜索帖子。

        ⚠️ 两个实测结论（已独立复核）：
        1. 响应里是 `data.posts`（不是 `data.list`），且**每条帖直接带完整正文**
           （`post.structured_content`，与 getPostFull 逐字一致）
           ⇒ 生产只需搜索，**不必逐帖调 getPostFull**，请求数直接降到 1/3。
        2. `getPostFull` 有风控：连续请求第 31 次起全部 `retcode=1034`
           ⇒ 少调它就是保命。

        ⭐ `order_type`（2026-09-20 星铁 4.6 前瞻当晚实测新增）：
        · 不传 = 服务端默认的**相关度排序**。问题：前瞻当晚社区刷屏，新帖热度低，
          **会被"热度靠前的老帖"挤出前 N 名** —— 实测 size=10 时"7 天内的帖 = 0 条"，
          size 加到 30 也只有 1 条 ⇒ 这是「星铁 4.6 兑换码明明 19:48 就有了、插件却查不到」的根因。
        · `order_type=2` = **按时间倒序**。实测同关键词立刻拿到当天 15~20 条新帖。
        · ⚠️ 时间序的代价：搜索是**分词宽松匹配**，时间序会让"今天所有含『兑换码』的帖"
          涌进来（今晚用「原神兑换码」搜出来的 20 条**全是星铁的**）⇒ **必须配合
          `game_id` 硬归属过滤**（见 `_filter_by_game`）。两者是一套，不能只上一个。
        """
        url = "https://bbs-api.mihoyo.com/post/wapi/searchPosts"
        params: dict[str, Any] = {"keyword": keyword, "size": size, "offset": 0}
        if order_type is not None:
            params["order_type"] = int(order_type)
        data = await self._get(
            url,
            params=params,
            headers={
                "DS": core.make_ds(keyword),
                "x-rpc-app_version": "2.71.1",
                "x-rpc-client_type": "4",
                "x-rpc-language": "zh-cn",
            },
        )
        if data.get("retcode") != 0:
            raise RuntimeError(f"搜索 retcode={data.get('retcode')} {data.get('message')}")
        payload = data.get("data") or {}
        return self._parse_items(payload.get("list") or payload.get("posts") or [])

    @staticmethod
    def _parse_items(items: list) -> list[dict]:
        """统一解析 searchPosts / userPost 的 items（两者结构一致，都自带完整正文）。"""
        out = []
        for item in items:
            post = item.get("post") or {} if isinstance(item, dict) else {}
            out.append(
                {
                    "post_id": str(post.get("post_id") or ""),
                    "subject": str(post.get("subject") or ""),
                    "created_at": post.get("created_at") or 0,
                    # 响应里就带正文；注意 post.content 被截断成 100 字符，不能用
                    "text": core.delta_to_text(post.get("structured_content")),
                    # 作者 uid：交叉验证按「不同作者数」去重，不是按帖数
                    "uid": str(post.get("uid") or ""),
                    # ⭐ **米游社给每条帖的权威归属字段**（星铁=6 / 原神=2 / 绝区零=8 / 崩坏3=1）。
                    # 它才是"这条帖属于哪个游戏"的唯一可靠判据 —— 靠标题/正文里有没有游戏名去猜，
                    # 2026-09-20 被"星铁前瞻里公布《绝区零》联动"击穿过（星铁帖正文含"绝区零"）。
                    "game_id": post.get("game_id"),
                }
            )
        return [p for p in out if p["post_id"]]

    async def user_posts(self, uid: str, size: int = 10) -> list[dict]:
        """备源 1：盯作者 —— 直接拉某位作者最近的帖子（完全绕开关键词搜索）。

        实测：专发兑换码的攻略作者一发帖就带全文，比搜索更精准。
        """
        url = "https://bbs-api.mihoyo.com/post/wapi/userPost"
        data = await self._get(url, params={"uid": uid, "size": size, "offset": 0})
        if data.get("retcode") != 0:
            raise RuntimeError(f"userPost({uid}) retcode={data.get('retcode')}")
        payload = data.get("data") or {}
        return self._parse_items(payload.get("list") or payload.get("posts") or [])

    async def miyolive_codes(
        self, uid: int
    ) -> tuple[list[str], datetime | None, dict[str, Any] | None]:
        """备源 2：官方 miyolive 直播活动（**原神**与**崩坏3**都走这条）。

        链路：官方号帖子 → act_id → miyolive/index → code_ver → refreshCode → code_list。
        ⚠️ 星铁/绝区零的官方号**从不挂 act_id**（复勘 280 帖确认）⇒ 它们走
        `probe_act_id()` 的「前瞻当晚探测」那条路，别把 uid 填上白费请求。

        Returns:
            `(码列表, 推断的有效期, 直播信息)`
            · 有效期取「直播结束 + 3 天」保守估计（前瞻码历史上都在直播后 1~2 天内失效）；
            · 直播信息 = `{"title": ..., "start": datetime|None}` —— `start` 是**官方给的精确开播时间**，
              用来做「前瞻直播提醒」。
        """
        listing = await self._get(
            "https://bbs-api.mihoyo.com/painter/api/user_instant/list",
            params={"offset": 0, "size": 20, "uid": uid},
        )
        act_id = ""
        for item in (listing.get("data") or {}).get("list") or []:
            post = ((item.get("post") or {}).get("post")) or {}
            m = re.search(r"act_id=([^&\"\\]+)", str(post.get("structured_content") or ""))
            if m:
                act_id = m.group(1)
                break
        if not act_id:
            return [], None, None
        return await self.miyolive_codes_by_act(act_id)

    async def miyolive_codes_by_act(
        self, act_id: str
    ) -> tuple[list[str], datetime | None, dict[str, Any] | None]:
        """已知 `act_id` 时直接走 miyolive 取码（`index` → `refreshCode`）。

        崩坏3 靠它；星铁/绝区零的「前瞻当晚探测」（`probe_act_id`）命中后也走这条。
        """
        if not act_id:
            return [], None, None
        idx = await self._get(
            "https://api-takumi.mihoyo.com/event/miyolive/index",
            headers={"x-rpc-act_id": act_id},
        )
        if idx.get("retcode") != 0:
            return [], None, None
        live = (idx.get("data") or {}).get("live") or {}
        ver = live.get("code_ver")
        if not ver:
            return [], None, None
        expire_at: datetime | None = None
        start_at: datetime | None = None
        try:
            end_dt = datetime.strptime(str(live.get("end") or ""), "%Y-%m-%d %H:%M:%S").replace(tzinfo=core.TZ)
            expire_at = end_dt + timedelta(days=3)  # 前瞻码历史上都在直播后 1~2 天内失效，取 3 天保守
        except ValueError:
            pass
        try:
            start_at = datetime.strptime(
                str(live.get("start") or ""), "%Y-%m-%d %H:%M:%S"
            ).replace(tzinfo=core.TZ)
        except ValueError:
            pass
        rc = await self._get(
            "https://api-takumi-static.mihoyo.com/event/miyolive/refreshCode",
            # ⚠️ `time` 必须是 **20 的整数倍**（前端 bundle 就是这么传的：`floor(now/20)*20`）。
            # 用任意时间戳实测也能返回，但按官方口径来更稳。
            params={"version": ver, "time": str(int(time.time()) // 20 * 20)},
            headers={"x-rpc-act_id": act_id},
        )
        out = []
        for item in (rc.get("data") or {}).get("code_list") or []:
            code = str(item.get("code") or "").strip()
            if code:
                out.append(code)
        logger.info(f"[hoyo_codes] 官方 miyolive 返回 {len(out)} 个码（act_id={act_id}）")
        return out, expire_at, {"title": str(live.get("title") or ""), "start": start_at}

    @staticmethod
    def match_act_id(texts: list[str], biz: str) -> str:
        """从若干帖正文里找**属于本游戏**的 miyolive `act_id`（找不到返回空串）。

        判据从严（唯一能防「推错游戏的码」的手段）：`act_id=` 附近那段文本里必须**同时**含
        `bbs/event/live` 和 `game_biz=<本游戏>`。
        ⚠️ 抽成 staticmethod 是为了让测试桩能**复用同一份判据**，而不是抄一份（抄的那份会失真）。
        """
        if not biz:
            return ""
        for raw in texts:
            text = str(raw or "")
            for m in re.finditer(r"act_id=([A-Za-z0-9]+)", text):
                seg = text[max(0, m.start() - 200): m.end() + 240]
                if "bbs/event/live" in seg and f"game_biz={biz}" in seg:
                    return m.group(1)
        return ""

    async def probe_act_id(self, game: str) -> str:
        """**前瞻当晚探测**：搜 `act_id`，找属于**本游戏**的 miyolive 活动链接。

        背景：星铁/绝区零官方从未挂过 act_id，
        但「将来会不会开」值得每场前瞻花 1 次请求盯着 —— 真开了的话，前瞻当晚玩家会
        刷屏分享 `webstatic.mihoyo.com/bbs/event/live/index.html?act_id=…&game_biz=…`。
        """
        biz = str(core.GAMES.get(game, {}).get("game_biz") or "")
        if not biz:
            return ""
        found = await self.search("act_id", size=20)
        act_id = self.match_act_id([str(p.get("text") or "") for p in found], biz)
        if act_id:
            logger.info(f"[hoyo_codes] act_id 探测命中：{game} act_id={act_id}")
        return act_id

    async def live_room_status(self, room_id: str) -> dict:
        """查 B站官方直播间的状态 —— **比"从帖子猜前瞻时间"可靠**。

        官方前瞻直播间是**常设**的，标题里会写「《XX》X.X版本前瞻特别节目」。

        Returns:
            `{"title": str, "living": bool, "cover": str}`；查不到返回空 dict。
        """
        try:
            data = await self._get(
                "https://api.live.bilibili.com/room/v1/Room/get_info",
                params={"room_id": room_id},
                headers={"Referer": "https://live.bilibili.com/"},
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hoyo_codes] 查直播间 {room_id} 失败：{exc}")
            return {}
        info = data.get("data") or {}
        return {
            "title": str(info.get("title") or ""),
            "living": int(info.get("live_status") or 0) == 1,
            # 封面图：`user_cover` 是常驻封面（稳定，未开播也有）；`keyframe` 是当前画面截图（开播才有）。
            # 2026-09-16 实测 `i0.hdslb.com` **无 Referer 也返回 200 image/jpeg** ⇒ 可以直接给 QQ 抓。
            "cover": str(info.get("user_cover") or info.get("keyframe") or ""),
            "online": int(info.get("online") or 0),
        }

    async def post_text(self, post_id: str) -> dict:
        url = "https://bbs-api.mihoyo.com/post/wapi/getPostFull"
        data = await self._get(url, params={"post_id": post_id, "read": 1})
        if data.get("retcode") != 0:
            raise RuntimeError(f"帖子 {post_id} retcode={data.get('retcode')}")
        wrapper = (data.get("data") or {}).get("post") or {}
        post = wrapper.get("post") or {}
        user = wrapper.get("user") or {}
        return {
            "post_id": post_id,
            "subject": str(post.get("subject") or ""),
            "text": core.delta_to_text(post.get("structured_content")),
            "author_uid": str(user.get("uid") or ""),
            "created_at": post.get("created_at") or 0,
        }


# ── 异环（NTE）：TapTap 社区源 ──────────────────────────────────────────────
NTE_APP_ID = 714119
TAPTAP_HEADERS = {
    "User-Agent": UA,
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}


class TapTapSource:
    """异环专用数据源 —— **TapTap 社区**（Hotta Studio 的官方社区主阵地）。

    为什么单独一个类：异环**不在米游社体系**，而且 TapTap 是 **SSR HTML**（没有 JSON 接口）：

      · 发现层 `GET /app/714119/topic`（列表页 SSR，7~10 条按时间倒序，**带绝对发布时间**）
      · 取码层 `GET /moment/<id>`（详情页 SSR，正文在 `div.tap-rich-content` 里）

    2026-09-16 实测：**免登录、免 cookie、免浏览器、连 UA 都不强制**。
    ⚠️ 列表页**每页只覆盖约 4~5 小时**（社区发帖量决定）⇒ 轮询间隔必须 < 4 小时。
    """

    BASE = "https://www.taptap.cn"

    def __init__(self, app_id: int = NTE_APP_ID, interval: float = 1.2, timeout: float = 20.0) -> None:
        self.app_id = app_id
        self._client = httpx.AsyncClient(timeout=timeout, headers=TAPTAP_HEADERS, follow_redirects=True)
        self._interval = max(0.5, float(interval))
        self._last_req = 0.0
        self._throttle_lock = asyncio.Lock()

    async def close(self) -> None:
        try:
            await self._client.aclose()
        except Exception:  # noqa: BLE001
            pass

    async def _throttle(self) -> None:
        async with self._throttle_lock:
            delta = time.monotonic() - self._last_req
            if delta < self._interval:
                await asyncio.sleep(self._interval - delta)
            self._last_req = time.monotonic()

    async def _get_html(self, url: str, params: dict | None = None) -> str:
        last_err: Exception | None = None
        for attempt in (1, 2, 3):
            await self._throttle()
            try:
                resp = await self._client.get(url, params=params)
                if resp.status_code != 200:
                    raise RuntimeError(f"HTTP {resp.status_code}")
                return resp.text
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                if attempt < 3:
                    await asyncio.sleep(1.5 * attempt)
        raise RuntimeError(f"请求失败：{url} :: {last_err}")

    async def list_posts(self, *, official: bool = False, page: int = 1) -> list[dict]:
        """话题列表页（发现层）。`official=True` 走官方 tab（前瞻日程信号在那里）。"""
        params: dict[str, Any] = {}
        if official:
            params["type"] = "official"
        if page > 1:
            params["page"] = page
        html = await self._get_html(f"{self.BASE}/app/{self.app_id}/topic", params or None)
        return nte_core.parse_taptap_list(html)

    async def post_detail(self, moment_id: str) -> dict | None:
        """帖子详情页（取码层）。"""
        url = f"{self.BASE}/moment/{moment_id}"
        html = await self._get_html(url)
        return nte_core.parse_taptap_post(html, url=url)


@register(
    "astrbot_plugin_hoyo_nte_codes",
    "whale",
    "前瞻兑换码（国服）：原神/星穹铁道/绝区零/崩坏3 + 异环 查询与自动推送",
    "1.3.0",
)
class HoyoCodesPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None) -> None:
        super().__init__(context)
        # ⚠️ 这里**绝不允许抛异常**：AstrBot 在配置构造失败时会丢弃用户**整份**配置
        # 并回退默认（star_manager.py:1220-1233）
        conf = config if isinstance(config, dict) else {}
        self.conf: dict[str, Any] = {**DEFAULTS, **conf}
        try:
            interval = float(self.conf.get("request_interval_seconds") or 1.2)
        except (TypeError, ValueError):
            interval = 1.2
        self.source = MiyousheSource(interval=interval)
        self._nte: TapTapSource | None = None  # 懒创建：只有启用异环时才连 TapTap
        self._task: asyncio.Task | None = None
        self._state_lock = asyncio.Lock()  # 只保护状态读改写（短临界区）
        self._fetch_lock = asyncio.Lock()  # 保护网络抓取（长临界区）
        self._state: dict[str, Any] | None = None
        self._bot_identity_cache: dict[str, tuple[str, str]] = {}  # platform_id -> (uin, 昵称)

    # ── 配置 & 状态 ────────────────────────────────────────────────────────
    def _cfg(self, key: str) -> Any:
        return self.conf.get(key, DEFAULTS.get(key))

    def _cfg_int(
        self,
        key: str,
        default: int,
        *,
        minimum: int | None = None,
        maximum: int | None = None,
    ) -> int:
        """安全取整数：字符串/None 都不会炸，0 也不会被当成"没配置"；顺带做范围钳制。"""
        try:
            val = int(self.conf.get(key, default))
        except (TypeError, ValueError):
            val = int(default)
        if minimum is not None and val < minimum:
            logger.warning(f"[hoyo_codes] 配置 {key}={val} 越界，已按下限 {minimum} 处理")
            val = minimum
        if maximum is not None and val > maximum:
            logger.warning(f"[hoyo_codes] 配置 {key}={val} 越界，已按上限 {maximum} 处理")
            val = maximum
        return val

    def _cfg_list(self, key: str, default: list | None = None) -> list:
        """安全取列表：逗号字符串也认；非法类型回退默认。"""
        fallback = list(default or [])
        val = self.conf.get(key, fallback)
        if isinstance(val, str):
            return [x.strip() for x in val.split(",") if x.strip()]
        if isinstance(val, (list, tuple)):
            return list(val)
        return fallback

    def _cfg_dict(self, key: str, default: dict | None = None) -> dict:
        """安全取字典（如各游戏的固定直播间表）；非法类型回退默认。"""
        fallback = dict(default if default is not None else DEFAULTS.get(key) or {})
        val = self.conf.get(key, fallback)
        return dict(val) if isinstance(val, dict) else fallback

    def _enabled_games(self) -> list[str]:
        """启用哪些游戏。⚠️ 空列表 = **不启用任何游戏**（尊重配置），并打 ERROR 让它显眼，
        而不是悄悄回退成"全部启用"。"""
        picked = [g for g in self._cfg_list("enabled_games", list(core.GAMES)) if g in core.GAMES]
        if not picked:
            logger.error("[hoyo_codes] enabled_games 为空或全是无效值 ⇒ 不启用任何游戏（到 WebUI 勾一个）")
        return picked

    def _empty_state(self) -> dict[str, Any]:
        return {
            "version": STATE_VERSION,
            # ⚠️ 基线是**按游戏**记的：抓取失败的游戏不算建好基线
            "baseline_games": [],
            "seen": {},
            "tombstones": {},
            "watched": {},
            "delivered": {},  # 每个码已成功投递给哪些会话
            "subscribers": [],
            "last_check": "",
            "last_error": "",
            "last_games": {},
            "checks": 0,
            "pushed": 0,
        }

    async def _load_state(self) -> dict[str, Any]:
        if self._state is not None:
            return self._state
        state = self._empty_state()
        try:
            if os.path.exists(STATE_FILE):
                with open(STATE_FILE, "r", encoding="utf-8") as fh:
                    loaded = json.load(fh)
                if isinstance(loaded, dict):
                    state.update(loaded)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[hoyo_codes] 读取状态失败（把坏文件改名留证，按空状态继续）：{exc}")
            try:  # 把坏文件留一份，别静默覆盖
                os.replace(STATE_FILE, f"{STATE_FILE}.bad-{int(time.time())}")
            except Exception:  # noqa: BLE001
                pass
        # ⚠️ 类型校验：state.json 被写坏/被手改后，错误类型会让每轮都抛异常而**永久瘫痪**
        for key in ("seen", "tombstones", "watched", "delivered", "last_games", "live_seen", "probe"):
            if key not in state:  # 首次运行 / 老版本的状态文件没这个键：静默补默认，不算异常
                state[key] = {}
                continue
            if not isinstance(state.get(key), dict):
                logger.error(f"[hoyo_codes] state[{key}] 类型不对，已重置为空 dict")
                state[key] = {}
        for key in ("subscribers", "baseline_games"):
            if not isinstance(state.get(key), list):
                logger.error(f"[hoyo_codes] state[{key}] 类型不对，已重置为空 list")
                state[key] = []
        self._state = state
        return state

    async def _save_state(self, state: dict[str, Any]) -> None:
        def _write() -> None:
            os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
            tmp = f"{STATE_FILE}.{os.getpid()}.{id(state)}.tmp"  # 唯一化，防止并发写互踩
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(state, fh, ensure_ascii=False, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, STATE_FILE)  # 原子替换

        try:
            await asyncio.to_thread(_write)
        except Exception as exc:  # noqa: BLE001
            logger.error(f"[hoyo_codes] 写状态失败：{exc}")

    # ── 抓取 ──────────────────────────────────────────────────────────────
    async def _collect(
        self, game: str
    ) -> tuple[list[core.CodeHit], bool, dict[str, Any] | None]:
        """抓一个游戏当前的候选码 —— **三源容灾**：

        主源   `searchPosts`（关键词，覆盖最广）
        备源 1 `userPost`（盯「从主源学到的、专发兑换码的活跃作者」，完全绕开关键词搜索）
        备源 2 `miyolive`（官方直播活动，实测目前只有原神有效，但最权威）

        Returns:
            `(候选码, 是否至少有一个源真的成功)` —— 第二个值决定这个游戏能不能建基线。
        """
        meta = core.GAMES[game]
        # 异环不在米游社体系 ⇒ 走 TapTap 链路（列表页发现 + 详情页取码）
        if meta.get("source") == "nte":
            return await self._collect_nte(game)
        max_age = self._cfg_int("post_max_age_days", 7, minimum=1)
        cutoff = time.time() - max_age * 86400
        max_posts = max(1, self._cfg_int("max_posts_per_game", 4, minimum=1))
        watch_limit = max(0, self._cfg_int("watch_authors_per_game", 2, minimum=0))
        search_size = max(10, self._cfg_int("search_size", 20, minimum=10))
        ok = False  # 本轮有没有任何一个源真的成功了

        seen_ids: set[str] = set()
        search_cands: list[dict] = []
        watch_cands: list[dict] = []

        # ── 主源：关键词搜索（**两种排序各跑一遍**）──
        # · `order_type=2`（时间倒序）：保证「前瞻当晚刚发的帖」一定进得来 —— 这正是
        #   2026-09-20 星铁 4.6 抓不到码的根因（默认相关度排序把新帖挤出了前 N 名：
        #   size=10 时"7 天内的帖 = 0 条"，size 加到 30 也只有 1 条）。
        # · 默认排序（相关度）：保证「近 7 天内的高相关帖」不漏，且对冷门游戏友好 ——
        #   时间序被热门游戏刷屏时，冷门游戏的最新 20 条里可能一条自己的都没有。
        # 两者合并去重，再统一做 `game_id` 硬归属过滤（见 `_filter_by_game`）。
        for kw in meta["keywords"][:2]:
            for order_type in (2, None):
                try:
                    found = await self.source.search(kw, size=search_size, order_type=order_type)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"[hoyo_codes] 搜索失败 [{game}] {kw} order_type={order_type}: {exc}"
                    )
                    continue
                ok = True
                found = self._filter_by_game(found, game)
                self._merge_posts(found, search_cands, seen_ids, cutoff)
        # 候选按**发布时间倒序** —— 这样下面「取前 N 帖」才真的等于「取最新的 N 帖」
        search_cands.sort(key=lambda p: p.get("created_at") or 0, reverse=True)

        # ── 备源 1：盯活跃作者 ──
        state = await self._load_state()
        watched = list((state.get("watched") or {}).get(game) or [])
        for uid in watched[-watch_limit:] if watch_limit else []:
            try:
                found = await self.source.user_posts(uid, size=10)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[hoyo_codes] 盯人失败 [{game}] uid={uid}: {exc}")
                continue
            ok = True
            found = self._filter_by_game(found, game)
            self._merge_posts(found, watch_cands, seen_ids, cutoff)

        # 给盯人**预留**名额（否则盯人的帖会被搜索结果的排名挤掉），
        # 但不能砍掉搜索的名额 —— 没盯人帖时搜索要能占满 max_posts
        watch_reserved = min(len(watch_cands), max(1, max_posts // 2)) if watch_cands else 0
        search_quota = max(1, max_posts - watch_reserved)
        picked = search_cands[:search_quota] + watch_cands[:watch_reserved]
        candidates = search_cands + watch_cands

        # ── 提码 ──
        min_cn_group = self._cfg_int("min_cn_group", 2, minimum=1)
        records: list[dict] = []
        for p in picked:
            text = p.get("text") or ""
            if not text:
                continue
            subject = p.get("subject") or ""
            cands = core.extract_candidates(
                text, subject=subject, exclude=[p["post_id"]], min_cn_group=min_cn_group
            )
            if not cands:
                continue
            # 有效期：**标题 + 正文**一起找（真机上「…截止时间明天晚上23:59」常常只写在标题里）；
            # 相对日期（明天/今晚/次日）以**发帖时间**为基准 —— 不是抓取时间。
            _posted = (
                datetime.fromtimestamp(p["created_at"], core.TZ) if p.get("created_at") else None
            )
            expiry = core.extract_expiry(text, subject=subject, posted_at=_posted)
            if expiry is None and core.has_expired_marker(subject + text[:200]):
                # 明写「（已过期）」但没写具体时间：给个**过去**的时间戳 ——
                # 查询里显示「已过期」、推送侧会被过滤掉
                expiry = core.expired_sentinel()
            records.append(
                {
                    "post_id": p["post_id"],
                    "title": subject,
                    "uid": p.get("uid") or "",
                    "candidates": cands,
                    "text": text,  # 标题复读判据要用正文
                    # 正文/标题里**明写**的有效期（提不到就是 None，不做猜测）
                    "expire_at": expiry,
                    # ⭐ 社区帖提取出来的时间 = **帖子里明确写的** ⇒ `False`（非估算）。
                    # ⚠️ 对抗性审查：这个键**必须有产出点** —— 少了它 `rec.get()` 恒为 `None`，
                    # 而 `_prefer_expiry` 会把 `None` 与官方的 `True`（估算）当同级 ⇒
                    # 「明确写的 > 估算的」直接变成死代码，官方估算照样顶掉社区明写。
                    "expire_estimated": False,
                }
            )

        # ── 有效期补充：从「含有效期线索」的候选帖里再捞一遍（2026-09-20 加）──
        # 为什么单独一步：`picked` 只取**最新** N 帖，而「有效期写得最清楚」的那条常常是稍早发的。
        # 实测（2026-09-20 星铁 4.6 当晚）：09-20 20:26 那条标题就写着「截止时间明天晚上23:59」，
        # 却排在第 5 位被 pick 掉 ⇒ 3 个 4.6 码的有效期修完提取逻辑**仍是空**。
        # ⚠️ 这些帖**只用来补有效期、绝不引入新码**（否则等于绕开 `max_posts` 的噪音控制）。
        if records:
            _picked_ids = {p["post_id"] for p in picked}
            _known = {c.code.lower() for r in records for c in r["candidates"]}
            _now_dt = datetime.now(core.TZ)
            _extra = [
                p
                for p in candidates
                if p["post_id"] not in _picked_ids
                and any(
                    w in (str(p.get("subject") or "") + str(p.get("text") or ""))
                    for w in core.EXPIRY_WORDS
                )
            ]
            for p in _extra:
                _text = p.get("text") or ""
                if not _text:
                    continue
                _posted = (
                    datetime.fromtimestamp(p["created_at"], core.TZ) if p.get("created_at") else None
                )
                _exp = core.extract_expiry(_text, subject=p.get("subject") or "", posted_at=_posted)
                if _exp is None:
                    continue
                _here = {
                    c.code.lower()
                    for c in core.extract_candidates(
                        _text,
                        subject=p.get("subject") or "",
                        exclude=[p["post_id"]],
                        min_cn_group=min_cn_group,
                    )
                } & _known
                if not _here:
                    continue
                for r in records:
                    if any(c.code.lower() in _here for c in r["candidates"]):
                        # ⚠️ **只填空、绝不覆盖**（实测发现的严重缺陷）：
                        # `_extra` 的定位是"补**缺失**的有效期"。若允许"取更早"覆盖，一条**三天前**
                        # 的老帖（写着「截止时间明天晚上23:59」⇒ 按**发帖时间**解析成**过去**）提到
                        # 同一个码，就能把真码的有效期压到过去 ⇒ 判"已过期" ⇒ **永久不推**
                        # （不推就不入账 ⇒ 每轮重判为新、每轮又被过滤掉），而且账本被**不可逆污染**。
                        #
                        # ⚠️ 而且**只补"还没过期"的值**：老帖是在**它自己发帖时**说话的，它说的
                        # "明天 23:59" 到今天早已过去。既然这个码此刻还在被 picked 的新帖讨论，
                        # 就说明它**没过期** ⇒ 老帖那个"过去的失效时间"与之矛盾、不可信，丢掉。
                        # （漏掉这一层，上面那个"永久漏推"照样复现 —— 只填空也救不了。）
                        if r.get("expire_at") is None and _exp > _now_dt:
                            r["expire_at"] = _exp
                            r["expire_estimated"] = False

        # ── 备源 2：官方 miyolive ──
        official_ok = False
        live_info: dict[str, Any] | None = None
        if self._cfg("enable_miyolive") and meta.get("miyolive_uid"):
            try:
                official_codes, official_expire, live_meta = await self.source.miyolive_codes(
                    int(meta["miyolive_uid"])
                )
                official_ok = True
                if live_meta:
                    live_info = {
                        "title": str(live_meta.get("title") or ""),
                        "start": live_meta.get("start"),
                        "room": "",
                    }
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[hoyo_codes] miyolive 失败 [{game}]: {exc}")
                official_codes, official_expire = [], None
            for code in official_codes:
                kind = "cn" if all("\u4e00" <= ch <= "\u9fff" for ch in code) else "ascii"
                records.append(
                    {
                        "post_id": f"miyolive:{code}",
                        "title": "官方前瞻直播活动",
                        "uid": "official:miyolive",
                        "candidates": [core.Candidate(code, kind, "cn")],
                        # 官方活动**不提供码的有效期**，这里是「直播结束 + 3 天」的**自行估算**
                        # （免得"活动早就结束了、源刚恢复"时把过期码当新码推）。
                        # ⚠️ 标成 `expire_estimated=True`：它只是兜底，**社区帖里明写的时间优先**
                        # （需求：「按照社区帖子的时间吧 社区一般更准 官方那个没更新过期时间」）。
                        "expire_at": official_expire,
                        "expire_estimated": True,
                    }
                )

        # ── 前瞻直播线索 ──
        # 原神：直接用官方 miyolive 的 `live.start`（精确到秒）；
        # 星铁/绝区零没有 miyolive ⇒ 从官方号的「前瞻特别节目预告」帖里看。
        for p in picked:
            text = p.get("text") or ""
            room = core.extract_live_room(text)
            if live_info is not None:
                if room and not live_info.get("room"):
                    live_info["room"] = room
                continue
            subject = p.get("subject") or ""
            if core.is_live_title(subject):
                live_info = {
                    "title": core.truncate(subject, 40),
                    "start": core.extract_live_start(text, subject=subject),
                    "room": room,
                }

        # ── 官方直播间状态（最可靠的前瞻信号）──
        # 官方前瞻直播间是**常设**的，标题就写着「《XX》X.X版本前瞻特别节目」，
        # 比"从帖子猜时间"稳得多；`live_status=1` 还能直接看出"正在直播"。
        room_id = str(
            (live_info or {}).get("room") or self._cfg_dict("bili_live_rooms").get(game) or ""
        )
        if room_id:
            try:
                status = await self.source.live_room_status(room_id)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[hoyo_codes] 直播间状态查询失败 [{game}]：{exc}")
                status = {}
            room_title = str(status.get("title") or "")
            if live_info is None and core.is_live_title(room_title):
                live_info = {"title": room_title, "start": None, "room": room_id}
            if live_info is not None:
                live_info["room"] = room_id
                live_info["living"] = bool(status.get("living"))
                if status.get("cover"):
                    live_info["cover"] = status["cover"]

        # ── 前瞻当晚的 act_id 探测（只对「没有官方 miyolive」的游戏）──
        # 星铁/绝区零官方从未挂过 act_id，
        # 但「将来会不会开」值得每场前瞻花 1 次请求盯着。触发信号只能用我们自己能拿到的：
        # 官方 B站直播间「正在直播」或「标题变了」；同一个信号只探 1 次。
        if not meta.get("miyolive_uid") and live_info is not None:
            act_id = ""
            try:
                act_id = await self._probe_act_id(game, live_info, state)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[hoyo_codes] act_id 探测失败 [{game}]：{exc}")
            if act_id:
                try:
                    p_codes, p_expire, _pm = await self.source.miyolive_codes_by_act(act_id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[hoyo_codes] 探测到的 act_id 取码失败 [{game}] {act_id}：{exc}")
                    p_codes, p_expire = [], None
                for code in p_codes:
                    kind = "cn" if all("\u4e00" <= ch <= "\u9fff" for ch in code) else "ascii"
                    records.append(
                        {
                            "post_id": f"miyolive:{code}",
                            "title": "官方前瞻直播活动（探测命中）",
                            "uid": "official:miyolive",
                            "candidates": [core.Candidate(code, kind, "cn")],
                            "expire_at": p_expire,
                            # 同 miyolive：这是估算，社区明写的时间优先
                            "expire_estimated": True,
                        }
                    )
                if p_codes:
                    ok = True  # 官方源成功 ⇒ 这个游戏算「抓到过」

        # ── 学习「谁在发兑换码」，供下轮盯人 ──
        self._learn_authors(game, candidates, watch_limit)

        # ⚠️ 「健康」判据：抓到帖子但**正文全空**说明米游社改了字段，
        # 这时不能算抓取成功，否则会建出空基线 → 下一轮把老码全推出去。
        # 放在最后算，这样单靠官方源成功也能算健康。
        posts_with_text = sum(1 for p in candidates if str(p.get("text") or "").strip())
        healthy = (ok or official_ok) and (not candidates or posts_with_text > 0)
        return core.collect_codes(records, game), healthy, live_info

    async def _probe_act_id(self, game: str, live_info: dict[str, Any], state: dict[str, Any]) -> str:
        """**前瞻当晚的 1 次 act_id 探测**（星铁/绝区零专用，见 `MiyousheSource.probe_act_id`）。

        星铁/绝区零**没有 `live.start`**（没有 act_id 就没有官方开播时间），所以触发信号只能
        用我们自己能拿到的两个：官方 B站直播间「**正在直播**」或「**标题变了**」。
        同一个信号**只探 1 次**；命中过的 `act_id` 会被记住并复用（直播期间每轮继续取码）。
        """
        probe = state.setdefault("probe", {})
        if not self._cfg("enable_act_id_probe"):
            # 开关关掉 = **连缓存里已命中的 act_id 也不再取码**（原来开关只挡探测，
            # 缓存命中后仍会每轮取码 ⇒ 关不掉）
            return ""
        title = str(live_info.get("title") or "")
        fp = f"{title}|{'live' if live_info.get('living') else 'idle'}"
        prev = probe.get(game) or {}
        if prev.get("fp") == fp:
            return str(prev.get("act_id") or "")  # 这个信号已经探过（命中过就继续复用）
        act_id = ""
        if self._cfg("enable_act_id_probe"):
            logger.info(f"[hoyo_codes] act_id 探测触发 [{game}] 信号={fp!r}")
            act_id = await self.source.probe_act_id(game)
        probe[game] = {"fp": fp, "act_id": act_id, "at": core.now_str()}
        if not act_id:
            logger.info(f"[hoyo_codes] act_id 探测未命中 [{game}]（符合预期：官方没开米游社直播间）")
        return act_id

    @staticmethod
    def _filter_by_game(posts: list[dict], game: str) -> list[dict]:
        """按米游社的**权威归属字段** `post.game_id` 过滤 —— 只留属于本游戏的帖。

        为什么必须做（2026-09-20 星铁 4.6 前瞻当晚的真事故）：
        为了拿到刚发布的帖，搜索必须传 `order_type=2`（时间倒序）；但米游社搜索是
        **分词宽松匹配**，时间倒序会把"今天所有含『兑换码』的帖"全倒出来 —— 实测用
        「**原神**兑换码」搜出来的 20 条**全是星铁的**（当晚星铁前瞻刷屏）。
        少了这道闸，**原神/绝区零订阅群会收到星铁的兑换码**（比"查不到"严重得多）。

        为什么不用"标题/正文里有没有游戏名"这种文本判据：
        当晚星铁前瞻公布了《绝区零》4.8 联动 ⇒ 大量**星铁帖**正文都含"绝区零"三个字，
        文本判据实测被击穿。`game_id` 是平台自己给帖子打的标，才是硬判据。

        降级策略：若本批响应里**一条都没带 `game_id`**（上游改了字段），则原样返回并打警告
        —— 宁可偶发串味，也不要让整条抓取链路静默失效。
        """
        gids = core.GAMES.get(game, {}).get("gids")
        if not gids:
            return posts
        if not posts:
            # 空列表直接返回，别刷「缺 game_id」的警告 ——
            # `user_posts` 每轮返回空（没盯到人）都会打一条，纯噪音。
            return posts
        tagged = [p for p in posts if p.get("game_id") is not None]
        if not tagged:
            logger.warning(f"[hoyo_codes] 搜索结果缺 game_id，本轮跳过归属过滤 [{game}]")
            return posts
        kept: list[dict] = []
        for p in tagged:
            try:
                if int(p["game_id"]) == int(gids):
                    kept.append(p)
            except (TypeError, ValueError):
                continue
        dropped = len(tagged) - len(kept)
        if dropped:
            logger.info(
                f"[hoyo_codes] 归属过滤 [{game}]：丢弃 {dropped} 条非本游戏帖（game_id != {gids}）"
            )
        return kept

    @staticmethod
    def _merge_posts(posts: list[dict], out: list[dict], seen_ids: set[str], cutoff: float) -> None:
        for p in posts:
            pid = p.get("post_id")
            if not pid or pid in seen_ids:
                continue
            if cutoff and p.get("created_at") and p["created_at"] < cutoff:
                continue
            seen_ids.add(pid)
            out.append(p)

    def _learn_authors(self, game: str, posts: list[dict], limit: int) -> None:
        """把「在发兑换码的作者」记进状态，下一轮盯人用。"""
        if limit <= 0 or self._state is None:
            return
        watched = self._state.setdefault("watched", {}).setdefault(game, [])
        for p in posts:
            uid = str(p.get("uid") or "")
            if not uid or uid in watched:
                continue
            head = str(p.get("subject") or "") + str(p.get("text") or "")[:200]
            if "兑换码" not in head:
                continue
            watched.append(uid)
        if len(watched) > limit:
            del watched[: len(watched) - limit]

    # ── 异环（NTE）：TapTap 链路 ────────────────────────────────────────────
    def _nte_source(self) -> TapTapSource:
        if self._nte is None:
            try:
                interval = float(self.conf.get("request_interval_seconds") or 1.2)
            except (TypeError, ValueError):
                interval = 1.2
            self._nte = TapTapSource(interval=interval)
        return self._nte

    async def _collect_nte(
        self, game: str
    ) -> tuple[list[core.CodeHit], bool, dict[str, Any] | None]:
        """异环：TapTap 列表页（发现）→ 详情页（取码）→ 与米哈游**共用聚合与分层**。

        请求预算：每轮 = 2 次列表（默认 tab + 官方 tab）+ 最多 `max_posts_per_game` 次详情。
        ⚠️ TapTap 列表页**每页只覆盖约 4~5 小时**（社区发帖量决定）⇒ 轮询间隔必须 < 4 小时
        （插件默认 60 分钟 ✓）；`post_max_age_days` 只用来挡历史帖。
        """
        max_age = self._cfg_int("post_max_age_days", 7, minimum=1)
        cutoff = time.time() - max_age * 86400
        max_posts = max(1, self._cfg_int("max_posts_per_game", 4, minimum=1))
        src = self._nte_source()
        ok = False

        cards: list[dict] = []
        official_cards: list[dict] = []
        try:
            cards = await src.list_posts()
            ok = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hoyo_codes] 异环列表页失败：{exc}")
        try:
            official_cards = await src.list_posts(official=True)
            ok = True
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hoyo_codes] 异环官方 tab 失败：{exc}")

        merged: dict[str, dict] = {}
        for c in official_cards + cards:
            pid = str(c.get("post_id") or "")
            if pid and pid not in merged:
                merged[pid] = c
        if not merged:
            # 可观测性：一条都没解析出来 = TapTap 大概改版了，别再默默"抓到 0 个码"
            logger.warning("[hoyo_codes] 异环列表页解析出 0 条帖子（TapTap 改版了？）")
        fresh = [c for c in merged.values()
                 if not c.get("created_at") or c["created_at"] >= cutoff]
        picked = [c for c in fresh if nte_core.is_interesting(c)][:max_posts]

        posts: list[dict] = []
        detail_budget = max_posts
        for c in picked:
            title = str(c.get("title") or "")
            summary = str(c.get("summary") or "")
            text = summary
            # ⚠️ **摘要里提到码也要拉详情页**：列表页摘要会被截断/粘连（实测少一个码
            # `YHDOUYIN0924`），详情页才是完整正文 —— 漏一个码比多花一次请求严重得多。
            if detail_budget > 0:
                detail_budget -= 1
                try:
                    detail = await src.post_detail(str(c["post_id"]))
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[hoyo_codes] 异环详情页失败 {c['post_id']}：{exc}")
                    detail = None
                if detail and detail.get("text"):
                    # 两个来源都留着提码（摘要常有、正文更全）
                    text = f"{summary}\n{detail['text']}" if summary else str(detail["text"])
                    title = title or str(detail.get("title") or "")
                    if not c.get("author_uid"):
                        c["author_uid"] = str(detail.get("author_uid") or "")
            posts.append({**c, "title": title, "text": text})

        # ── 前瞻日程：官方帖免登录就写着「将于 X年X月X日 HH:MM 正式播出」 ──
        live_info: dict[str, Any] | None = None
        for c in official_cards:
            title = str(c.get("title") or "")
            if not core.is_live_title(title):
                continue
            # ⚠️ 标题和时间分别在不同行，要并成一行再提（extract_live_start 是逐行扫的）
            start = core.extract_live_start(f"{title} {c.get('summary') or ''}")
            live_info = {"title": core.truncate(title, 40), "start": start, "room": ""}
            break

        records = nte_core.build_records(posts)
        posts_with_text = sum(1 for p in posts if str(p.get("text") or "").strip())
        healthy = ok and (not picked or posts_with_text > 0)
        return core.collect_codes(records, game), healthy, live_info

    async def _collect_all(
        self,
    ) -> dict[str, tuple[list[core.CodeHit], bool, dict[str, Any] | None]]:
        """按游戏抓取，**保留每个游戏的成败**（调用方靠它决定能不能建基线）。"""
        results: dict[str, tuple[list[core.CodeHit], bool, dict[str, Any] | None]] = {}
        for game in self._enabled_games():
            try:
                results[game] = await self._collect(game)
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[hoyo_codes] 抓取 {game} 失败：{exc}")
                results[game] = ([], False, None)
        return results

    # ── 推送 ──────────────────────────────────────────────────────────────
    async def _targets(self, state: dict[str, Any], *, kind: str = "codes") -> dict[str, list[str]]:
        """返回 `{umo: games}`；**games 为空列表 = 订阅全部游戏**。

        `kind="codes"` ⇒ 要收**新码推送**的会话；`kind="live"` ⇒ 要收**前瞻直播提醒**的会话。
        ⚠️ 这两类现在可以**分开订阅**（`/订阅兑换码` 与 `/订阅前瞻`），所以这里必须按需要过滤 ——
        以前只有一个 subscribers 列表，前瞻提醒是"跟着兑换码订阅走"的，
        后来补上独立的「订阅前瞻」订阅，才拆成两类。

        兼容三种历史格式（见 `_norm_sub`）：
          · 旧：`subscribers: ["umo1", "umo2"]`（全订）
          · 中：`subscribers: [{"umo": "...", "games": ["genshin"]}]`（缺 codes/live 时默认都为 True）
          · 新：多带 `codes` / `live` 两个布尔
        """
        out: dict[str, list[str]] = {}
        for umo in self._cfg_list("push_targets", []):  # 配置里的固定目标：两类都收
            u = str(umo).strip()
            if u:
                out.setdefault(u, [])
        for item in state.get("subscribers") or []:
            sub = _norm_sub(item)
            if sub is None or not sub[kind]:
                continue
            out[sub["umo"]] = sub["games"]  # 同一会话只保留一份订阅（后写的覆盖）
        return out

    @staticmethod
    def _update_sub(
        state: dict[str, Any],
        umo: str,
        *,
        games: list[str] | None = None,
        codes: bool | None = None,
        live: bool | None = None,
    ) -> dict[str, Any]:
        """新建/更新本会话的订阅记录（就地改 `state`），返回归一化后的那条。

        只传想改的字段；`games=None` 表示不动游戏偏好。两样通知都关掉时**整条移除**。
        """
        rest: list[dict[str, Any]] = []
        cur: dict[str, Any] | None = None
        for item in state.get("subscribers") or []:
            sub = _norm_sub(item)
            if sub is None:
                continue
            if sub["umo"] == umo:
                cur = sub
            else:
                rest.append(sub)
        if cur is None:
            cur = {"umo": umo, "games": [], "codes": False, "live": False}
        if games is not None:
            cur["games"] = [g for g in games if g in core.GAMES]
        if codes is not None:
            cur["codes"] = bool(codes)
        if live is not None:
            cur["live"] = bool(live)
        if cur["codes"] or cur["live"]:
            rest.append(cur)
            state["subscribers"] = rest
            return cur
        state["subscribers"] = rest
        return {}

    async def _push(self, text: str, umo: str) -> bool:
        """推给**一个**会话，返回是否真的送达。

        ⚠️ 平台/会话不存在时 `Context.send_message` 是**返回 False 而不抛异常**
        （`astrbot/core/star/context.py:673-677`）—— 必须检查返回值。
        """
        try:
            sent = await self.context.send_message(umo, MessageChain([Plain(text)]))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hoyo_codes] 推送到 {umo} 异常：{exc}")
            return False
        if sent is False:
            logger.warning(f"[hoyo_codes] 推送到 {umo} 未送达（平台/会话不存在？）")
            return False
        logger.info(f"[hoyo_codes] 已推送消息到 {umo}")
        return True

    async def _push_live_notice(
        self, notice: list[dict[str, Any]], text: str, umo: str
    ) -> bool:
        """推送前瞻提醒：**直播间封面大图 + 文字（含直播间链接）**。

        ⚠️ 为什么不是"分享卡片"（2026-09-16 实测，代价是往群里刷了 5 条重复消息）：
        QQ 对**机器人自造卡片**有投递风控 ——
          · `com.tencent.structmsg` + 公开样本 appid ⇒ 显示「发送者版本过低，无法展示内容」；
          · `xml` 段 ⇒ NapCat 直接拒（`retcode=1200 消息体无法解析`）；
          · **逐字段照抄真人分享的真样本**（`com.tencent.tuwen.lua` + B站官方 appid 100951776
            + `config.token`）⇒ NapCat 报发送成功，但**群里一条都看不到**
            （`token`/`msg_seq` 是 QQ 给真人客户端签发的，机器人伪造不了）。
        ⇒ 改用 **HarukaBot 同款**「封面图 + 文字链接」：观感接近卡片，且一定投递得到。
        """
        parts: list[Any] = []
        cover = next((str(it.get("cover") or "") for it in notice if it.get("cover")), "")
        if cover:
            try:
                parts.append(Image(file=cover))
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[hoyo_codes] 封面图构造失败（降级为纯文本）：{exc}")
        parts.append(Plain(text))
        try:
            sent = await self.context.send_message(umo, MessageChain(parts))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hoyo_codes] 前瞻提醒推送到 {umo} 异常：{exc}")
            return False
        if sent is False:
            # ⚠️ 返回 False 只说明**没拿到成功回执**，不等于没发出去
            # （2026-09-16 实测：NapCat 发卡片超时会误报 False，但消息其实已送达）。
            logger.warning(f"[hoyo_codes] 前瞻提醒推送到 {umo} 未确认送达")
            return False
        logger.info(f"[hoyo_codes] 已推送前瞻提醒（图+链接）到 {umo}")
        return True

    async def _bot_identity(self, umo: str) -> tuple[str, str]:
        """拿机器人自己的 QQ 号与昵称 —— 合并转发卡片上要显示发送者。

        ⚠️ 两个坑（实测发现，都已修）：
          · **失败结果绝不能进缓存** —— 否则一次偶发失败就让 uin 永久变成 `"0"`，
            平台恢复也不重试、只能重载插件（不可自愈）；
          · 缓存必须**按平台维度**存 —— 单槽全局缓存会让多平台（QQ + Telegram 等）
            互相串号，卡片上的发送者挂错。
        """
        uin = str(self._cfg("forward_bot_uin") or "").strip()
        name = str(self._cfg("forward_bot_name") or "兑换码播报").strip()
        if uin:  # 配置里写死了就不再问平台
            return uin, name
        platform_id = str(umo).split(":")[0]
        cached = self._bot_identity_cache.get(platform_id)
        if cached is not None:
            return cached
        try:
            manager = getattr(self.context, "platform_manager", None)
            for platform in getattr(manager, "platform_insts", []) or []:
                if platform.meta().id != platform_id:
                    continue
                bot = getattr(platform, "bot", None)
                if bot is None:
                    continue
                info = await bot.call_action("get_login_info")
                got_uin = str(info.get("user_id") or "")
                got_name = str(info.get("nickname") or name)
                if got_uin:  # ✅ 只有**真的取到**才缓存
                    self._bot_identity_cache[platform_id] = (got_uin, got_name)
                    return got_uin, got_name
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hoyo_codes] 取机器人自身信息失败（将用默认显示名）：{exc}")
        # ⚠️ 失败**不缓存** —— 下一轮还会重试
        return "0", name

    async def _make_nodes(self, messages: list[str], umo: str):
        """把消息列表打包成合并转发用的 `Node` 列表；不可用时返回 None（调用方降级纯文本）。"""
        if Node is None or Nodes is None or not self._cfg("push_as_forward"):
            return None
        try:
            uin, name = await self._bot_identity(umo)
            return [Node(uin=uin, name=name, content=[Plain(m)]) for m in messages]
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hoyo_codes] 构造合并转发失败（{exc}），将降级纯文本")
            return None

    async def _push_hits(self, hits: list[core.CodeHit], umo: str) -> bool:
        """把一个会话该收的码推过去。

        优先用**合并转发卡片**（标题一条、**每个兑换码单独一条**、来源一条）——
        设计目标：「方便直接复制兑换码到游戏里粘贴」；
        合并转发失败就**降级成一条纯文本**，保证码一定送得到。
        """
        nodes = await self._make_nodes(core.render_push_messages(hits), umo)
        if nodes is not None:
            try:
                sent = await self.context.send_message(umo, MessageChain([Nodes(nodes)]))
                if sent is not False:
                    logger.info(f"[hoyo_codes] 已推送（合并转发 {len(nodes)} 条）到 {umo}")
                    return True
                logger.warning(f"[hoyo_codes] 合并转发未送达 {umo}，降级为纯文本")
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[hoyo_codes] 合并转发异常（{exc}），降级为纯文本")
        return await self._push(core.render_new_codes_message(hits), umo)

    # ── 主检查流程 ────────────────────────────────────────────────────────
    async def check_once(self, *, push: bool = True, log: bool = True) -> dict[str, Any]:
        """跑一轮检查。返回本轮摘要（供命令与测试复用）。"""
        async with self._fetch_lock:
            results = await self._collect_all()  # 抓取在另一种锁里，别阻塞命令
        async with self._state_lock:
            state = await self._load_state()
            seen = state.setdefault("seen", {})
            tombstones = state.setdefault("tombstones", {})
            delivered = state.setdefault("delivered", {})
            baseline_games = list(state.get("baseline_games") or [])
            state["last_check"] = core.now_str()
            state["checks"] = int(state.get("checks") or 0) + 1
            state["last_games"] = {g: ("ok" if ok else "fail") for g, (_h, ok, _l) in results.items()}

            hits: list[core.CodeHit] = []
            new_hits: list[core.CodeHit] = []
            deferred: list[str] = []
            newly_baselined: list[str] = []
            lives: dict[str, dict[str, Any]] = {}
            for game, (game_hits, ok, live) in results.items():
                # ⚠️ **每个游戏独立 try**（纵深防御）：账本里**一条**坏数据曾让这里抛异常，
                # 而它在逐游戏循环内 ⇒ **后面的游戏整轮被跳过**（实测原神的真码一次都没推）。
                # 即便某个游戏的数据有问题，也绝不允许影响其它游戏。
                try:
                    if live:
                        lives[game] = live
                    hits.extend(game_hits)
                    if game not in baseline_games:
                        # ⚠️ 基线**按游戏**记：只有真正抓成功的游戏才置位
                        if not ok:
                            deferred.append(game)
                            continue
                        # ⚠️ 基线轮用 `create=True` 是**刻意取舍**：必须把该轮所有码都记进账本，
                        # 否则上线首轮会把"历史遗留的码"全当成新码刷屏。代价 = 基线轮里那些 C 级码
                        # 将来即使升到 A 级也**不会被推**（与本轮 `create=False` 想避免的漏推同机理，
                        # 只是发生在首轮）—— 这是明确接受的取舍，不是缺陷。
                        core.mark_seen(seen, game_hits)
                        baseline_games.append(game)
                        newly_baselined.append(game)
                        if log:
                            logger.info(f"[hoyo_codes] {game} 建立基线 {len(game_hits)} 个码（不推送）")
                        continue
                    new_hits.extend(core.diff_new_codes(seen, game_hits, tombstones=tombstones))
                    # ⚠️ 判新之后，顺手**刷新已记账码的元数据**（有效期 / 作者 / 档位），
                    # 但 **create=False ⇒ 绝不新增条目**（2026-09-20 加）。
                    # 为什么：平时只对 new_hits 调 mark_seen ⇒ **存量账本永远不刷新** ——
                    # 星铁 4.6 那 3 个码是旧逻辑记的（expire 为空），修好「有效期写在标题里也能提到」
                    # 之后查询里依旧是「有效期未知」，等于白修。查询路径 `_query_hits` 又是只读的。
                    core.mark_seen(seen, game_hits, create=False)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(f"[hoyo_codes] 处理 {game} 的本轮结果失败（跳过该游戏）：{exc}")

            state["baseline_games"] = baseline_games
            if log and deferred:
                logger.info(f"[hoyo_codes] 首轮抓取失败，这些游戏的基线推迟：{deferred}")

            # 只推可信档（A：≥2 位作者或官方源；B：单作者但标题带版本号且同帖多候选）+ 未过期
            allowed = {str(t).upper() for t in self._cfg_list("push_tiers", ["A", "B"])}
            now_dt = datetime.now(core.TZ)
            new_hits = [h for h in new_hits if h.tier in allowed]
            # 🩸 **硬闸**（「**社区来源的必须 ≥2 个**（作者）」）：
            # 即便配置里留着 B 档，**社区单作者**也一律不推。实测「≥2 位不同作者」是
            # **唯一零误报档**（`collect_codes()` 的注释里当年就是这么写的）；保留 B 档的代价已经实付过：
            # 2026-09-21 01:35:43 就是 B 档单作者把「冬季与绝区零进行冬季联动」这种**说明句**推进了群。
            # 代价评估：前瞻码很快就有人转发（实测星铁 4.6 那 3 个码有 4~7 位作者），
            # 「等第二位作者」通常只差几分钟。
            # ⚠️ **异环例外**（「**给异环单独放宽**」）：
            # 异环不在米游社体系、走 TapTap 列表页（每页只覆盖 4~5 小时），社区发帖量小
            # ⇒ **单作者是常态**，也要求 ≥2 会大面积漏推；且它的码是**英文**
            # （`extract_nte_codes`），不存在"中文说明句被当码"那类误报风险。
            new_hits = [
                h for h in new_hits if h.game == "nte" or h.official or h.authors >= 2
            ]
            new_hits = [
                h for h in new_hits if not (h.expire_at is not None and h.expire_at <= now_dt)
            ]

            pushed = False
            if new_hits and push:
                targets = await self._targets(state)
                if not targets:
                    logger.info("[hoyo_codes] 没有订阅者，本轮只记账不推送")
                    core.mark_seen(seen, new_hits)
                    state["pushed"] = int(state.get("pushed") or 0) + len(new_hits)
                    pushed = True
                else:
                    # 按目标补推：只给「还没收到这个码」的目标发；
                    # 订阅里指定了游戏的会话，只收那些游戏的码
                    for umo, games in targets.items():
                        missing = [
                            h
                            for h in new_hits
                            if (not games or h.game in games)
                            and umo not in (delivered.get(h.code.lower()) or [])
                        ]
                        if not missing:
                            continue
                        if await self._push_hits(missing, umo):
                            for h in missing:
                                lst = delivered.setdefault(h.code.lower(), [])
                                if umo not in lst:
                                    lst.append(umo)
                    # ⚠️ 只有**该收的目标都送达**才记账，否则下轮补推。
                    # 注意：没订这个游戏的会话不算"该收"，不能因为它而卡住记账
                    all_done = True
                    for h in new_hits:
                        for umo, games in targets.items():
                            if games and h.game not in games:
                                continue
                            if umo not in (delivered.get(h.code.lower()) or []):
                                all_done = False
                                break
                        if not all_done:
                            break
                    if all_done:
                        core.mark_seen(seen, new_hits)
                        state["pushed"] = int(state.get("pushed") or 0) + len(new_hits)
                        for h in new_hits:
                            delivered.pop(h.code.lower(), None)  # 全送达后清投递记录
                        pushed = True
                    else:
                        logger.warning("[hoyo_codes] 有目标未送达，本轮不记账，下轮补推")
            elif new_hits and not push:
                core.mark_seen(seen, new_hits)

            # ── 前瞻直播提醒（**每个前瞻事件只发一条**，临近开播时才发）──
            if self._cfg("notify_live") and lives and push:
                live_seen = state.setdefault("live_seen", {})
                rooms = self._cfg_dict("bili_live_rooms")
                notice: list[dict[str, Any]] = []
                for game, info in lives.items():
                    start = info.get("start")
                    start_iso = start.isoformat() if start else ""
                    fp = f"{info.get('title') or ''}|{start_iso}"
                    prev = live_seen.get(game) or {}
                    if prev.get("fp") == fp and prev.get("noticed"):
                        continue  # 这个前瞻已经报过了
                    if start is not None:
                        secs = (start - now_dt).total_seconds()
                        if secs > self._cfg_int("live_notice_minutes", 60) * 60:
                            continue  # 还早，等进窗口再报
                        if secs < -3 * 3600:
                            continue  # 结束太久，没必要报
                    elif not info.get("living"):
                        # ⚠️ 没有精确开播时间时（星铁/绝区零只能用**常设直播间**的标题当信号），
                        # **只有「正在直播」才算数**。
                        # 2026-09-16 实测：这些直播间的标题会**滞后一整个版本**
                        # （星铁 4.5 早已上线，标题仍是「《崩坏：星穹铁道》4.5版本前瞻特别节目」），
                        # 不加这道闸，插件一启用就会误报一条"过期"的前瞻提醒。
                        continue
                    notice.append(
                        {
                            "game": game,
                            "title": info.get("title") or "",
                            "start": start,
                            "room": str(info.get("room") or rooms.get(game) or ""),
                            # 封面图：推送时当"大图"发（见 `_push_live_notice`）
                            "cover": str(info.get("cover") or ""),
                        }
                    )
                if notice:
                    text = core.render_live_notice(notice, now=now_dt, rooms=rooms)
                    sent_any = False
                    for umo, games in (await self._targets(state, kind="live")).items():
                        if games and not any(it["game"] in games for it in notice):
                            continue  # 这个会话没订这些游戏
                        if await self._push_live_notice(notice, text, umo):
                            sent_any = True
                    if sent_any:
                        for it in notice:
                            st = it.get("start")
                            live_seen[it["game"]] = {
                                "fp": f"{it['title']}|{st.isoformat() if st else ''}",
                                "noticed": True,
                            }
                        logger.info(
                            f"[hoyo_codes] 已推送前瞻直播提醒：{[it['game'] for it in notice]}"
                        )

            removed = core.prune_seen(
                seen,
                retention_days=self._cfg_int("retention_days", 180),
                tombstones=tombstones,
            )
            await self._save_state(state)
            if log and (new_hits or removed):
                logger.info(
                    f"[hoyo_codes] 本轮新码 {len(new_hits)} 个（推送={pushed}），清理超期 {len(removed)} 个"
                )
            return {
                "baseline": bool(deferred or newly_baselined),
                "hits": hits,
                "new": new_hits,
                "pushed": pushed,
                "pruned": removed,
                "deferred": deferred,
                "baselined": newly_baselined,
                # 供 `_next_interval()` 判断「要不要为临近的开播加密轮询」
                "lives": lives,
            }

    @staticmethod
    def _game_args(event: AstrMessageEvent, first: str) -> list[str]:
        """把命令后面**所有**游戏参数都解析出来（支持多游戏）。

        ⚠️ AstrBot 只会把**第一个 token** 当参数传给 handler（`CommandTokens.get(0)`，
        见 `core/utils/command_parser.py`），所以 `/订阅前瞻 原神 星穹铁道 崩坏三` 里的
        后两个得从 `event.message_str` 自己捞 —— 这样也不依赖框架的参数绑定细节。
        """
        raw = str(getattr(event, "message_str", "") or "")
        raw = re.sub(
            r"^[/!！\s]*(?:订阅前瞻|退订前瞻|订阅兑换码|退订兑换码|兑换码|codes)\s*",
            "", raw, flags=re.IGNORECASE,
        ).strip()
        return core.resolve_games(f"{first} {raw}".strip())

    async def _query_hits(
        self, games: list[str] | None
    ) -> tuple[dict[str, list[core.CodeHit]], list[str]]:
        """查询用的抓取：返回 `({game: hits}, 失败信息列表)`。

        `games=None/空` = 所有启用的游戏；**支持一次查多个**（`/兑换码 原神 崩铁`）。
        抽出来是为了让「合并转发」和「纯文本降级」两条路复用同一次抓取（别抓两遍）。
        """
        picked = [g for g in (games or []) if g in core.GAMES] or self._enabled_games()
        data: dict[str, list[core.CodeHit]] = {}
        errs: list[str] = []
        async with self._fetch_lock:  # 命令路径也要和后台轮询互斥
            for g in picked:
                try:
                    hits, _ok, _live = await self._collect(g)
                except Exception as exc:  # noqa: BLE001
                    errs.append(f"【{core.GAMES[g]['name']}】查询失败：{exc}")
                    continue
                data[g] = hits
        # ⚠️ 实时抓取之外，再用**本地已记录的码**兜底：
        # 短时效窗口的数据源（异环 TapTap 列表页只覆盖 4~5 小时）一翻页就什么都抓不到，
        # 于是出现「插件明明记着 5 个码、查询却说没有」的怪象 ⇒「我知道的 ≠ 我此刻能看到的」。
        # 合并规则：实时优先、按码去重、**明确已过期的历史码淘汰**（见 core.merge_query_hits）。
        try:
            async with self._state_lock:
                state = await self._load_state()
            seen = state.get("seen") or {}
            for g in picked:
                if g in data:
                    data[g] = core.merge_query_hits(data[g], core.hits_from_seen(seen, g))
        except Exception as exc:  # noqa: BLE001
            logger.warning(f"[hoyo_codes] 合并本地记录失败（只给实时结果）：{exc}")
        return data, errs

    async def _query(self, games: list[str] | None) -> str:
        """纯文本版查询（合并转发不可用时的降级路径；容器内验收脚本也用它）。"""
        data, errs = await self._query_hits(games)
        return _render_query_text(
            data, errs, show_expired=bool(self._cfg("show_expired_codes"))
        )

    # ── 生命周期 ──────────────────────────────────────────────────────────
    async def _ensure_task(self) -> None:
        """幂等地保证后台轮询在跑（插件热重载后 initialize 会重新构造实例）。"""
        if self._task and not self._task.done():
            return
        self._task = asyncio.create_task(self._loop())
        logger.info("[hoyo_codes] 后台轮询已启动")

    async def initialize(self) -> None:
        """AstrBot 加载/重载插件时调用 —— 官方指定的起后台任务的位置。

        ⚠️ 不要用 `on_astrbot_loaded` 起后台任务：那个钩子**只在进程启动时触发一次**，
        WebUI 热重载插件不会重跑，任务会静默消失（源码见 star_manager.py:1416）。
        """
        await self._ensure_task()

    @filter.on_astrbot_loaded()
    async def _on_loaded(self) -> None:
        """进程启动时的一次性钩子（兜底：万一某个版本没走到 initialize）。"""
        await self._ensure_task()

    def _next_interval(self, base: int, result: dict[str, Any]) -> int:
        """算下一次轮询该等多久 —— **动态轮询**。

        平时 = `check_interval_minutes`（默认 60 分钟）。但只要某个游戏**已知开播时间**
        且还在 `live_fast_window_minutes`（默认 60 分钟）窗口内，就缩到
        `live_fast_poll_minutes`（默认 5 分钟）—— 这样「前瞻提醒」能**准点**发出，
        而不用把一整天的轮询都加密。

        ⚠️ **已经提醒过的那个前瞻不再加密**（`state.live_seen` 里 `noticed=True`），
        所以提醒一发出就自动恢复常规间隔，不会一直高频查。
        """
        lives = result.get("lives") if isinstance(result, dict) else None
        if not isinstance(lives, dict) or not lives:
            return base
        fast = max(60, self._cfg_int("live_fast_poll_minutes", 5, minimum=1) * 60)
        window = max(60, self._cfg_int("live_fast_window_minutes", 60, minimum=1) * 60)
        if fast >= base:  # 加密间隔不比常规小就没意义
            return base
        now_dt = datetime.now(core.TZ)
        seen = (self._state or {}).get("live_seen") or {}
        for game, info in lives.items():
            start = (info or {}).get("start")
            if not isinstance(start, datetime):
                continue  # 星铁/绝区零没有精确开播时间 ⇒ 谈不上"提前量"
            secs = (start - now_dt).total_seconds()
            if not (0 <= secs <= window):
                continue
            fp = f"{(info or {}).get('title') or ''}|{start.isoformat()}"
            prev = seen.get(game) or {}
            if prev.get("fp") == fp and prev.get("noticed"):
                continue  # 这个前瞻已经提醒过 ⇒ 不再加密
            logger.info(
                f"[hoyo_codes] 临近开播（{game} 还有 {int(secs // 60)} 分钟）⇒ "
                f"下一轮 {fast // 60} 分钟后再查"
            )
            return fast
        return base

    async def _loop(self) -> None:
        delay = max(5, self._cfg_int("startup_delay_seconds", 90))
        interval = max(5, self._cfg_int("check_interval_minutes", 60)) * 60
        try:
            await asyncio.sleep(delay)
            while True:
                result: dict[str, Any] = {}
                try:
                    result = await self.check_once(push=True)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    logger.error(f"[hoyo_codes] 轮询出错（下轮继续）：{exc}")
                    try:
                        state = await self._load_state()
                        state["last_error"] = f"{core.now_str()} :: {exc}"
                        await self._save_state(state)
                    except Exception:  # noqa: BLE001
                        pass
                # 动态间隔：临近已知开播时间时加密（见 `_next_interval`）
                # ⚠️ 必须包在 try 里：它一旦抛异常（如 naive datetime 相减），
                # 后台任务会**直接死掉且无人 await** ⇒ 插件静默停摆、此后再不检查任何游戏。
                # 算不出来就老老实实用常规间隔。
                try:
                    sleep_for = self._next_interval(interval, result)
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        f"[hoyo_codes] 计算下轮间隔失败（回退常规 {interval // 60} 分钟）：{exc}"
                    )
                    sleep_for = interval
                await asyncio.sleep(sleep_for)
        except asyncio.CancelledError:
            logger.info("[hoyo_codes] 后台轮询已停止")
            raise

    # ── 命令 ──────────────────────────────────────────────────────────────
    @filter.command("兑换码", alias={"codes"})
    async def cmd_codes(self, event: AstrMessageEvent, game: str = ""):
        """查询前瞻兑换码。用法：/兑换码 [游戏名]，如 原神 / 崩铁 / 绝区零 / 崩坏3 / 异环（不带 = 全部）"""
        key = core.resolve_game(game)
        if game and key is None:
            yield event.plain_result(f"未知游戏名，只认识：{core.GAME_HINT}（直接发 `/兑换码` 就是查全部）")
            return
        # 支持一次查多个（`/兑换码 原神 崩铁`）
        wanted = self._game_args(event, game)
        data, errs = await self._query_hits(wanted or None)
        if not data and not errs:
            yield event.plain_result("没有启用任何游戏，或者查询失败。")
            return
        # 用**合并转发**：每个码独占一条，长按单条就能只复制那个码
        show_expired = bool(self._cfg("show_expired_codes"))
        messages: list[str] = []
        for g, hits in data.items():
            messages.extend(
                core.render_codes_messages(
                    g, hits, header="当前兑换码", show_expired=show_expired
                )
            )
        messages.extend(errs)
        nodes = await self._make_nodes(messages, event.unified_msg_origin)
        if nodes is not None and callable(getattr(event, "chain_result", None)):
            try:
                yield event.chain_result([Nodes(nodes)])
                return
            except Exception as exc:  # noqa: BLE001
                logger.warning(f"[hoyo_codes] 查询走合并转发失败（{exc}），降级为纯文本")
        # 降级：退回**分段纯文本**（不是把卡片那些短行直接拼起来）
        yield event.plain_result(_render_query_text(data, errs, show_expired=show_expired))

    @filter.command("订阅兑换码")
    async def cmd_subscribe(self, event: AstrMessageEvent, game: str = ""):
        """订阅新兑换码推送。用法：`/订阅兑换码 [游戏名]`（不带游戏名 = 全部游戏）。"""
        umo = event.unified_msg_origin
        # 支持一次订多个（`/订阅兑换码 原神 崩铁`）
        wanted = self._game_args(event, game)
        if game and not wanted:
            yield event.plain_result(f"未知游戏名，只认识：{core.GAME_HINT}（直接发 `/兑换码` 就是查全部）")
            return
        games = wanted
        async with self._state_lock:  # 命令路径也必须走锁
            state = await self._load_state()
            self._update_sub(state, umo, games=games, codes=True)
            await self._save_state(state)
        scope = "、".join(core.GAMES[g]["name"] for g in games) if games else "全部游戏"
        yield event.plain_result(
            f"✅ 已订阅新码推送（{scope}）！发现新的国服前瞻兑换码时会推送到本会话。\n"
            f"（检查间隔 {self._cfg_int('check_interval_minutes', 60)} 分钟；"
            "发「/退订兑换码」完全退订，或「/退订兑换码 原神」只退一个游戏）\n"
            "💡 想收**前瞻直播提醒**另发一条 `/订阅前瞻`（这两类是独立订阅）"
        )

    @filter.command("退订兑换码")
    async def cmd_unsubscribe(self, event: AstrMessageEvent, game: str = ""):
        """退订新兑换码推送。用法：`/退订兑换码 [游戏名]`（不带游戏名 = 连前瞻提醒一起完全退订）。"""
        umo = event.unified_msg_origin
        # 支持一次退多个（`/退订兑换码 原神 崩铁`）
        wanted = self._game_args(event, game)
        if game and not wanted:
            yield event.plain_result(f"未知游戏名，只认识：{core.GAME_HINT}（直接发 `/兑换码` 就是查全部）")
            return
        async with self._state_lock:
            state = await self._load_state()
            cur: dict[str, Any] | None = None
            for item in state.get("subscribers") or []:
                sub = _norm_sub(item)
                if sub is not None and sub["umo"] == umo:
                    cur = sub
                    break
            if cur is None or not cur["codes"]:
                msg = "本会话本来就没订阅新码推送呀～"
            elif not wanted:
                # 不带游戏名 = **完全退订**（码 + 前瞻都关）—— 符合"我不要这个插件的通知了"的直觉
                self._update_sub(state, umo, codes=False, live=False)
                msg = "已退订（新码推送和前瞻提醒都不再发了）。"
            else:
                rest = (
                    [g for g in cur["games"] if g not in wanted]
                    if cur["games"]
                    else [g for g in core.GAMES if g not in wanted]
                )
                names = "、".join(core.GAMES[k]["name"] for k in wanted)
                self._update_sub(state, umo, games=rest, codes=bool(rest))
                msg = f"已退订「{names}」的新码推送（前瞻提醒不受影响）。"
            await self._save_state(state)
        yield event.plain_result(msg)

    @filter.command("订阅前瞻")
    async def cmd_subscribe_live(self, event: AstrMessageEvent, game: str = ""):
        """订阅「前瞻直播提醒」。用法：`/订阅前瞻 [游戏名]`（不带游戏名 = 保持现有游戏偏好）。"""
        umo = event.unified_msg_origin
        # 支持一次订多个（例如：`/订阅前瞻 原神 星穹铁道 崩坏三 绝区零`）
        wanted = self._game_args(event, game)
        if game and not wanted:
            yield event.plain_result(f"未知游戏名，只认识：{core.GAME_HINT}（直接发 `/兑换码` 就是查全部）")
            return
        async with self._state_lock:
            state = await self._load_state()
            # ⚠️ 不带游戏名时**不动**已有的游戏偏好（避免把「只订原神」悄悄放大成全部）
            self._update_sub(state, umo, games=(wanted or None), live=True)
            await self._save_state(state)
        scope = "、".join(core.GAMES[k]["name"] for k in wanted) if wanted else "本会话当前关注的游戏"
        yield event.plain_result(
            f"✅ 已订阅**前瞻直播提醒**（{scope}）！\n"
            f"临近开播（默认提前 {self._cfg_int('live_notice_minutes', 60)} 分钟）会提醒一次，"
            "每个前瞻只发一条、不刷屏；临近开播时插件会自动加密检查，提醒更准点。\n"
            "（想同时收新码推送，再发一条 `/订阅兑换码`；发 `/退订前瞻` 可单独关掉它）"
        )

    @filter.command("退订前瞻")
    async def cmd_unsubscribe_live(self, event: AstrMessageEvent, game: str = ""):
        """退订「前瞻直播提醒」（新码推送不受影响）。"""
        umo = event.unified_msg_origin
        async with self._state_lock:
            state = await self._load_state()
            cur: dict[str, Any] | None = None
            for item in state.get("subscribers") or []:
                sub = _norm_sub(item)
                if sub is not None and sub["umo"] == umo:
                    cur = sub
                    break
            if cur is None or not cur["live"]:
                msg = "本会话本来就没订阅前瞻提醒呀～"
            else:
                self._update_sub(state, umo, live=False)
                msg = "已关闭**前瞻直播提醒**（新码推送不受影响）。"
            await self._save_state(state)
        yield event.plain_result(msg)

    @filter.command("兑换码状态")
    async def cmd_status(self, event: AstrMessageEvent):
        """查看插件运行状态。"""
        async with self._state_lock:
            state = await self._load_state()
            snapshot = {
                "seen": dict(state.get("seen") or {}),
                "tombstones": dict(state.get("tombstones") or {}),
                "subscribers": list(state.get("subscribers") or []),
                "baseline_games": list(state.get("baseline_games") or []),
                "last_games": dict(state.get("last_games") or {}),
                "last_error": state.get("last_error") or "",
                "checks": state.get("checks", 0),
                "pushed": state.get("pushed", 0),
                "last_check": state.get("last_check") or "",
            }
        per_game = {core.GAMES[g]["name"]: len(snapshot["seen"].get(g, {})) for g in core.GAMES}
        base = [core.GAMES[g]["short"] for g in snapshot["baseline_games"] if g in core.GAMES]
        last = "、".join(
            f"{core.GAMES[g]['short']}{'✅' if v == 'ok' else '❌'}"
            for g, v in snapshot["last_games"].items()
            if g in core.GAMES
        )
        lines = [
            "🐳 兑换码插件状态",
            f"已记录码数：{core.seen_count(snapshot['seen'])}（"
            + "、".join(f"{k} {v}" for k, v in per_game.items())
            + "）",
            f"墓碑指纹：{sum(len(v) for v in snapshot['tombstones'].values())}",
            f"订阅会话：{len(snapshot['subscribers'])}"
            + (
                "（" + "、".join(_sub_label(s) for s in snapshot["subscribers"]) + "）"
                if snapshot["subscribers"]
                else ""
            ),
            f"检查次数：{snapshot['checks']}｜已推送码数：{snapshot['pushed']}",
            f"上次检查：{snapshot['last_check'] or '（尚未）'}",
            f"上次各游戏抓取：{last or '（尚未）'}",
            f"已建基线：{'、'.join(base) if base else '（无，下轮对抓取成功的游戏建立且不推送）'}",
            f"轮询间隔：{self._cfg_int('check_interval_minutes', 60)} 分钟"
            f"｜明细保留：{self._cfg_int('retention_days', 180)} 天",
        ]
        if snapshot["last_error"]:
            lines.append(f"⚠️ 上次错误：{core.truncate(str(snapshot['last_error']), 120)}")
        yield event.plain_result("\n".join(lines))

    @filter.command("兑换码自检")
    async def cmd_selftest(self, event: AstrMessageEvent):
        """立刻跑一轮真实抓取（不推送、不记账），用于排障。"""
        async with self._state_lock:
            state = await self._load_state()
            before = core.seen_count(state.get("seen", {}))
        async with self._fetch_lock:
            results = await self._collect_all()
        hits = [h for _g, (hs, _ok, _l) in results.items() for h in hs]
        by_game: dict[str, list[str]] = {}
        for h in hits:
            by_game.setdefault(core.GAMES[h.game]["name"], []).append(h.code)
        detail = "\n".join(f"【{k}】{'、'.join(v)}" for k, v in by_game.items()) or "（本轮没抓到候选码）"
        fail = [core.GAMES[g]["short"] for g, (_hs, ok, _l) in results.items() if g in core.GAMES and not ok]
        yield event.plain_result(
            "🔎 自检（只读，不推送不记账）\n"
            f"{detail}\n"
            f"当前已记录 {before} 个码，本轮候选 {len(hits)} 个"
            + (f"\n❌ 抓取失败的游戏：{'、'.join(fail)}" if fail else "")
        )

    async def terminate(self) -> None:
        if self._task and not self._task.done():
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        await self.source.close()
        if self._nte is not None:
            await self._nte.close()
        logger.info("[hoyo_codes] 插件已卸载")
