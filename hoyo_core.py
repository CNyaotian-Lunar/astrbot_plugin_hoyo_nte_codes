"""米哈游前瞻兑换码（国服）—— 纯逻辑核心。

规则来源：对 78 帖逐帖人工核对 + 实测。
六条硬结论直接决定实现：

1. 搜索响应（`data.posts[]`）**自带完整正文**，一条请求 = 20 帖全文；不要调 `getPostFull`
   （它有滑窗配额，51 秒内 30 次就被 `retcode=1034` 风控）。
2. ⭐ **国服码与国际服码形态完全一样**（星铁国服 `NEYSCGAWKE98` vs 国际服 `2TKRKAR6YG2X`），
   只能靠正文里的「国服:/国际服:」**分段文字**区分 ⇒ 必须做区域状态机，否则会把国际服码推给国服玩家。
3. 中文停用词**必须完全匹配，不能子串匹配** —— 星铁有真码就叫「兑换码记得换」。
4. ASCII 码 8~16 位且非纯数字；**不能额外要求含大写或数字** —— 有作者全小写转贴（`fxlknkwhvurc`）。
5. 噪音靠「锚点行（含'兑换'）+ 距锚点行窗口 + 中文连续成组」三重压制。
6. 可信度 = **不同作者数（uid）**：实测所有真码有 3~10 位作者、所有误报只有 1 位作者。
   ⚠️ **必须按 uid 去重，不能按帖数**（实测有同一位作者发 2 帖）。

本模块零 AstrBot 依赖，可离线自测。
"""

from __future__ import annotations

import hashlib
import json
import random
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

TZ = timezone(timedelta(hours=8))

# 米游社 Web 端公开 salt（社区广为使用；实测 searchPosts 其实不校验签名，带着更稳）
DS_SALT = "xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs"

# ── 游戏定义 ────────────────────────────────────────────────────────────────
# `source` 决定走哪条数据链路：`miyoushe`（米游社 searchPosts 那套）/ `nte`（异环专用链路）
#
# ⭐ `gids` = 米游社的**游戏/分区 ID**，是「这条帖属于哪个游戏」的**权威判据**。
#    2026-09-20 星铁 4.6 前瞻当晚实测：
#    · 搜索响应里**每条帖都自带 `post.game_id`**（星铁=6 / 原神=2 / 绝区零=8 / 崩坏3=1）；
#    · 前瞻当晚社区刷屏时，**纯关键词搜索会跨游戏串味** —— 用「原神兑换码」搜出来的
#      20 条全是星铁的帖（因为搜索是分词宽松匹配 + 我们改用了时间倒序 `order_type=2`）。
#    ⇒ 修复：抓取侧一律用 `game_id == gids` 做**硬归属过滤**，不靠"帖子里有没有游戏名"去猜。
#    对照表来源：UIGF-org/mihoyo-api-collect 的 ID 对照表（社区事实标准）。
GAMES: dict[str, dict[str, Any]] = {
    "genshin": {
        "name": "原神",
        "short": "原神",
        # 第 2 个关键词刻意放宽到「XX兑换码」：前瞻兑换码 是 兑换码 的子集，多一个角度提高召回
        "keywords": ["原神前瞻兑换码", "原神兑换码"],
        "miyolive_uid": 75276539,
        "gids": 2,
        "source": "miyoushe",
    },
    "starrail": {
        "name": "崩坏：星穹铁道",
        "short": "崩铁",
        "keywords": ["星穹铁道前瞻兑换码", "崩铁兑换码"],
        # ⚠️ 实测：星铁官方号的前瞻帖**从不带 act_id**（0/9），miyolive 对它恒为空 —— 别白费请求
        "miyolive_uid": None,
        "gids": 6,
        "source": "miyoushe",
        # 前瞻当晚「act_id 探测」用：命中的链接必须带 `game_biz=hkrpg_cn` 才认（防推错游戏）
        "game_biz": "hkrpg_cn",
    },
    "zzz": {
        "name": "绝区零",
        "short": "绝区零",
        "keywords": ["绝区零前瞻兑换码", "绝区零兑换码"],
        # ⚠️ 同理：绝区零官方号 0/4 带 act_id
        "miyolive_uid": None,
        "gids": 8,
        "source": "miyoushe",
        "game_biz": "nap_cn",
    },
    "hi3": {
        "name": "崩坏3",
        "short": "崩坏3",
        # ⚠️ 崩坏3 的前瞻码**基本不出现在社区帖正文里**（官方只在 miyolive 活动页发），
        # 所以社区搜索这两条关键词抓的是**非前瞻码**（新春问答这类）+ 兜底，
        # 真正的码走官方 miyolive 链路（miyolive_uid 见下）。
        "keywords": ["崩坏3前瞻兑换码", "崩坏3兑换码"],
        # 「爱酱小跟班」官方资讯号：它的《崩坏3》Vx.x 特别节目预告帖正文里带 act_id
        # （2026-09-16 实测：user_instant 拿到 act_id=ea202609101428456900 → index → refreshCode 通）
        "miyolive_uid": 73565430,
        "gids": 1,
        "source": "miyoushe",
    },
    "nte": {
        "name": "异环",
        "short": "异环",
        # 异环**不在米游社**（Hotta Studio / 完美世界）⇒ 不走关键词搜索，
        # 走 `source="nte"` 的 TapTap 链路（列表页发现 + 详情页取码），见 `nte_core.py`
        "keywords": [],
        "miyolive_uid": None,
        # 异环不在米游社体系 ⇒ 没有 gids（归属靠 TapTap 那条链路自己保证）
        "gids": None,
        "source": "nte",
    },
}

# 数据源 → 给人看的站点名。
# ⚠️ **异环不在米游社体系**（Hotta Studio / 完美世界，走 TapTap）⇒ 来源行**绝不能硬编码
# "米游社"**。已知缺陷：异环显示「来源：… · 米游社」是错的。
SITE_NAME: dict[str, str] = {
    "miyoushe": "米游社",
    "nte": "TapTap",
}

# 别名表：**精确匹配优先**，其次按「别名长度降序」做子串匹配（长别名更具体，避免「绝」这种
# 单字别名把「拒绝」也吃成绝区零 —— 见 resolve_game）。
GAME_ALIASES: dict[str, str] = {
    # 原神
    "原神": "genshin", "genshin": "genshin", "ys": "genshin",
    # 崩坏：星穹铁道 —— 需求：「星铁=崩铁=星穹铁道=崩坏：星穹铁道」全认
    "星穹铁道": "starrail", "崩坏星穹铁道": "starrail",
    "崩坏：星穹铁道": "starrail", "崩坏:星穹铁道": "starrail",
    "崩铁": "starrail", "星铁": "starrail", "穹铁道": "starrail",
    "starrail": "starrail", "sr": "starrail", "hkrpg": "starrail",
    # 绝区零
    "绝区零": "zzz", "zzz": "zzz", "绝": "zzz",
    # 崩坏3（含「崩三」「崩3」等口语叫法）
    "崩坏3rd": "hi3", "崩坏三": "hi3", "崩坏3": "hi3", "崩三": "hi3", "崩3": "hi3",
    "honkaiimpact3rd": "hi3", "honkai impact 3rd": "hi3", "hi3": "hi3", "bh3": "hi3", "bbb": "hi3",
    # 异环（Neverness to Everness）
    "neverness to everness": "nte", "neverness": "nte", "异环": "nte", "nte": "nte",
}

# 游戏名提示（命令里回给用户的那句话，**动态生成** —— 加游戏时不用再改文案）
GAME_HINT = " / ".join(meta["short"] for meta in GAMES.values())


def resolve_game(text: str) -> str | None:
    """把用户输入（中文名 / 别名）解析成 game key；无匹配返回 None（= 全部）。

    ⚠️ 先精确匹配，再**按别名长度降序**做子串匹配。旧版按字典插入顺序扫子串，
    「崩坏3rd」会被更早出现的短别名抢走；「绝」这种单字别名也可能误伤（如「拒绝」）。
    """
    t = (text or "").strip().lower()
    if not t:
        return None
    if t in GAME_ALIASES:
        return GAME_ALIASES[t]
    for alias in sorted(GAME_ALIASES, key=len, reverse=True):
        if alias and alias in t:
            return GAME_ALIASES[alias]
    return None


# 多个游戏名之间的分隔符（需求：「`/订阅前瞻 原神 星穹铁道 崩坏三 绝区零` 类似这种的泛匹配」）
GAME_SPLIT_RE = re.compile(r"[\s,，、+＋|/／;；·]+")


def resolve_games(text: str) -> list[str]:
    """把**多个**游戏名解析成 game key 列表（保序去重）。

    支持「原神 星穹铁道 崩坏三 绝区零」这种空格分隔，也支持 `、`/`,`/`+`/`|`/`/` 分隔。
    ① 整串精确命中 → 就那一个；② 否则按分隔符切开逐个解析；
    ③ 一个都没认出来时，退回**整体子串匹配**（兼容「崩铁的码」这类带后缀的说法）。
    """
    t = (text or "").strip()
    if not t:
        return []
    if t.lower() in GAME_ALIASES:
        return [GAME_ALIASES[t.lower()]]
    out: list[str] = []
    for part in GAME_SPLIT_RE.split(t):
        key = resolve_game(part)
        if key and key not in out:
            out.append(key)
    if out:
        return out
    whole = resolve_game(t)  # 例："崩铁的码" → starrail
    return [whole] if whole else []


# ── DS 签名 ─────────────────────────────────────────────────────────────────
def make_ds(query: str = "", body: str = "", *, t: int | None = None, r: int | None = None) -> str:
    """生成米游社 Web 接口的 DS 头：`<t>,<r>,<md5>`。"""
    ts = int(time.time()) if t is None else int(t)
    rnd = random.randint(100000, 200000) if r is None else int(r)
    raw = f"salt={DS_SALT}&t={ts}&r={rnd}&b={body}&q={query}"
    return f"{ts},{rnd},{hashlib.md5(raw.encode('utf-8')).hexdigest()}"


# ── 正文解析（Quill delta → 纯文本）────────────────────────────────────────
def delta_to_text(structured_content: str | None) -> str:
    """把米游社帖子的 structured_content（Quill delta JSON 字符串）转成纯文本。

    图片/视频等 dict insert **丢弃**（避免把图片 URL 里的 hash 当成码）；
    解析失败时退化为「剥 HTML 标签」。
    """
    if not structured_content:
        return ""
    try:
        blocks = json.loads(structured_content)
    except (ValueError, TypeError):
        return re.sub(r"<[^>]+>", " ", str(structured_content))
    if not isinstance(blocks, list):
        return ""
    return "".join(b.get("insert") for b in blocks if isinstance(b, dict) and isinstance(b.get("insert"), str))


# ── 提码 ────────────────────────────────────────────────────────────────────
URL_RE = re.compile(r"https?://[^\s\"'）)】\]]+")
EMOJI_RE = re.compile(r"_\([^)]{0,40}\)")  # 米游社表情 _(米游姬-撒花)
PLACEHOLDER_RE = re.compile(r"\[(?:图片|附件|视频|链接)\]")

# 区域标签（决定国服 / 国际服）
CN_TAG_RE = re.compile(r"^(国服|国内服|国区|国内|中国大陆|简中服)(?!国际)[^：:]{0,12}[：:]\s*(.*)$")
# 裸标签：**行首出现即切换**（实测发现有「国际服兑换码如下」这种无冒号写法，末尾加锚点会漏）
CN_BARE_RE = re.compile(r"^(国服|国内服|国区|国内|中国大陆|简中服)")
GL_TAG_RE = re.compile(r"^(国际服|海外服|国际版|国际区|国际服兑换码|Global|国际码)[^：:]{0,12}[：:]\s*(.*)$", re.I)
GL_BARE_RE = re.compile(r"^(国际服|海外服|国际版|国际区|国际服兑换码|Global|国际码)", re.I)

# token 形态
ASCII_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{8,16}$")
# ⚠️ 上限 15 → **10**（2026-09-21 修实发误报）：真中文码最长只有 9 字（原神语料：
# 「往冥府的安魂歌」8 / 「风仙薇斯纳为你效劳」9 / 「首席女高音沃雅妮莎」9），而 12~15 字全放进来后，
# 群里**真的**收到过两条说明句被当码推送（2026-09-21 01:35:43，tier=B 单作者）：
#   · `冬季与绝区零进行冬季联动`（12 字）
#   · `正常抽需要古老月华来兑换抽卡卷`（15 字）
CN_TOKEN_RE = re.compile(r"^[\u4e00-\u9fa5]{4,10}$")
VERSION_LIKE_RE = re.compile(r"^\d+(\.\d+)+$")

# 中文小标题黑名单：**完全匹配**（子串匹配会误杀「兑换码记得换」这种真码）
CN_STOP = {
    "兑换码", "兑换方法", "兑换方式", "兑换地址", "使用方法", "兑换途径", "兑换入口",
    "前瞻兑换码", "国服兑换码", "国际服兑换码", "前瞻直播", "前瞻节目", "前瞻总结",
    "卡池", "活动", "福利", "优化", "音乐", "周边", "联动信息", "总结", "简评", "简介",
    "全新活动", "全新角色", "全新武器", "全新地图", "新增活动", "新版本", "新内容",
    "抽卡", "更多", "其他", "本期", "注意事项", "有效期", "兑换时间", "详细内容",
    "领取方式", "参与方式", "更新内容", "角色卡池", "武器卡池", "光锥卡池",
    "上半", "下半", "新角色", "新武器", "新地图", "前瞻", "版本", "福利内容",
}
# 「XX上半 / XX下半」这类排版小标题
CN_STOP_RE = re.compile(r"^(?:祈愿|跃迁|卡池|独家频段|集录|角色|武器|光锥|常驻|限定)?[^，。！？、]{0,6}(?:上半|下半)$")
# 🩸 **说明句特征词**（2026-09-21 加）：中文码是**名词性词组**（角色名/成语/短句），
# 带这些连接词/时态词的多半是正文里的说明句。
# ⚠️ **只放"看到就基本确定是句子"的词**，且**必须避开真码的先例** ——
# 初版含 `记得`，当场撞挂了历史真码「**兑换码记得换**」（`selftest_core` 第 92 行那条回归），
# 连带把同帖的真码「这个是兑换码」也拖没了（连续成组被拆散）。同理不含
# `不要/想要/大家/我们/你们` 这类口语词（真码是口语短语时会误杀）。
CN_SENTENCE_RE = re.compile(
    r"(进行|需要|可以|应该|建议|如果|因为|所以|但是|而且|已经|正在|即将|将会)"
)

ASCII_STOP = {"GLOBAL", "CN", "UTC", "HTTP", "HTTPS", "GAMETIPSSTUDIO"}

# 行首是这些引号 ⇒ 多半是版本名/作品名
QUOTE_HEADS = "「『《【“\""

# 兑换码区锚点：「兑换」出现的行
ANCHOR_RE = re.compile(r"兑换")
# 候选行距离最近锚点行的最大距离（超出则认为不在兑换码区）
ANCHOR_WINDOW = 15

TITLE_VERSION_RE = re.compile(r"\d+\.\d+|版本|周年|月之")

# ── 有效期识别 ──────────────────────────────────────────────────────────────
# 实测语料（2026-09-15）里玩家/攻略作者的实际写法：
#   · 原神7.1前瞻兑换码有效期至2026年9月15日12：00      （全角冒号！）
#   · 2026/08/30 23:59:59前有效
#   · (兑换码将于2026年8月15日23:59:59后失效)
#   · 4-兑换码将于2026年10月31日 00:00 失效，记得尽快兑换哦~
# ⚠️ 对抗性审查：**时间部分从这里剥掉**，改由 `_parse_hms()` 统一解析 ——
# 原来这里只认 `HH:MM`，于是 `9月21日12时00分`、`9月21号12:00`、`明天中午` 全部退化成 23:59，
# **偏晚 12~24 小时**（用户以为还有时间、其实早废了，是危险方向）。
DATE_TIME_RE = re.compile(
    r"(?:(20\d{2})\s*[-/年]\s*)?(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?"
)
# ⚠️ 实测补充了「之前 / 以前」：真机高频写法是「请在9月21日23：59**之前**兑换好兑换码」，
# 两字之差 —— `DATE_TIME_RE` 明明能匹配，却因整行**不含任何有效期词**而整帖丢弃
# （真实语料 2 条帖、共 3 个真码因此提不到有效期）。
EXPIRY_WORDS = ("有效期", "失效", "过期", "截止", "有效", "之前", "以前")
# ⚠️ 这些词出现在**日期锚点之前**时，说明那个日期不是「码」的有效期（如"邮件有效期为30天"、
# "活动时间：…"）。**判据必须是"位于日期之前"** —— 实测发现：原来是**行内任意位置**
# 判定，于是「兑换码有效期至9月21日23:59，请尽快获取**奖励**~」句尾的"奖励"把**整行真有效期**
# 干掉了（真实语料 15 条有效期声明里 4 条、**27%** 被这一个词吃掉）。
EXPIRY_EXCLUDE = ("邮件", "奖励", "领取", "活动时间", "维护", "更新")
# 行内**任意位置**出现这些词 ⇒ 这行是**提问**而不是有效期声明：
# 「今晚的前瞻兑换码的有效期是多久啊」会被误提成"今晚 23:59"。
EXPIRY_EXCLUDE_ANY = ("多久", "什么时候")
EXPIRED_MARK_RE = re.compile(r"[（(]\s*已过期\s*[)）]|已失效|已经过期")

# 相对日期词 → 相对**发帖时间**的天数偏移（2026-09-20 新增，与 extract_expiry 的缺陷②配套）。
# 语料依据：真机上高频写法是「截止时间明天晚上23:59」「今晚23:59过期」「有效期至次日」，
# 而 `DATE_TIME_RE` 只吃绝对日期（`9月21日` / `2026年9月21日`）⇒ 这类帖的有效期**完全提不到**。
REL_DAY_OFFSET = {
    "今天": 0, "今日": 0, "今晚": 0, "本日": 0, "当日": 0,
    "明天": 1, "明日": 1, "明晚": 1, "次日": 1,
    "后天": 2, "后日": 2,
    "大后天": 3,
}
REL_DAY_RE = re.compile("|".join(sorted(REL_DAY_OFFSET, key=len, reverse=True)))
# 时间写法（**绝对/相对路径共用**；`REL_TIME_*` 是历史名，保留给老脚本，语义见 `_parse_hms`）。
# 后来补全：原来只认 `HH:MM` 与 `N点`，导致 `12时00分` / `12点30` / `中午` /
# `凌晨` 全部退化 ⇒ **偏晚 12~24 小时**。
REL_TIME_HM_RE = TIME_HMS_RE = re.compile(r"(\d{1,2})\s*[:：]\s*(\d{2})(?:\s*[:：]\s*(\d{2}))?")
REL_TIME_POINT_RE = TIME_POINT_RE = re.compile(r"(\d{1,2})\s*[点时](?:\s*(\d{1,2})\s*分?)?")
NIGHT_RE = re.compile(r"晚上|晚|夜间|夜里")
NOON_RE = re.compile(r"中午|正午")
MIDNIGHT_RE = re.compile(r"凌晨|半夜")
# 「这行里到底有没有写出一个具体时刻」—— 相对词边界判据用
TIME_ANY_RE = re.compile(r"\d{1,2}\s*[:：]\s*\d|\d{1,2}\s*[点时]|中午|正午|凌晨|半夜")


def _parse_hms(line: str, start: int = 0, *, default_ss: int = 59) -> tuple[int, int, int] | None:
    """解析一行里（`start` 之后）的「几点几分几秒」；**没写时间返回 None**（由调用方取缺省）。

    `start` 用于跳过前面的无关时间：「今日12:00开服，兑换码有效期至**次日23:59**」要看的是
    相对词（次日）**之后**的那个时间，从行首找会拿到"12:00"。
    `default_ss` 是"只写到分"时的缺省秒：**有效期取 59**（当天结束，偏晚但同一天内），
    **前瞻开播取 0**（19:30 就是 19:30:00，不是 19:30:59）。

    支持的写法（后来补全；原来只认 `HH:MM`/`N点` ⇒ 其余全部退化成 23:59 = **偏晚**）：
      · `23:59` / `23:59:30`（写全秒就保留，顺带解决绝对/相对两条路径的秒数不一致）
      · `12点` / `12时` / `12时30分` / `12点30`
      · `中午` → 12:00 · `凌晨` → 00:00
      · 「晚上12点」= 当天结束 ⇒ **23:59:59**（写成 12:00 会早报 12 小时）
    """
    tail = line[start:]
    m = TIME_HMS_RE.search(tail)
    if m:
        h, mi = int(m.group(1)), int(m.group(2))
        ss = int(m.group(3)) if m.group(3) is not None else default_ss
        if 0 <= h <= 23 and 0 <= mi <= 59 and 0 <= ss <= 59:
            return h, mi, ss
    m = TIME_POINT_RE.search(tail)
    if m:
        h = int(m.group(1))
        mi = int(m.group(2)) if m.group(2) is not None else 0
        if NIGHT_RE.search(tail) and h <= 12:
            return 23, 59, default_ss
        if 0 <= h <= 23 and 0 <= mi <= 59:
            return h, mi, default_ss
    if NOON_RE.search(tail):
        return 12, 0, default_ss
    if MIDNIGHT_RE.search(tail):
        return 0, 0, default_ss
    return None


def extract_expiry(
    text: str,
    *,
    subject: str = "",
    now: datetime | None = None,
    posted_at: datetime | None = None,
) -> datetime | None:
    """从**标题 + 正文**里提取**兑换码有效期**。

    提不到就返回 None —— **不做"直播后 N 天"这类规律猜测**（实测各游戏/各帖写法都不一样，
    猜错比不显示更糟）；调用方在拿不到时可以只提示"通常 1~2 天内失效"。

    ⚠️ 修两个**实测缺陷**（星铁 4.6 前瞻当晚发现，用例：「如果明确知道
    9.21 23:59 过期的话 为啥不写上呢」）：

    ① **标题没被看** —— 真机上「星穹铁道4.6前瞻直播兑换码 **截止时间明天晚上23:59**」
       的有效期**只写在标题里**，而旧实现只扫正文 ⇒ 星铁 3 个 4.6 码的 `expire_at` 全是 None
       （对照：`extract_live_start` 一直支持 `subject=`，这里当年漏了）。

    ② **相对日期不认** —— `DATE_TIME_RE` 只吃绝对日期，而玩家最高频的写法是
       「明天晚上23:59」「今晚23:59」「次日」⇒ 即使把标题并进来也提不到。

    相对日期的基准取 **`posted_at`（帖子发布时间）**，不是抓取时间：帖子里说的"明天"是
    **发帖时**的明天；拿抓取时间去算，抓到一条三天前的帖就会算错（宁可保守，不能报错的时间）。
    """
    blob = "\n".join(x for x in (subject or "", text or "") if x)
    if not blob.strip():
        return None
    # tzinfo 规范化：接口原来没约定，传 UTC 时刻会让 `.date()` 偏一天。
    if now is not None and now.tzinfo is None:
        now = now.replace(tzinfo=TZ)
    if posted_at is not None and posted_at.tzinfo is None:
        posted_at = posted_at.replace(tzinfo=TZ)
    moment = now or datetime.now(TZ)
    base = posted_at or moment  # 相对日期的解释基准

    # 行收集：
    #   · 长段落（"整段塞进一行"的常见排版）按标点切成子句 —— 原来 >120 字符**整行跳过** ⇒ 漏提；
    #   · 额外把**标题行 + 正文首行**拼成一行 —— 「…（有效期见下）」+ 下一行「明天23:59」这种
    #     跨行写法，逐行判定永远组合不起来。
    lines: list[str] = []
    for raw in blob.splitlines():
        s = raw.strip()
        if not s:
            continue
        if len(s) <= 200:
            lines.append(s)
        else:
            lines.extend(c.strip() for c in re.split(r"[。！？；;!?]", s) if c.strip())
    _head = [x.strip() for x in blob.splitlines() if x.strip()][:2]
    if len(_head) == 2:
        lines.append(" ".join(_head))

    best: datetime | None = None
    for line in lines:
        if not line:
            continue
        if not any(w in line for w in EXPIRY_WORDS):
            continue
        if any(w in line for w in EXPIRY_EXCLUDE_ANY):
            continue

        # 定位「日期锚点」：排除词只有出现在它**之前**，才算是在限定这个日期。
        m = DATE_TIME_RE.search(line)
        rel = None if m else REL_DAY_RE.search(line)
        if m is None and rel is None:
            continue
        anchor = m.start() if m else rel.start()
        if any(w in line[:anchor] for w in EXPIRY_EXCLUDE):
            continue

        dt: datetime | None = None
        if m:
            # ⚠️ 一行里可能有**多个**日期：区间写法（「有效期：9月1日~9月21日23:59」）必须取
            # **结束日** —— 取第一个会让真码当场被判"早已过期"、从查询里消失。
            # ⇒ 从后往前试，取**最后一个能解析成功**的。
            for _m in reversed(list(DATE_TIME_RE.finditer(line))):
                y, mo, d = _m.group(1), _m.group(2), _m.group(3)
                base_year = int(y) if y else moment.year
                # 时间统一由 `_parse_hms` 解析（`12时00分`/`中午` 等写法、
                # 以及绝对与相对两条路径的秒数口径都在这里对齐）；没写时间缺省当天 23:59:59。
                h, mi, ss = _parse_hms(line, _m.end()) or (23, 59, 59)
                try:
                    cand = datetime(base_year, int(mo), int(d), h, mi, ss, tzinfo=TZ)
                except ValueError:
                    continue
                # 没写年份时（真实帖最常见的写法，如「有效期8月30日23：59：59」），
                # 若按今年算已经过去太久，就试明年
                if not y and cand < moment - timedelta(days=30):
                    try:
                        cand = cand.replace(year=base_year + 1)
                    except ValueError:  # 2/29
                        continue
                dt = cand
                break

        if dt is None and rel is not None:  # 退回相对日期（缺陷②）
            # 同绝对日期：一行可能有**多个**相对词（「今日12:00开服，兑换码有效期至次日23:59」），
            # 要取**最后一个**（更靠近"有效期"的语义）+ 它**之后**的那个时间；
            # 取第一个会拿到无关的开服时间、把真码拉早一天。
            _days = list(REL_DAY_RE.finditer(line))
            if _days:
                rel = _days[-1]
            # ⭐ **词边界**：相对词必须确实在"说日期" ——
            #   ① 它**之前 ≤4 字符**内有有效期词（「有效期**至次日**」「**截止**时间明天」），或
            #   ② 它**之后 ≤6 字符**内有具体时刻（「**今晚**23:59」）。
            # 否则「**明日**方舟联动…」「《**明天**》」「**后天**上线」都会凭空造出一个有效期。
            _pre = line[max(0, rel.start() - 4):rel.start()]
            _post = line[rel.end():rel.end() + 6]
            if not (any(w in _pre for w in EXPIRY_WORDS) or TIME_ANY_RE.search(_post)):
                continue
            h, mi, ss = _parse_hms(line, rel.end()) or (23, 59, 59)
            day = (base + timedelta(days=REL_DAY_OFFSET[rel.group(0)])).date()
            try:
                dt = datetime(day.year, day.month, day.day, h, mi, ss, tzinfo=TZ)
            except ValueError:
                dt = None

        if dt is None:
            continue
        # ⭐ 有效期不可能**早于发帖时间**（帖是"发码"的）。这一道防线一次挡掉三类真实误提：
        #   · **区间**写法取到开始日：「有效期：9月1日~9月21日23:59」→ 9-01
        #     ⇒ 真码当场被判"已过期"、`render_codes` 直接输出「当前没有未过期的兑换码」；
        #   · 行内先出现的无关时间：「今日12:00开服，兑换码有效期至次日23:59」；
        #   · 上一期的失效声明：「上一期兑换码已于9月19日失效」。
        # 宽容 1 小时：发帖时间口径可能略有偏差，且跨零点发帖很常见。
        if posted_at is not None and dt < posted_at - timedelta(hours=1):
            continue
        # 合理性窗口：太离谱的日期当误提丢掉
        if dt < moment - timedelta(days=30) or dt > moment + timedelta(days=90):
            continue
        if best is None or dt < best:  # 取最早的一个（宁可提示人早换）
            best = dt
    return best


def has_expired_marker(text: str) -> bool:
    """正文/标题里明写「（已过期）」这类标记。"""
    return bool(text and EXPIRED_MARK_RE.search(text))


def expired_sentinel(now: datetime | None = None) -> datetime:
    """给「明写（已过期）」但没写具体时间的帖一个**过去**的时间戳。

    这样它既能在查询里被标成「已过期」，又会被推送侧的"过期不推"过滤掉。
    """
    return (now or datetime.now(TZ)) - timedelta(days=1)


# ── 前瞻直播时间识别 ────────────────────────────────────────────────────────
LIVE_WORDS = ("前瞻", "直播", "特别节目", "开播", "节目")
LIVE_TITLE_RE = re.compile(r"前瞻.*(特别节目|直播)|前瞻特别节目|前瞻直播")


def extract_live_start(
    text: str, *, subject: str = "", now: datetime | None = None
) -> datetime | None:
    """从官方帖正文/标题里提取「前瞻直播开播时间」。

    实测官方预告帖会写「9月20日 19:30 前瞻特别节目」「今晚 19:30 直播」这类。
    ⚠️ 只认**明确写了日期时间**的；提不到就返回 None（调用方只报「有前瞻预告」）。
    """
    moment = now or datetime.now(TZ)
    candidates: list[datetime] = []
    for raw in (subject or "").split("\n") + (text or "").split("\n"):
        line = raw.strip()
        if not line or len(line) > 120:
            continue
        if not any(w in line for w in LIVE_WORDS):
            continue
        m = DATE_TIME_RE.search(line)
        if not m:
            continue
        y, mo, d = m.group(1), m.group(2), m.group(3)
        base_year = int(y) if y else moment.year
        # ⚠️ 时间改由 `_parse_hms` 解析（`DATE_TIME_RE` 已剥掉时间部分，见其注释）；
        # 前瞻开播的缺省仍是 **19:30:00**（米哈游前瞻的惯例时间），且"只写到分"时秒取 0。
        h, mi, ss = _parse_hms(line, m.end(), default_ss=0) or (19, 30, 0)
        try:
            dt = datetime(base_year, int(mo), int(d), h, mi, ss, tzinfo=TZ)
        except ValueError:
            continue
        if not y and dt < moment - timedelta(days=3):
            try:
                dt = dt.replace(year=base_year + 1)
            except ValueError:
                continue
        # 合理窗口：过去 3 天内 ~ 未来 90 天内
        if dt < moment - timedelta(days=3) or dt > moment + timedelta(days=90):
            continue
        candidates.append(dt)
    return min(candidates) if candidates else None


def is_live_title(subject: str) -> bool:
    """标题是不是「前瞻特别节目」这类 —— 用来判断官方有没有发预告。"""
    return bool(subject and LIVE_TITLE_RE.search(subject))


LIVE_ROOM_RE = re.compile(r"live\.bilibili\.com/(?:blanc/)?(\d{3,12})")


def extract_live_room(text: str) -> str:
    """从官方帖正文里提 B站直播间 room_id（原神的预告帖会挂 `live.bilibili.com/<id>`）。"""
    m = LIVE_ROOM_RE.search(text or "")
    return m.group(1) if m else ""


def render_live_notice(
    items: list[dict[str, Any]],
    *,
    now: datetime | None = None,
    rooms: dict[str, str] | None = None,
) -> str:
    """渲染「前瞻直播提醒」推送（**每个前瞻事件只发一条**）。

    items: `[{"game": "genshin", "title": "原神7.1前瞻", "start": datetime|None, "room": "21987615"}]`
    rooms: 各游戏的固定直播间兜底表 `{"genshin": "21987615", ...}`
    """
    moment = now or datetime.now(TZ)
    lines = ["🎬 前瞻直播提醒"]
    for it in items:
        name = GAMES[it["game"]]["name"]
        title = it.get("title") or f"{name} 前瞻特别节目"
        start = it.get("start")
        if start is None:
            when = "官方已发预告（具体时间没写）"
        else:
            secs = (start - moment).total_seconds()
            if secs <= 0:
                when = "**正在直播中**"
            elif secs < 3600:
                when = f"**{int(secs // 60)} 分钟后开播**（{start:%H:%M}）"
            else:
                when = f"{start:%m-%d %H:%M} 开播"
        room = str(it.get("room") or (rooms or {}).get(it["game"], "") or "")
        tail = f"\n　　B站直播间：https://live.bilibili.com/{room}" if room else ""
        lines.append(f"【{name}】{title} —— {when}{tail}")
    lines.append("")
    lines.append("直播中/直播后会放出兑换码，插件会第一时间抓取")
    return "\n".join(lines)


@dataclass
class Candidate:
    """一个候选码。"""

    code: str
    kind: str  # "cn" | "ascii"
    region: str  # "cn" | "global" | "unknown"
    line: str = ""


@dataclass
class CodeHit:
    code: str
    game: str
    uids: list[str] = field(default_factory=list)  # 不同作者 uid（官方源记为 "official:xxx"）
    titles: list[str] = field(default_factory=list)
    posts: list[str] = field(default_factory=list)
    tier: str = "C"  # A ≥2 位作者或官方源 | B 单作者但标题带版本号且帖内多候选 | C 丢弃
    official: bool = False  # 来自官方活动接口（如米游社 miyolive）
    expire_at: datetime | None = None  # 正文里明写的有效期（提不到就是 None）
    # ⭐ `expire_at` 是「**估算**」的还是「帖子里**明确写的**」。
    #   · `True` = 估算（官方 miyolive 的有效期恒为「**直播结束 + 3 天**」的保守估计，
    #     见 `main.py: miyolive_codes_by_act`）；
    #   · `False` = 帖子里明确写的时间；
    #   · `None` = 未知（老账本里没存这个字段）。
    # 冲突时 **明确写的优先于估算的**（「有效期的精确值仍然『明确写的 > 估算的』」）。
    # 🩸 实例：原神 7.1 的码，官方估算 = 09-15 21:16:10、社区明写 = 09-15 12:00 ——
    # 用估算的话，12:00~21:16 这 **9 小时**里查询会说"还能换"，其实早废了。
    expire_estimated: bool | None = None
    # 这条是**从本地 `seen` 记录还原的**（不是本轮实时抓到的）⇒ 渲染时标「历史记录」。
    # 用途：短时效窗口的数据源（异环 TapTap 列表页只覆盖 4~5 小时）一翻页就抓不到，
    # 查询得靠本地记录兜底。
    recorded: bool = False
    # 这条记录**最后一次见到**的时间（人读格式，如 `2026-09-16 20:19:51`）。
    # ⚠️ 老记录里没有 `expire_at` ⇒ 只能标"有效期未知"，那时**记录时间就是用户判断
    # 新鲜度的唯一依据**（前瞻码通常 1~2 天失效）。
    recorded_at: str = ""

    @property
    def authors(self) -> int:
        return len({u for u in self.uids if u and not str(u).startswith("official:")})

    @property
    def confidence(self) -> int:
        """兼容旧接口：等同作者数。"""
        return self.authors


def _clean_line(raw: str) -> str:
    return raw.strip().strip("「」『』【】[]（）()《》<>\"'`*#-—·、。,.：:;；!！?？｜|").strip()


def _is_cn_token(line: str) -> bool:
    return (
        bool(CN_TOKEN_RE.match(line))
        and line not in CN_STOP
        and not CN_STOP_RE.match(line)
        and not CN_SENTENCE_RE.search(line)  # 说明句不是码（2026-09-21 修误报）
    )


def _is_ascii_token(line: str) -> bool:
    if not ASCII_TOKEN_RE.match(line):
        return False
    if VERSION_LIKE_RE.match(line):
        return False
    if not any(ch.isalpha() for ch in line):  # 纯数字不是兑换码
        return False
    return line.upper() not in ASCII_NOISE_UPPER


ASCII_NOISE_UPPER = {w.upper() for w in ASCII_STOP}


def extract_candidates(
    text: str,
    *,
    subject: str = "",
    exclude: Iterable[str] = (),
    min_cn_group: int = 2,
    include_global: bool = False,
    anchor_window: int = ANCHOR_WINDOW,
) -> list[Candidate]:
    """从帖子正文提取**国服侧**候选码。

    Args:
        text: 帖子正文纯文本（delta_to_text 的结果）
        subject: 帖子标题（用于「无锚点时靠标题含『兑换码』退化」判定 + 标题复读过滤）
        exclude: 需要屏蔽的字符串（帖子 id 等）
        min_cn_group: 中文候选必须「连续成组」的最少行数（挡「有效期至明日」这类孤例说明文字）
        include_global: True 时连国际服候选一起返回（调试用）
        anchor_window: 候选行距锚点行的最大距离

    Returns:
        保序去重后的 Candidate 列表（默认只含国服侧）
    """
    if not text:
        return []
    clean = URL_RE.sub(" ", text)
    for e in exclude:
        if e:
            clean = clean.replace(str(e), " ")

    raw_lines = clean.splitlines()
    # 预处理：剥表情、全角空格、去掉纯占位行
    lines: list[str] = []
    for raw in raw_lines:
        s = EMOJI_RE.sub("", raw).replace("\u3000", " ").strip()
        if s and PLACEHOLDER_RE.sub("", s).strip():
            lines.append(s)
        else:
            lines.append("")

    # 锚点：含「兑换」的行；一个都没有时，只有标题含「兑换码」才退化处理（否则整帖跳过）
    anchors = [i for i, s in enumerate(lines) if ANCHOR_RE.search(s)]
    if not anchors and "兑换码" not in (subject or ""):
        return []
    if not anchors:
        anchors = [0]  # 退化：把正文开头当码区

    def near_anchor(idx: int) -> bool:
        return any(abs(idx - a) <= anchor_window for a in anchors)

    found: list[tuple[int, Candidate]] = []
    cn_run: list[int] = []
    cn_runs: list[list[int]] = []
    # 区域初值：**标题点名国际服的整帖按国际服处理**（实测发现：正文里可能只有
    # 「国际服兑换码如下」这类无冒号写法，甚至一个标签都没有）
    region = "unknown"
    subj = subject or ""
    if re.search(r"国际服|海外服|国际版|Global", subj, re.I):
        region = "global"
    elif re.search(r"国服|国内服|国区|简中服", subj):
        region = "cn"
    # 中文候选要求离锚点更近（帖末的「感谢观看」「祝大家游戏愉快」成组会被当码）
    cn_window = max(1, int(anchor_window * 0.4))

    def flush_cn_run() -> None:
        if cn_run:
            cn_runs.append(list(cn_run))
            cn_run.clear()

    def consider(idx: int, raw_line: str, line: str) -> None:
        """判定一行里的候选（区域已知）。"""
        nonlocal cn_run
        is_cn = _is_cn_token(line)
        is_ascii = _is_ascii_token(line)
        if is_cn:
            cn_run.append(idx)
        elif line:
            flush_cn_run()
        limit = cn_window if is_cn else anchor_window
        if not any(abs(idx - a) <= limit for a in anchors):
            return
        if raw_line[:1] in QUOTE_HEADS:  # 版本名/作品名
            return
        if is_cn:
            if line in CN_STOP or CN_STOP_RE.match(line):
                return
            if "兑换码" in line and len(line) > 10:  # 说明句
                return
            found.append((idx, Candidate(code=line, kind="cn", region=region, line=line)))
        if is_ascii:
            found.append((idx, Candidate(code=line, kind="ascii", region=region, line=line)))

    for idx, raw in enumerate(lines):
        if not raw:
            flush_cn_run()
            continue
        line = _clean_line(raw)
        if not line:
            flush_cn_run()
            continue

        # 1) 区域标签行（优先判定；冒号后的 rest 也参与候选判定）
        m = CN_TAG_RE.match(line)
        if m:
            region = "cn"
            flush_cn_run()
            rest = _clean_line(m.group(2) or "")
            if rest:
                consider(idx, raw, rest)
            continue
        if CN_BARE_RE.match(line):
            region = "cn"
            flush_cn_run()
            continue
        m = GL_TAG_RE.match(line)
        if m:
            region = "global"
            flush_cn_run()
            rest = _clean_line(m.group(2) or "")
            if rest:
                consider(idx, raw, rest)
            continue
        if GL_BARE_RE.match(line):
            region = "global"
            flush_cn_run()
            continue

        consider(idx, raw, line)

    flush_cn_run()

    # 中文候选必须「连续成组」：孤零零一个中文词组多半是说明文字或角色名
    if min_cn_group > 1:
        keep: set[int] = set()
        for run in cn_runs:
            if len(run) >= min_cn_group:
                keep.update(run)
        found = [(i, c) for i, c in found if c.kind != "cn" or i in keep]

    # 国际服码对国服玩家无效
    if not include_global:
        found = [(i, c) for i, c in found if c.region != "global"]

    seen: set[str] = set()
    out: list[Candidate] = []
    for _idx, cand in found:
        key = cand.code.lower()
        if key in seen:
            continue
        seen.add(key)
        out.append(cand)
    return out


def _prefer_expiry(
    new_dt: datetime | None,
    new_est: bool | None,
    old_dt: datetime | None,
    old_est: bool | None,
) -> bool:
    """新的有效期是否该**取代**旧的。

    规则（「有效期的精确值仍然『明确写的 > 估算的』」）：
      1. 旧的为空 → 接受新的；
      2. **明确写的（False）优先于估算的（True）** —— 估算再早也不该顶掉精确值，
         因为官方估算（直播结束 + 3 天）可能**比真实失效时间晚**，那段时间里会误报"还能换"；
      3. **同一可信度之间取更早的**（宁可提示人早换）；`None`（未知）与估算同级。
    """
    if new_dt is None:
        return False
    if old_dt is None:
        return True
    # 可信度排序：**明确写的(False) < 未知(None) < 估算(True)**（数值越小越可信）。
    # ⚠️ 对抗性审查：原来把 `None`(未知) 与 `True`(估算) 当**同级**，而社区帖的 records
    # **默认不带 `expire_estimated` 键** ⇒ `rec.get(...)` 恒为 `None` ⇒ 规则退化成老的"取更早"，
    # 「明确 > 估算」成了**死代码**（官方估算照样顶掉社区明写）。三档分开才成立。
    _RANK = {False: 0, None: 1, True: 2}
    new_rank = _RANK.get(new_est, 1)
    old_rank = _RANK.get(old_est, 1)
    if new_rank != old_rank:
        return new_rank < old_rank
    return new_dt < old_dt


def collect_codes(records: list[dict[str, Any]], game: str) -> list[CodeHit]:
    """把多帖的候选聚合成 CodeHit，并按**不同作者数**分层。

    Args:
        records: `[{"post_id", "title", "uid", "candidates": [Candidate, ...]}, ...]`
        game: game key

    Returns:
        按 tier(A→B→C)、作者数、码名排序的 CodeHit 列表
    """
    table: dict[str, CodeHit] = {}
    meta: dict[str, dict[str, Any]] = {}
    for rec in records:
        title = str(rec.get("title") or "")
        uid = str(rec.get("uid") or "")
        pid = str(rec.get("post_id") or "")
        cands = rec.get("candidates") or []
        n_cands = len(cands)  # 该帖的候选总数（B 级判据之一）
        body = str(rec.get("text") or "")
        for cand in cands:
            # 标题复读 —— ⚠️ 只有当这个词**只出现在标题、正文里根本没有**时才丢
            # （有人把真码写进标题，一律丢会导致漏报；没给正文时保守不丢）
            if (
                title
                and body
                and cand.code
                and cand.code.replace(" ", "") in title.replace(" ", "")
                and cand.code not in body
            ):
                continue
            key = cand.code.lower()
            hit = table.get(key)
            if hit is None:
                hit = CodeHit(code=cand.code, game=game)
                table[key] = hit
                meta[key] = {"title_has_version": False, "max_cands_per_post": 0}
            if uid and uid not in hit.uids:
                hit.uids.append(uid)
                if str(uid).startswith("official:"):
                    hit.official = True
            if pid and pid not in hit.posts:
                hit.posts.append(pid)
            if title and title not in hit.titles:
                hit.titles.append(title)
            if title and TITLE_VERSION_RE.search(title):
                meta[key]["title_has_version"] = True
            if n_cands > meta[key]["max_cands_per_post"]:
                meta[key]["max_cands_per_post"] = n_cands
            # 有效期聚合（2026-09-20 加"明确 > 估算"）：
            # 「帖子里明确写的」优先于「官方保守估算的」；同级取**更早**的（宁可提示人早换）。
            exp = rec.get("expire_at")
            if isinstance(exp, datetime) and _prefer_expiry(
                exp, rec.get("expire_estimated"), hit.expire_at, hit.expire_estimated
            ):
                hit.expire_at = exp
                hit.expire_estimated = rec.get("expire_estimated")

    for key, hit in table.items():
        if hit.official or hit.authors >= 2:
            hit.tier = "A"  # 官方活动源，或 ≥2 位不同作者（实测唯一零误报档）
        elif meta[key]["title_has_version"] and meta[key]["max_cands_per_post"] >= 2:
            hit.tier = "B"  # 单作者，但标题带版本号且该帖有多个候选
        else:
            hit.tier = "C"

    tier_rank = {"A": 0, "B": 1, "C": 2}
    return sorted(table.values(), key=lambda h: (tier_rank.get(h.tier, 9), -h.authors, h.code))


# ── 状态与「新码」判定 ──────────────────────────────────────────────────────
def now_str() -> str:
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def code_fingerprint(code: str) -> str:
    """码的短指纹：清理明细后仍能识别「这个码推过」，防止旧码重现被当成新码。"""
    return hashlib.sha1((code or "").strip().lower().encode("utf-8")).hexdigest()[:12]


def _parse_ts(value: Any) -> datetime | None:
    try:
        return datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S").replace(tzinfo=TZ)
    except (ValueError, TypeError):
        return None


GLOBAL_BUCKET = "_global"


def diff_new_codes(
    seen: dict[str, dict[str, Any]],
    hits: list[CodeHit],
    *,
    tombstones: dict[str, list[str]] | None = None,
) -> list[CodeHit]:
    """返回 never-seen 的码（即本轮「新码」）。不修改任何入参。

    ⚠️ 先查 `_global` 桶：同一篇「三游汇总帖」会让同一个码在多个游戏下都被收集到，
    只按游戏桶判重会**把同一个码推两次**、且其中一次挂着错误的游戏名。
    """
    out = []
    global_bucket = seen.get(GLOBAL_BUCKET) or {}
    for h in hits:
        key = h.code.lower()
        if key in global_bucket or key in seen.get(h.game, {}):
            continue
        if tombstones and code_fingerprint(h.code) in (tombstones.get(h.game) or []):
            continue
        out.append(h)
    return out


_TIER_RANK = {"A": 0, "B": 1, "C": 2}

# 单条账本明细里 `uids` / `posts` 的**上限**：不设限时这两列表随轮数线性膨胀
# （实测 40 轮 uids=41/posts=41、单条 946B；外推 200 轮 4188B）。20 条足够支撑"≥2 位作者"判据与溯源。
SEEN_LIST_LIMIT = 20


def mark_seen(seen: dict[str, dict[str, Any]], hits: list[CodeHit], *, create: bool = True) -> None:
    """把码记入 seen（原地修改）。同时写游戏桶与 `_global` 桶（跨游戏防重复推）。

    `create=False` = **只刷新已存在的条目、绝不新增**，用于「每轮顺手把存量账本的元数据
    更新到最新」（2026-09-20 新增）：

    · 为什么需要它：`check_once` 平时**只对"新码"调用本函数**，于是**存量账本永远不刷新** ——
      星铁 4.6 那 3 个码是旧逻辑记的（`expire_at` 为空），修好「有效期写在标题里也能提到」
      之后，查询里依旧显示「有效期未知」⇒ **修了等于没修**。查询路径 `_query_hits` 又是只读的。
    · 为什么必须 `create=False`：C 级码若被提前写进账本，将来它样本凑齐升到 A 级时会被
      `diff_new_codes` 当成"已见过"而**永久漏推**。
    """
    for h in hits:
        key = h.code.lower()
        for bucket_name in (h.game, GLOBAL_BUCKET):
            # `create=False` 且桶不存在时**别建空桶** —— "只刷新"的路径不该留下任何新痕迹
            # （`prune_seen` 虽然会把空桶 pop 掉，但 `retention_days<=0` 时它会残留）。
            bucket = seen.get(bucket_name)
            if bucket is None:
                if not create:
                    continue
                bucket = seen[bucket_name] = {}
            entry = bucket.get(key)
            if not isinstance(entry, dict):
                # ⚠️ 实测发现的**致命健壮性缺陷**：`create=False` 让"每轮都会触碰
                # 账本里的**每一个**条目"（原来只处理 new_hits、碰不到存量）。于是账本里**一条坏数据**
                # 就足以让 `check_once` **每轮抛异常**、后面的游戏整轮跳过 ⇒ **全插件永久停摆**：
                #   · `entry` 是 str/list ⇒ `entry["last_seen"] = ...` → `TypeError: 'str' object does not support item assignment`
                #   · `entry["uids"] is None` ⇒ `u not in None` → `TypeError: argument of type 'NoneType' is not iterable`
                # 处理：**键不存在（None）** 时按 `create` 语义决定是否新建；
                #       **键存在但值坏了** 时视为需要**重建**（键本来就在，重建不算"新增"，
                #       而且这是唯一的自愈机会）。
                if entry is None and not create:
                    continue
                new_entry = {
                    "code": h.code,
                    "game": h.game,
                    # ⚠️ 统一大写落账：`h.tier` 若是小写（如 "a"），渲染层会认不出来、
                    # 显示成一个孤零零的 `·`。
                    "tier": str(h.tier).upper(),
                    "first_seen": now_str(),
                    "last_seen": now_str(),
                    # ⚠️ 新建时也要**截断到上限**：只在"更新分支"裁剪的话，
                    # 首轮就把 50 个 uids 写进账本，上限形同虚设。
                    "uids": list(h.uids)[-SEEN_LIST_LIMIT:],
                    "posts": list(h.posts)[-SEEN_LIST_LIMIT:],
                    # ⚠️ 必须存有效期：查询兜底要把「已记录的码」还原成 CodeHit 并**判过期**，
                    # 没有这个字段就只能标"有效期未知"（2026-09-16 补）
                    "expire_at": h.expire_at.isoformat() if h.expire_at else "",
                }
                # `expire_estimated`：True=官方估算 / False=帖子里明写 / 字段缺失=未知（老账本）。
                # ⚠️ **只在已知时才写这个键** —— 缺失要能被区分成"未知"，否则老账本里的
                # 官方估算值会被当成"明确写的"，社区的真实失效时间就再也覆盖不进去了。
                if h.expire_estimated is not None:
                    new_entry["expire_estimated"] = bool(h.expire_estimated)
                bucket[key] = new_entry
            else:
                entry["last_seen"] = now_str()
                # ⚠️ tier **只升不降**（2026-09-20）：账本记的是"这个码**历史上**达到过的最可信档"。
                # 档位是**证据积累**的产物（作者数会随采集窗口波动，不代表可信度真的下降），
                # 而且降档会让同一枚码在查询里**来回跳**。
                # 🩸 **注释更正**（实测）：原注释写"降到 C 会让码在查询里消失"
                # —— **不成立**：`show_tier_c` 默认 True，`main.py` 还显式传 True，C 级照样展示。
                # 真正受影响的是**推送**（`push_tiers` 默认只含 A/B）。
                if _TIER_RANK.get(str(h.tier).upper(), 9) < _TIER_RANK.get(
                    str(entry.get("tier") or "C").upper(), 9
                ):
                    entry["tier"] = str(h.tier).upper()
                if h.expire_at is not None:
                    prev = _parse_iso(entry.get("expire_at"))
                    # 「明确写的 > 估算的」，同级取更早（原来没有就补上）
                    if _prefer_expiry(
                        h.expire_at, h.expire_estimated, prev, entry.get("expire_estimated")
                    ):
                        _same_value = prev is not None and prev == h.expire_at
                        entry["expire_at"] = h.expire_at.isoformat()
                        if h.expire_estimated is not None:
                            entry["expire_estimated"] = bool(h.expire_estimated)
                        elif not _same_value:
                            # 值换了、而新值的来源未知 ⇒ 标记也归"未知"
                            entry.pop("expire_estimated", None)
                        # **值没变时保留旧标记** —— 原来无条件 pop，会把已知的"估算"
                        # 降级成"未知"（信息丢失）。
                # ⚠️ `uids` / `posts` 可能是 `None` 或非 list（被外部改坏）—— 必须先纠正再 `in`，
                # 否则 `u not in None` 会抛 `TypeError: argument of type 'NoneType' is not iterable`
                # 并把整个轮询带崩。
                _uids = entry.get("uids")
                if not isinstance(_uids, list):
                    _uids = entry["uids"] = []
                for u in h.uids:
                    if u not in _uids:
                        _uids.append(u)
                # **设上限**，否则单条明细会线性膨胀（实测 40 轮 uids=41/posts=41、946B；
                # 外推 200 轮 4188B）。保留最近的若干条即可（`authors` 只需要知道"≥2 位"）。
                if len(_uids) > SEEN_LIST_LIMIT:
                    del _uids[: len(_uids) - SEEN_LIST_LIMIT]
                _posts = entry.get("posts")
                if not isinstance(_posts, list):
                    _posts = entry["posts"] = []
                for p in h.posts:
                    if p not in _posts:
                        _posts.append(p)
                if len(_posts) > SEEN_LIST_LIMIT:
                    del _posts[: len(_posts) - SEEN_LIST_LIMIT]


def _parse_iso(value: Any) -> datetime | None:
    """解析 `mark_seen` 存下的 `expire_at`（ISO 字符串）；解析不了就返回 None。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=TZ)
    return dt


def hits_from_seen(seen: dict[str, dict[str, Any]], game: str) -> list[CodeHit]:
    """把 `seen` 里**已记录过**的码还原成 `CodeHit`（供查询兜底）。

    动机：查询走的是"此刻实时抓取"，对**短时效窗口**的数据源
    （异环的 TapTap 列表页每页只覆盖 4~5 小时）一翻页就什么都抓不到 ⇒
    出现「插件明明记着 5 个码、`/兑换码 异环` 却说没有」的怪象。
    ⇒ 一句话：**「我知道什么」和「我此刻能看到什么」是两件事**。

    还原出来的条目 `recorded=True`，渲染时会标「历史记录」。
    """
    out: dict[str, CodeHit] = {}
    for bucket_name in (game, GLOBAL_BUCKET):
        for key, entry in (seen.get(bucket_name) or {}).items():
            if not isinstance(entry, dict):
                continue
            if str(entry.get("game") or "") != game:
                continue  # `_global` 桶里混着别的游戏的码，别串号
            lk = str(key).lower()
            if lk in out:
                continue
            uids = [str(u) for u in (entry.get("uids") or [])]
            out[lk] = CodeHit(
                code=str(entry.get("code") or key),
                game=game,
                uids=uids,
                titles=[],
                posts=[str(p) for p in (entry.get("posts") or [])],
                tier=str(entry.get("tier") or "C"),
                official=any(u.startswith("official:") for u in uids),
                expire_at=_parse_iso(entry.get("expire_at")),
                # 缺键 ⇒ None（未知）：老账本里的值是官方估算，不能冒充"明确写的"
                expire_estimated=entry.get("expire_estimated"),
                recorded=True,
                recorded_at=str(entry.get("last_seen") or entry.get("first_seen") or ""),
            )
    return list(out.values())


def merge_query_hits(live: list[CodeHit], recorded: list[CodeHit]) -> list[CodeHit]:
    """把「已记录的码」合并进实时抓取结果：**实时优先、按码去重、过期的淘汰**。

    ⚠️ 需求：「**记得过期这些过期的码**」⇒ 从本地记录还原的码一旦
    **明确已过期**就不再补进来。实时抓到的那份不受影响（渲染层仍会把已过期的沉到末尾
    单独成组「仅供核对」—— 那是网站的现况、有核对价值；而本地旧账没必要占版面）。
    """
    now = datetime.now(TZ)
    out = list(live)
    have = {h.code.lower() for h in live}
    for h in recorded:
        lk = h.code.lower()
        if lk in have:
            continue
        if h.expire_at is not None and h.expire_at <= now:
            continue  # 已过期 ⇒ 淘汰
        have.add(lk)
        out.append(h)
    return out


def prune_seen(
    seen: dict[str, dict[str, Any]],
    *,
    retention_days: int = 180,
    tombstones: dict[str, list[str]] | None = None,
    max_tombstones: int = 2000,
    now: datetime | None = None,
) -> list[str]:
    """清理「最后出现时间」超过 retention_days 的码明细（原地修改 seen）。

    被清掉的码会把**指纹**写进 tombstones，这样它日后重现也不会被误报成新码。
    ⚠️ 时间戳解析失败的条目不清理（宁可留着，也不要误清）；retention_days<=0 表示不清理。
    """
    if retention_days <= 0:
        return []
    moment = now or datetime.now(TZ)
    cutoff = moment - timedelta(days=retention_days)
    removed: list[str] = []
    for game, bucket in list(seen.items()):
        if not isinstance(bucket, dict):
            continue
        for key, entry in list(bucket.items()):
            if not isinstance(entry, dict):
                continue
            # ⚠️ 回收基准**不能**用每轮都会被刷新的 `last_seen`：`create=False` 的存量刷新
            # 会让"偶尔还被提到"的老码永远留在账本里 ⇒ `retention_days` 形同虚设、`state.json` 只增不减
            # （实测 5000 条 38.5ms/轮、20000 条 4MB/134ms）。
            # 改用「**首次见到**」与「**有效期结束**」里**较晚**的那个：
            #   · 活跃码（有效期还在未来）不会被误清；
            #   · 老码按期回收，并写指纹进 `tombstones`（下面已处理）防它重现时被误报成新码。
            first = _parse_ts(entry.get("first_seen"))
            exp = _parse_iso(entry.get("expire_at"))
            ref = first
            if exp is not None and (ref is None or exp > ref):
                ref = exp
            if ref is None or ref >= cutoff:
                continue
            code = entry.get("code") or key
            removed.append(code)
            bucket.pop(key, None)
            if tombstones is not None:
                fp = code_fingerprint(code)
                lst = tombstones.setdefault(game, [])
                if fp not in lst:
                    lst.append(fp)
                    if len(lst) > max_tombstones:
                        del lst[: len(lst) - max_tombstones]
        if not bucket:
            seen.pop(game, None)
    return removed


def seen_count(seen: dict[str, dict[str, Any]]) -> int:
    """已记账的码总数（不含跨游戏去重用的 `_global` 桶，避免重复计数）。"""
    return sum(
        len(v) for k, v in seen.items() if k != GLOBAL_BUCKET and isinstance(v, dict)
    )


# ── 渲染 ────────────────────────────────────────────────────────────────────
DISCLAIMER = "⚠️ 国服码，请尽快在游戏内兑换（前瞻码通常 1~2 天内失效）"
TIER_MARK = {"A": "✔", "B": "✔", "C": "?"}


def _src_label(hit: CodeHit) -> str:
    """来源标签：「官方活动源」/「N 位作者」；从本地记录还原的额外标「历史记录（何时记）」."""
    base = "官方活动源" if hit.official else f"{hit.authors} 位作者"
    if not hit.recorded:
        return base
    when = (hit.recorded_at or "")[5:16]  # `2026-09-16 20:19` → `09-16 20:19`
    return f"历史记录 · {base}" + (f"｜{when} 记录" if when else "")


def expiry_suffix(hit: CodeHit, now: datetime | None = None) -> str:
    """把有效期渲染成人话（还剩多久），提不到有效期就返回空串。

    ⚠️ 例外：**从本地记录还原的码**提不到有效期时必须显式说「有效期未知」——
    否则用户会照默认文案（"有效期通常 1~2 天"）误判，那比不显示更糟。
    """
    if not hit.expire_at:
        return "｜⚠️ 有效期未知" if hit.recorded else ""
    moment = now or datetime.now(TZ)
    left = (hit.expire_at - moment).total_seconds()
    if left <= 0:
        return "｜⚠️ 已过期"
    hours = left / 3600
    if hours < 1:
        return f"｜{max(1, int(left // 60))} 分钟后过期"
    if hours < 48:
        return f"｜{int(hours)} 小时后过期"
    return f"｜{hit.expire_at:%m-%d %H:%M} 过期"


def render_codes(
    game: str,
    hits: list[CodeHit],
    *,
    header: str,
    show_tier_c: bool = True,
    show_expired: bool = False,
) -> str:
    """渲染查询结果。

    `show_tier_c` 默认 True —— 人主动问的时候要给全信息（直播刚结束只有
    一位作者发帖时，C 档真码不该被藏起来），只是标注「仅单一来源，自行核对」。

    `show_expired` 默认 **False**（需求「**已经明确过期的话是不是能不展示了**」）：
    明确已过期的码连「另有 N 个已过期」一起**不列** —— 复制了也没用，只占版面。
    想核对旧码就把配置 `show_expired_codes` 打开。
    （沿革：最前面照列 ⇒ 降级到末尾单独成组 ⇒ 现在默认不显示。）
    """
    name = GAMES[game]["name"]
    shown = [h for h in hits if show_tier_c or h.tier in ("A", "B")]
    if not shown:
        return f"【{name}】{header}\n（没有查到有效的兑换码）"
    moment = datetime.now(TZ)
    dead = [h for h in shown if h.expire_at is not None and h.expire_at <= moment]
    alive = [h for h in shown if h not in dead]
    lines = [f"【{name}】{header}"]
    if alive:
        for h in alive:
            lines.append(
                f"{TIER_MARK.get(h.tier, '·')} {h.code}（{_src_label(h)}）{expiry_suffix(h)}"
            )
    else:
        lines.append("（当前没有未过期的兑换码）")
    lines.append("✔ = 官方源或 ≥2 位作者交叉验证")
    # ⚠️ 需求：「**已经明确过期的话是不是能不展示了**」
    # ⇒ 默认**连「另有 N 个已过期」都不列**（列出来只占版面、复制了也没用）；
    # 想核对旧码时把配置 `show_expired_codes` 打开。
    if dead and show_expired:
        lines.append(f"（另有 {len(dead)} 个已过期，仅供核对）")
        for h in dead:
            lines.append(f"· {h.code}（{_src_label(h)}）{expiry_suffix(h)}")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def render_codes_messages(
    game: str, hits: list[CodeHit], *, header: str, show_tier_c: bool = True,
    show_expired: bool = False, now: datetime | None = None,
) -> list[str]:
    """把**查询结果**也拆成多条消息（供合并转发卡片用）。

    需求：「我想的是 第一条消息：【原神】当前兑换码（…汇总…）
    第二条 往冥府的安魂歌 第三条 （官方活动源）｜⚠️ 已过期…… 然后合并聊天记录转发
    **这样我方便复制**」。

    ⇒ 拆分规则（和 `render_push_messages` 同思路，但查询要带汇总信息）：
      · 第 1 条：`【游戏】当前兑换码` + 汇总（可用几个 / 没有未过期的）+ 图例 + 过期计数
      · 之后**每个码独占一条**（只有码本身 —— 长按复制最干净，不会把来源时效一起复制走）
      · 紧跟一条写它的来源与时效：`（官方活动源）｜⚠️ 已过期`
      · 最后一条：免责说明
    """
    name = GAMES[game]["name"]
    shown = [h for h in hits if show_tier_c or h.tier in ("A", "B")]
    if not shown:
        return [f"【{name}】{header}\n（没有查到有效的兑换码）", DISCLAIMER]

    moment = now or datetime.now(TZ)
    dead = [h for h in shown if h.expire_at is not None and h.expire_at <= moment]
    alive = [h for h in shown if h not in dead]

    head = [f"【{name}】{header}"]
    if alive:
        head.append(f"可用 {len(alive)} 个 —— **长按下面每一条即可单独复制**")
    else:
        head.append("（当前没有未过期的兑换码）")
    head.append("✔ = 官方源或 ≥2 位作者交叉验证")
    # 同 `render_codes`：默认**不展示明确已过期**的码
    if dead and show_expired:
        head.append(f"（另有 {len(dead)} 个已过期，仅供核对）")
    msgs = ["\n".join(head)]

    for h in alive + (dead if show_expired else []):
        msgs.append(h.code)  # ← 只放码本身，长按复制不会带别的东西
        msgs.append(f"（{_src_label(h)}）{expiry_suffix(h, now=moment)}".rstrip())
    msgs.append(DISCLAIMER)
    return msgs


def render_push_messages(hits: list[CodeHit], *, now: datetime | None = None) -> list[str]:
    """把一次推送拆成**多条消息**（用于合并转发卡片）。

    设计目标：「**方便直接复制兑换码到游戏里粘贴**」：如果所有码挤在一条消息里，
    长按复制会把整段都复制走。所以这里拆成 ——

        · 每条游戏一行标题（带版本号与有效期）
        · **每个兑换码单独一条消息**
        · 最后一条写来源

    Args:
        hits: 本轮要推的新码
        now: 计算剩余时间用（测试可注入）

    Returns:
        消息文本列表（保序）
    """
    moment = now or datetime.now(TZ)
    by_game: dict[str, list[CodeHit]] = {}
    for h in hits:
        by_game.setdefault(h.game, []).append(h)

    msgs: list[str] = []
    for game, group in by_game.items():
        version = ""
        for h in group:
            m = re.search(r"(\d+\.\d+)", " ".join(h.titles))
            if m:
                version = m.group(1)
                break
        expires = [h.expire_at for h in group if h.expire_at]
        if expires:
            left = min(expires) - moment
            secs = left.total_seconds()
            if secs <= 0:
                exp_txt = "⚠️ 已过期"
            elif secs < 3600:
                exp_txt = f"有效期剩 {max(1, int(secs // 60))} 分钟"
            elif secs < 48 * 3600:
                exp_txt = f"有效期剩 {int(secs // 3600)} 小时"
            else:
                exp_txt = f"有效期至 {min(expires):%m-%d %H:%M}"
        else:
            exp_txt = "有效期通常 1~2 天"
        label = f"{GAMES[game]['name']} {version} 前瞻兑换码（{exp_txt}）".replace("  ", " ").strip()
        msgs.append(label)
        for h in group:
            msgs.append(h.code)

    authors = max((h.authors for h in hits), default=0)
    if any(h.official for h in hits):
        src = "官方活动源"
    elif authors >= 2:
        src = f"{authors} 位作者交叉验证"
    else:
        src = "单一来源"
    # 来源站按**本轮涉及的数据源**动态生成（异环 → TapTap，米哈游系 → 米游社）；
    # 多游戏混推时并列，顺序按首次出现，稳定可预期。
    sites: list[str] = []
    for h in hits:
        site = SITE_NAME.get(str((GAMES.get(h.game) or {}).get("source") or ""), "")
        if site and site not in sites:
            sites.append(site)
    site_txt = " + ".join(sites) if sites else "社区"
    msgs.append(f"来源：{src} · {site_txt}　（每条消息都可长按单独复制）")
    return msgs


def render_new_codes_message(new_hits: list[CodeHit]) -> str:
    """把「新码」渲染成一条推送消息。"""
    by_game: dict[str, list[CodeHit]] = {}
    for h in new_hits:
        by_game.setdefault(h.game, []).append(h)
    lines = ["🎮 发现新的前瞻兑换码！"]
    for game, hits in by_game.items():
        lines.append("")
        lines.append(f"【{GAMES[game]['name']}】")
        for h in hits:
            src = "官方活动源" if h.official else f"{h.authors} 位作者确认"
            lines.append(f"• {h.code}（{src}）{expiry_suffix(h)}")
    lines.append("")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def truncate(text: str, limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[: limit - 1] + "…"
