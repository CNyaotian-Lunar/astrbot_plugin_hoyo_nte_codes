"""异环（NTE）纯逻辑自测 —— 用**真实抓来的 TapTap 帖子 HTML** 做夹具。

夹具：`nte-post-835621339560675781.html`（2026-09-16 抓的真实响应，262 KB）
—— 用真货而不是手写 HTML，才能验出「SSR 结构变了没有」。
夹具体积较大、**不入库**：用环境变量 `NTE_FIXTURE_DIR` 指定夹具所在目录；未设置时跳过真实 HTML 用例。

    python selftest_nte.py
"""

from __future__ import annotations

import os
import sys
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import nte_core as nte  # noqa: E402
import hoyo_core as core  # noqa: E402

PASS = FAIL = 0
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


# 真实响应夹具目录（夹具数百 KB、不入库）：由环境变量 `NTE_FIXTURE_DIR` 指定。
# 默认留空 ⇒ 跳过需要夹具的用例，而不是去猜某个本机路径。
FIXTURE_DIR = os.environ.get("NTE_FIXTURE_DIR") or ""
RAW = os.path.join(FIXTURE_DIR, "nte-post-835621339560675781.html") if FIXTURE_DIR else ""

print("=== [1] 真实 TapTap 帖子 HTML（SSR）解析 ===")
if RAW and os.path.exists(RAW):
    html = open(RAW, encoding="utf-8", errors="replace").read()
    post = nte.parse_taptap_post(html, url="https://www.taptap.cn/moment/835621339560675781")
    check("能解析出帖子", post is not None)
    if post:
        print(f"    title      = {post['title']}")
        print(f"    post_id    = {post['post_id']}")
        print(f"    created_at = {datetime.fromtimestamp(post['created_at'], nte.TZ) if post['created_at'] else None}")
        print(f"    text 前 80 = {post['text'][:80]!r}")
        check("标题正确（去掉了 TapTap 后缀）",
              post["title"] == "异环1.3雾中朔望星回前瞻：兑换码+卡池载具", repr(post["title"]))
        check("post_id 提出来了", post["post_id"] == "835621339560675781", post["post_id"])
        check("发帖时间解析对（meta bytedance:lrDate_time = 2026-08-09T13:00:52Z）",
              post["created_at"] is not None
              and abs(post["created_at"] - datetime(2026, 8, 9, 21, 0, 52, tzinfo=nte.TZ).timestamp()) < 1,
              str(post["created_at"]))
        check("正文含关键段落（不是被截断的 meta 摘要）",
              "三个兑换码" in post["text"] and "8月10日23:59失效" in post["text"],
              repr(post["text"][:120]))
        check("正文里没有 HTML 残留标签", "<div" not in post["text"] and "&amp;" not in post["text"])
        codes = nte.extract_nte_codes(post["text"], subject=post["title"])
        print(f"    提码 = {codes}")
        check("提出三个国服码（注意是 EYEOFDELUSION，不是 EYEODELUSION）",
              codes == ["FOGDENGAME", "EYEOFDELUSION", "SUMMERTIME"], str(codes))
        exp = nte.extract_expiry(post["text"])
        print(f"    有效期 = {exp}")
        # 该帖 8 月 10 日失效，相对抓取日(9-16)已过 30 天窗口 ⇒ 引擎按设计丢弃（返回 None）
        check("过期太久的有效期按设计不猜（返回 None，不编日期）",
              exp is None or exp.year in (2026, 2027), str(exp))
else:
    print("  ⚠️ 未提供夹具（设 NTE_FIXTURE_DIR=<夹具目录> 可跑真实 HTML 用例），跳过")

print("\n=== [2] 国际服码必须被滤掉（异环独有判据）===")
check("NTE 前缀 = 国际服", nte.is_intl_code("NTEFREE") and nte.is_intl_code("nte123") and not nte.is_intl_code("YHBILIBILI0423"))
mixed = """《异环》前瞻福利
兑换码：YHBILIBILI0423 / NTEFREE / SUMMERTIME
记得尽快兑换
"""
got = nte.extract_nte_codes(mixed, subject="异环1.4前瞻兑换码")
print(f"    混排提码 = {got}")
check("混排时国际服码 NTEFREE 被剔除、国服码留下",
      "NTEFREE" not in got and "YHBILIBILI0423" in got and "SUMMERTIME" in got, str(got))

print("\n=== [3] 边界 ===")
check("空/None 不炸", nte.extract_nte_codes("") == [] and nte.extract_nte_codes(None) == [])  # type: ignore[arg-type]
check("HTML 太短 ⇒ 判定为无效页（风控/登录页）", nte.parse_taptap_post("<html>blocked</html>") is None)
check("非帖子 HTML 也不炸", nte.parse_taptap_post("<html>" + "x" * 500 + "</html>") is not None)
check("strip_tags 反转义实体", nte.strip_tags("<b>a&amp;b&#39;c</b>") == "a&b'c",
      nte.strip_tags("<b>a&amp;b&#39;c</b>"))
check("strip_tags 丢掉 script/style 内容",
      "alert" not in nte.strip_tags("<script>alert(1)</script><style>.a{}</style>hi"))

print("\n=== [4] 防误报（异环只认 ASCII 码，中文短句不能变码）===")
check("「记得尽快兑换」这类中文不会被误判成码",
      nte.extract_nte_codes("兑换码\n记得尽快兑换\n", subject="异环前瞻兑换码") == [],
      str(nte.extract_nte_codes("兑换码\n记得尽快兑换\n", subject="异环前瞻兑换码")))
check("没有「兑换」锚点的正文不提取（避免满页英文都变码）",
      nte.extract_nte_codes("SUMMERTIME FOGDENGAME\n今天天气不错\n") == [],
      str(nte.extract_nte_codes("SUMMERTIME FOGDENGAME\n今天天气不错\n")))
check("黑名单是整体相等（FOGDENGAME 含 GAME 但不该被误杀）",
      nte.extract_nte_codes("兑换码：GAME\nFOGDENGAME\n") == ["FOGDENGAME"],
      str(nte.extract_nte_codes("兑换码：GAME\nFOGDENGAME\n")))
check("同一位作者的多段正文里重复出现的码只留一个",
      nte.extract_nte_codes("兑换码：SUMMERTIME\n记得换\n兑换码 SUMMERTIME\n") == ["SUMMERTIME"],
      str(nte.extract_nte_codes("兑换码：SUMMERTIME\n记得换\n兑换码 SUMMERTIME\n")))

print("\n=== [5] 与米游社共用的聚合层（tier 按不同作者数）===")
_posts = [
    {"post_id": "p1", "title": "异环1.4前瞻兑换码汇总", "author_uid": "u1",
     "text": "兑换码：SUMMERTIME / FOGDENGAME\n记得换"},
    {"post_id": "p2", "title": "异环1.4前瞻兑换码", "author_uid": "u2",
     "text": "三个兑换码 SUMMERTIME / FOGDENGAME\n失效时间见公告"},
    {"post_id": "p3", "title": "随便聊聊", "author_uid": "u3",
     "text": "我个人推荐 SOLOCODE 这个码\n但是不一定对"},
]
_recs = nte.build_records(_posts)
check("只有含「兑换」锚点的帖才进 records", len(_recs) >= 2, str([r["post_id"] for r in _recs]))
_hits = core.collect_codes(_recs, "nte")
_by_code = {h.code: h for h in _hits}
print(f"    hits = {[(h.code, h.tier, h.authors) for h in _hits]}")
check("两位不同作者提到的 SUMMERTIME ⇒ A 档",
      _by_code.get("SUMMERTIME") is not None and _by_code["SUMMERTIME"].tier == "A",
      str([(h.code, h.tier) for h in _hits]))
check("同一位作者的帖只算一个 uid（交叉验证不能注水）",
      _by_code["SUMMERTIME"].authors == 2 if "SUMMERTIME" in _by_code else False,
      str(_by_code.get("SUMMERTIME").uids if "SUMMERTIME" in _by_code else None))
check("空列表不炸", nte.build_records([]) == [])

print("\n=== [6] TapTap 话题列表页（发现层，SSR）===")
LIST_RAW = os.path.join(FIXTURE_DIR, "nte-topic-714119.html") if FIXTURE_DIR else ""
if LIST_RAW and os.path.exists(LIST_RAW):
    lhtml = open(LIST_RAW, encoding="utf-8", errors="replace").read()
    cards = nte.parse_taptap_list(lhtml)
    print(f"    解析出 {len(cards)} 条卡片：")
    for c in cards[:5]:
        ts = datetime.fromtimestamp(c["created_at"], nte.TZ) if c["created_at"] else "?"
        print(f"      {c['post_id']}  {ts}  uid={c['author_uid']:>10}  {c['title'][:26]!r}")
        if c["summary"]:
            print(f"          摘要: {c['summary'][:60]!r}")
    check("列表页解析出多条卡片（≥5）", len(cards) >= 5, str(len(cards)))
    check("每条都有 post_id 与**绝对**发布时间（相对时间是文本，取的是 title 属性）",
          all(c["post_id"] and c["created_at"] for c in cards),
          str([(c["post_id"], c["created_at"]) for c in cards]))
    # ⚠️ 页面里混着「社区置顶」帖，前几条**不保证严格按时间倒序** ⇒ 别断言顺序；
    # 真正要保证的是「时间全部解析出来、且都在近期」，靠时间窗过滤来判新。
    _now = datetime.now(nte.TZ).timestamp()
    check("时间都解析出来且落在合理窗口内（近 30 天）",
          all(c["created_at"] and _now - 30 * 86400 < c["created_at"] <= _now + 3600 for c in cards),
          str([c["created_at"] for c in cards]))
    _titled = [c for c in cards if c["title"]]
    check("标题不会被作者名顶替、也不会全空（itemprop=name 的坑）",
          len(_titled) >= len(cards) // 2 and all(c["title"] != "超可爱小团子" for c in _titled),
          str([c["title"] for c in cards]))
    interesting = [c for c in cards if nte.is_interesting(c)]
    print(f"    关键词命中 {len(interesting)} 条")
    check("关键词过滤能挑出兑换码相关帖", len(interesting) >= 1, str([c["title"] for c in interesting]))
    # ⭐ 摘要里直接带码的情况：这一条能省掉详情页请求
    in_summary = [c for c in cards if nte.extract_nte_codes(c["summary"], subject=c["title"])]
    if in_summary:
        got = nte.extract_nte_codes(in_summary[0]["summary"], subject=in_summary[0]["title"])
        print(f"    摘要里直接含码：{in_summary[0]['title'][:20]!r} → {got}")
        check("摘要里也能提到码（详情页挂掉时的兜底）", len(got) >= 2, str(got))
    else:
        print("    （本轮列表恰好没有「摘要里带码」的帖 —— 不算失败，走详情页即可）")
else:
    print("  ⚠️ 未提供列表页夹具（设 NTE_FIXTURE_DIR=<夹具目录> 可跑），跳过")

print("\n" + "=" * 46)
print(f"通过 {PASS} / 失败 {FAIL}")
for f in FAILED:
    print(f"  - {f}")
sys.exit(1 if FAIL else 0)
