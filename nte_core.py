"""异环（Neverness to Everness / NTE）专用纯逻辑核心。

为什么单独一个文件：**异环不是米哈游的游戏**（Hotta Studio / 完美世界），官方社区大本营是
**TapTap（app id 714119）**，数据形态和米游社完全不同：

  · 米游社：JSON 接口（`searchPosts`），正文是 Quill delta
  · 异环：**TapTap 帖子是 SSR 的 HTML**，正文在
    `<div class="tap-rich-content__row ..." data-rich-content-index="N">…</div>` 里
    （2026-09-16 实测：**纯 HTTP GET 就能拿到全文**，不需要浏览器、不需要 JS 渲染，
     开头的 `<meta name="description">` 摘要会被截断，别拿它当正文）

本模块只做两件**不依赖网络**的事：
  1. 把 TapTap 帖子 HTML 变成结构化话题（标题 / 正文 / 发帖时间）；
  2. 从正文里提**国服**兑换码 —— 规则复用 `hoyo_core.extract_candidates`
     （锚点窗 / 停用词 / 成组），外层只加异环独有的判据：**国际服码前缀 `NTE…` 排除**。

零 AstrBot 依赖，可离线自测（见 `selftest_nte.py`）。
"""

from __future__ import annotations

import html as html_mod
import re
import sys
import os
from datetime import datetime, timedelta, timezone
from typing import Any

_PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
if _PLUGIN_DIR not in sys.path:
    sys.path.insert(0, _PLUGIN_DIR)

import hoyo_core as core  # noqa: E402

TZ = timezone(timedelta(hours=8))

# ── 帖子 HTML 解析 ──────────────────────────────────────────────────────────
OG_TITLE_RE = re.compile(r'<meta[^>]+property="og:title"[^>]+content="([^"]*)"')
POST_TIME_RE = re.compile(r'<meta[^>]+property="bytedance:lrDate_time"[^>]+content="([^"]*)"')
# 正文段落：SSR 渲染出来的每个段落一个 div（实测每个段落都有 data-rich-content-index）
ROW_RE = re.compile(r'data-rich-content-index="\d+"[^>]*>(.*?)</div>', re.S)
# 兜底：整页去标签（TapTap 改结构时还能抢救出正文）
SCRIPT_STYLE_RE = re.compile(r"(?is)<(script|style)[^>]*>.*?</\1>")
# 🩸 块级标签必须换成**换行**再 strip，否则相邻文本会**粘连**：
# 实测 TapTap 摘要里三个码是 `<br>`/`<div>` 分隔的，直接去标签会得到
# `THEWHOOTSWITCHHOUSEYHDOUYIN0924`（33 个字符的"一个大 token"）⇒ 提码全丢。
BLOCK_TAG_RE = re.compile(r"(?i)</?(?:div|p|br|li|tr|td|h[1-6]|section|article)\b[^>]*/?>")
TAG_RE = re.compile(r"<[^>]+>")
MOMENT_URL_RE = re.compile(r"taptap\.cn/moment/(\d+)")

# 国际服码：异环国际服码一律 `NTE…`（如 NTEFREE）；国服是 `YH…` 或纯字母词（如 FOGDENGAME）
INTL_PREFIX = "NTE"

# 作者链接：`/user/785543430`（页面顶部第一个就是楼主）—— 交叉验证要靠 uid 去重
AUTHOR_RE = re.compile(r'href="/user/(\d+)"')

# 兑换码候选：前后不能紧挨字母数字，避免把长串切一半
ASCII_CODE_RE = re.compile(r"(?<![A-Za-z0-9])([A-Z][A-Z0-9]{5,15})(?![A-Za-z0-9])")
# 大写英文字面量黑名单（**整体相等**才算，所以 FOGDENGAME / SUMMERTIME 不会因为含 GAME/TIME 被误杀）
CODE_STOP = {
    "TAPTAP", "GAME", "GAMES", "APP", "APK", "PC", "IOS", "ANDROID", "BILIBILI",
    "CODE", "CODES", "CODE1", "TIPS", "GUIDE", "NEWS", "SSR", "SR", "UR", "RMB",
    "VIP", "QQ", "URL", "HTTP", "HTTPS", "PLAYER", "LEVEL", "BUG", "PVP", "PVE",
}
# 锚点行（含「兑换」）上下各看几行
# ⚠️ 别调太小：TapTap 摘要里三个码常被 `<br>` 拆成三行，而锚点行只有第一行
# （`#异环 #异环兑换码`），窗口 2 会让最后一个码落在窗外（实测漏掉 YHDOUYIN0924）。
ANCHOR_WINDOW = 5


def strip_tags(fragment: str) -> str:
    """去标签 + 反转义 HTML 实体，并把**块级标签换成换行**（防相邻文本粘连）。

    ⚠️ 不要退回"直接删掉所有标签"：三个兑换码是 `<br>` 分隔的时候，直接删会连成
    `THEWHOOTSWITCHHOUSEYHDOUYIN0924` 一个长串，提码器一个都认不出来（实测踩到）。
    """
    s = SCRIPT_STYLE_RE.sub("", fragment or "")
    s = BLOCK_TAG_RE.sub("\n", s)
    s = TAG_RE.sub("", s)
    s = html_mod.unescape(s)
    s = re.sub(r"[ \t\u00a0]+", " ", s)
    s = re.sub(r"\n[ \t]*", "\n", s)
    return re.sub(r"\n{2,}", "\n", s).strip()


def parse_taptap_post(html: str, *, url: str = "") -> dict[str, Any] | None:
    """把 TapTap 帖子 HTML 解析成 `{post_id, url, title, text, created_at}`。

    `created_at` 是 **epoch 秒（float）**，与米游社 `searchPosts` 返回的字段口径一致，
    这样上层 `_merge_posts()` / 时间窗过滤可以直接复用。

    解析不出来（比如拿到的是登录页/风控页）返回 None，由调用方决定告警还是跳过。
    """
    if not html or len(html) < 200:
        return None

    m_title = OG_TITLE_RE.search(html)
    title = html_mod.unescape(m_title.group(1)) if m_title else ""
    # og:title 尾巴上带着「 - 异环综合讨论 - TapTap 异环论坛」，去掉
    title = re.sub(r"\s*-\s*(TapTap|异环).*$", "", title).strip()

    created_at: float | None = None
    m_time = POST_TIME_RE.search(html)
    if m_time:
        try:  # 形如 2026-08-09T13:00:52+00:00
            created_at = datetime.fromisoformat(m_time.group(1)).timestamp()
        except ValueError:
            created_at = None

    rows = [strip_tags(m.group(1)) for m in ROW_RE.finditer(html)]
    text = "\n".join(r for r in rows if r)
    if not text:
        # 结构变了就退回「整页去标签」：会混进导航等噪音，但提码靠锚点窗还能压住
        text = strip_tags(html)
    if not text and not title:
        return None

    post_id = ""
    m_id = MOMENT_URL_RE.search(url or "") or MOMENT_URL_RE.search(html)
    if m_id:
        post_id = m_id.group(1)

    author_uid = ""
    m_author = AUTHOR_RE.search(html)
    if m_author:
        author_uid = m_author.group(1)

    return {
        "post_id": post_id or (url or "").rstrip("/").rsplit("/", 1)[-1],
        "url": url,
        "title": title,
        "text": text,
        "created_at": created_at,
        "author_uid": author_uid,
    }


# ── 提码 ────────────────────────────────────────────────────────────────────
def is_intl_code(code: str) -> bool:
    """国际服码（`NTE…`）—— 对国服玩家无效，必须滤掉。"""
    return (code or "").upper().startswith(INTL_PREFIX)


def extract_nte_codes(text: str, *, subject: str = "") -> list[str]:
    """从异环帖子正文里提**国服**兑换码（保序去重）。

    ⚠️ **不能直接复用米游社的 `extract_candidates`**：那套是**按行**取 token 的
    （一行 = 一个候选），而异环的码常写成一行多个：

        三个兑换码：FOGDENGAME / EYEOFDELUSION / SUMMERTIME，游戏内…粘贴提交

    整行既不等于任何单个 token，米游社引擎会直接丢掉；反过来 `min_cn_group=1` 时它还会把
    「记得尽快兑换」这种中文短句当成中文码。所以这里自己实现，只认 ASCII（异环没有中文码）：

      · 锚点行 = 含「兑换」的行，看它上下 `ANCHOR_WINDOW` 行；
      · 候选 = 边界完整的大写字母数字串（6~16 字符），黑名单整体相等才算命中；
      · 国际服码（`NTE…`）剔除。
    """
    if not text:
        return []
    lines = text.splitlines()
    anchors = [i for i, line in enumerate(lines) if "兑换" in line]
    if not anchors:
        return []
    out: list[str] = []
    for i in anchors:
        lo = max(0, i - ANCHOR_WINDOW)
        hi = min(len(lines), i + ANCHOR_WINDOW + 1)
        for line in lines[lo:hi]:
            for m in ASCII_CODE_RE.finditer(line):
                code = m.group(1)
                if code in CODE_STOP or is_intl_code(code):
                    continue
                if code not in out:
                    out.append(code)
    return out


def extract_expiry(
    text: str,
    *,
    subject: str = "",
    now: datetime | None = None,
    posted_at: datetime | None = None,
) -> datetime | None:
    """有效期直接用米游社那套（`8月10日23:59失效` 这种写法它认得）。

    ⚠️ 2026-09-20 起**必须把标题一起传进来**：真机上有效期常写在标题里
    （「…直播兑换码 **截止时间明天晚上23:59**」），只看正文会漏 —— 米游社那条链路
    就是踩了这个（星铁 4.6 三个码的 `expire_at` 全是 None）。
    `posted_at` 是相对日期（明天/今晚）的解释基准，TapTap 帖自带 `created_at`。
    """
    return core.extract_expiry(text, subject=subject, now=now, posted_at=posted_at)


def build_records(posts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """把 `parse_taptap_post()` 的结果转成 `core.collect_codes()` 吃的话题记录。

    这样异环和米哈游**共用同一套聚合/分层/有效期逻辑**：
      · A 档 = ≥2 位不同作者交叉验证（uid 去重后仍 ≥2）；
      · B 档 = 单作者但标题带版本号且帖内多候选；
      · 标题复读过滤、有效期取最早 —— 全都白拿，不用重写。
    """
    out: list[dict[str, Any]] = []
    for p in posts or []:
        text = str(p.get("text") or "")
        if not text:
            continue
        title = str(p.get("title") or "")
        codes = extract_nte_codes(text, subject=title)
        if not codes:
            continue
        out.append({
            "post_id": str(p.get("post_id") or ""),
            "title": title,
            "uid": str(p.get("author_uid") or ""),
            # 已经排除了国际服码，所以 region 固定 "cn"
            "candidates": [core.Candidate(code, "ascii", "cn") for code in codes],
            "text": text,
            # 有效期：**标题 + 正文**一起找，相对日期以**发帖时间**为基准（2026-09-20 修）
            "expire_at": extract_expiry(
                text,
                subject=title,
                posted_at=(
                    datetime.fromtimestamp(p["created_at"], core.TZ)
                    if p.get("created_at")
                    else None
                ),
            ),
        })
    return out


# ── 列表页解析（发现层）─────────────────────────────────────────────────────
# ⚠️ 上一轮误判「列表页不是 SSR」：`__NUXT__` 变量里确实没有数据，但帖子是**以普通 HTML
# 元素 SSR 在页面正文里**的。2026-09-16 复核确认（`probe/raw/nte-topic-714119.html`）。
CARD_MARK_RE = re.compile(r'<div class="[^"]*moment-list-item')
CARD_LINK_RE = re.compile(r'href="/moment/(\d+)')
# ⚠️ `itemprop="name"` 在**作者名**上也有 ⇒ 锚点必须用这个 class，否则作者名会被当成标题
CARD_TITLE_RE = re.compile(
    r'class="moment-article__summary--title[^"]*"[^>]*>(.*?)</span>', re.S)
# ⚠️ 摘要 div 的真实 class 是 `moment-article__summary moment-article__summary--wrapper gray-06`
# （**不精确等于** `moment-article__summary`），而且内容是**嵌套 div** ⇒ 不能用
# `class="moment-article__summary"` + `(.*?)</div>` 这种写法（实测摘要全解析成空字符串）。
# 正解：用 `itemprop="text"` 定位这个 div，再用括号配对取出整个 div。
CARD_SUMMARY_OPEN_RE = re.compile(r'<div[^>]*itemprop="text"[^>]*>')
DIV_OPEN_RE = re.compile(r"<div\b", re.I)
DIV_CLOSE_RE = re.compile(r"</div>", re.I)


def slice_div(html: str, start: int) -> str:
    """`html[start:]` 必须从某个 `<div` 起 —— 返回该 div 的**完整内部 HTML**（支持嵌套）。"""
    depth = 0
    i = start
    while True:
        m_open = DIV_OPEN_RE.search(html, i)
        m_close = DIV_CLOSE_RE.search(html, i)
        if not m_close:
            return html[start:]
        if m_open and m_open.start() < m_close.start():
            depth += 1
            i = m_open.end()
        else:
            depth -= 1
            i = m_close.end()
            if depth == 0:
                return html[start:i - len("</div>")]

# 相对时间是文本，**title 属性才是绝对时间**（"2026/09/16 01:49:27"）
CARD_TIME_RE = re.compile(r'class="tap-time[^"]*"[^>]*title="([^"]+)"')
CARD_AUTHOR_RE = re.compile(r'href="/user/(\d+)"')

# 列表页里值得进详情页的关键词（避免把「闲聊帖」也拉详情，省请求）
INTERESTING_WORDS = ("兑换码", "兑换口令", "口令码", "礼包码", "cdk", "前瞻", "偷跑", "福利码")


def parse_taptap_list(html: str) -> list[dict[str, Any]]:
    """解析 TapTap 话题列表页（SSR）→ 帖子卡片列表。

    返回每条：`{post_id, url, title, summary, author_uid, created_at}`，
    `created_at` 是 epoch 秒（与米游社口径一致），按页面顺序（= 发布时间倒序）。
    """
    if not html:
        return []
    marks = [m.start() for m in CARD_MARK_RE.finditer(html)]
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for i, start in enumerate(marks):
        end = marks[i + 1] if i + 1 < len(marks) else len(html)
        chunk = html[start:end]
        m_link = CARD_LINK_RE.search(chunk)
        if not m_link:
            continue
        pid = m_link.group(1)
        if pid in seen:  # 同一张卡片可能被多个 class 匹配到
            continue
        seen.add(pid)

        m_title = CARD_TITLE_RE.search(chunk)
        m_summary = CARD_SUMMARY_OPEN_RE.search(chunk)
        m_time = CARD_TIME_RE.search(chunk)
        m_author = CARD_AUTHOR_RE.search(chunk)

        created_at: float | None = None
        if m_time:
            try:
                created_at = datetime.strptime(
                    m_time.group(1).strip(), "%Y/%m/%d %H:%M:%S"
                ).replace(tzinfo=TZ).timestamp()
            except ValueError:
                created_at = None

        out.append({
            "post_id": pid,
            "url": f"https://www.taptap.cn/moment/{pid}",
            "title": strip_tags(m_title.group(1)) if m_title else "",
            "summary": strip_tags(slice_div(chunk, m_summary.start())) if m_summary else "",
            "author_uid": m_author.group(1) if m_author else "",
            "created_at": created_at,
        })
    return out


def is_interesting(post: dict[str, Any]) -> bool:
    """这张卡片值不值得进详情页（标题/摘要里出现兑换码相关词）。"""
    blob = f"{post.get('title') or ''} {post.get('summary') or ''}".lower()
    return any(w in blob for w in INTERESTING_WORDS)
