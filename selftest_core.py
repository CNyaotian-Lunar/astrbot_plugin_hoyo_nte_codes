"""hoyo_core 离线自测（零依赖，本机直接跑）。

    python selftest_core.py            # 纯离线
    python selftest_core.py --online   # 额外做一次真实米游社抓取验证（可选）
"""

from __future__ import annotations

import json
import sys
import urllib.parse
import urllib.request

import hoyo_core as core

# Windows 控制台默认 GBK，打印 emoji 会炸 —— 强制 UTF-8
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

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


def codes(text: str, **kw) -> list[str]:
    """取 code 列表（方便断言）。"""
    return [c.code for c in core.extract_candidates(text, **kw)]


# ── 真实夹具（取自 2026-09-15 实测的米游社帖子正文）─────────────────────────
IMG = "https://upload-bbs.miyoushe.com/upload/2026/08/28/280612327/05a545cbec27356163eca944c085e15c_2157150092020735303.jpg"
DELTA_ZZZ = json.dumps([
    {"insert": "绝区零3.2前瞻兑换码"},
    {"insert": "_(米游姬-撒花)"},
    {"insert": "\nCLARET0909\n"},
    {"insert": {"image": IMG}, "attributes": {"height": 599}},
    {"insert": "「独家频段」", "attributes": {"bold": True}},
    {"insert": "上半卡池：克拉蕾（S电"},
    {"insert": "锋御", "attributes": {"bold": True}},
    {"insert": "）、南宫羽（S以太击破）"},
    {"insert": "\n"},
    {"insert": "本次兑换码将于8月30日 23:59:59失效，记得及时兑换哦~"},
], ensure_ascii=False)

IMG2 = "https://upload-bbs.miyoushe.com/upload/2026/09/12/280612327/96fb1dba63c4a37a0a1dad82c9dae6a2_7830883352749172291.png"
DELTA_GS = json.dumps([
    {"insert": "原神7.1前瞻兑换码"},
    {"insert": "_(米游姬-撒花)"},
    {"insert": "\n\n往冥府的安魂歌\n风仙薇斯纳为你效劳\n首席女高音沃雅妮莎\n"},
    {"insert": {"image": IMG2}, "attributes": {"height": 1021}},
    {"insert": "\n有效期至 2026-09-14 23:59"},
], ensure_ascii=False)

print("=== [1] delta_to_text ===")
t_zzz = core.delta_to_text(DELTA_ZZZ)
t_gs = core.delta_to_text(DELTA_GS)
check("绝区零正文含 CLARET0909", "CLARET0909" in t_zzz, repr(t_zzz[:80]))
check("图片 URL 不进正文", "upload-bbs" not in t_zzz, repr(t_zzz[:80]))
check("原神正文含三个中文码", all(c in t_gs for c in ("往冥府的安魂歌", "风仙薇斯纳为你效劳", "首席女高音沃雅妮莎")))
check("解析失败时退化剥 HTML", "hi" in core.delta_to_text("<p>hi</p>"))
check("空输入返回空串", core.delta_to_text("") == "" and core.delta_to_text(None) == "")

print("\n=== [2] extract_candidates ===")
check("绝区零提出 CLARET0909", codes(t_zzz, subject="绝区零3.2前瞻兑换码") == ["CLARET0909"],
      str(codes(t_zzz, subject="绝区零3.2前瞻兑换码")))
gs_codes = codes(t_gs, subject="原神7.1前瞻兑换码")
check("原神提出三码（保序）", gs_codes == ["往冥府的安魂歌", "风仙薇斯纳为你效劳", "首席女高音沃雅妮莎"], str(gs_codes))
check("不误抓版本号/日期", not any(x in gs_codes for x in ("2026-09-14", "23:59")))
check("整帖无「兑换」且标题也没有 ⇒ 整帖跳过", codes("CLARET0909\n随便写点什么") == [])

# ⭐ 国服 / 国际服分段（最关键的一条：两者形态完全一样，只能靠文字区分）
REGION_POST = "绝区零3.2前瞻直播兑换码\n国服：CLARET0909\n国际服：flintworks\n前瞻简报……"
check("只取国服码，丢掉国际服码", codes(REGION_POST, subject="绝区零3.2前瞻兑换码") == ["CLARET0909"],
      str(codes(REGION_POST, subject="绝区零3.2前瞻兑换码")))
check("调试时可连国际服一起看",
      set(codes(REGION_POST, subject="绝区零3.2前瞻兑换码", include_global=True)) == {"CLARET0909", "flintworks"})
check("国际服段在前的行不会被算进国服段",
      codes("兑换码\n国际服：ABC12345XY\n国服：CLARET0909", subject="兑换码") == ["CLARET0909"],
      str(codes("兑换码\n国际服：ABC12345XY\n国服：CLARET0909", subject="兑换码")))

# ⭐ 中文停用词必须完全匹配（星铁有真码就叫「兑换码记得换」）
check("真码「这个是兑换码」不被停用词误杀",
      "这个是兑换码" in codes("兑换码\n这个是兑换码\n兑换码记得换", subject="崩铁4.2前瞻兑换码"),
      str(codes("兑换码\n这个是兑换码\n兑换码记得换", subject="崩铁4.2前瞻兑换码")))
check("整行恰好是停用词「兑换码」⇒ 丢弃",
      "兑换码" not in codes("兑换码\n这个是兑换码\n兑换码记得换", subject="崩铁4.2前瞻兑换码"))
check("小标题会被丢（完全匹配 + 正则）",
      codes("兑换码\n前瞻直播\n上半\n卡池", subject="兑换码") == [])

# ASCII 形态
check("星铁 12 位码", codes("兑换码\nNEYSCGAWKE98", subject="星铁前瞻兑换码") == ["NEYSCGAWKE98"])
check("首位是数字的码也认（0729XUSHOU）", codes("兑换码\n国服：0729XUSHOU", subject="兑换码") == ["0729XUSHOU"])
check("纯小写转贴也认（fxlknkwhvurc）", codes("兑换码\nfxlknkwhvurc", subject="兑换码") == ["fxlknkwhvurc"])
check("纯数字不当码", codes("兑换码\n280612327", subject="兑换码") == [])
check("短串不当码", codes("兑换码\nABC", subject="兑换码") == [])
check("版本号不当码", codes("兑换码\n3.2", subject="兑换码") == [])
check("行首引号包着的版本名被丢掉", codes("兑换码\n「挥掷千星的筹码」", subject="兑换码") == [],
      str(codes("兑换码\n「挥掷千星的筹码」", subject="兑换码")))

# 中文孤例
check("中文孤例默认丢弃", codes("兑换码\n有效期至明日", subject="兑换码") == [])

# 🩸 2026-09-21 **实发误报**（群里真的收到了）：正文里的中文**说明句**被当成码推了出去
# （`冬季与绝区零进行冬季联动` 12 字 / `正常抽需要古老月华来兑换抽卡卷` 15 字，tier=B、单作者、
# 01:35:43 推送）。根因 = `CN_TOKEN_RE` 原为 4~15 字，而真中文码最长只有 9 字。
check("中文说明句（12/15 字）不再被当码",
      codes("兑换码\n冬季与绝区零进行冬季联动\n正常抽需要古老月华来兑换抽卡卷",
            subject="星穹铁道4.6前瞻兑换码") == [])
check("句子特征词（进行/需要/已经）单独也要挡住（不靠长度）",
      codes("兑换码\n明日方舟需要博士进行作战部署\n今晚已经开服了",
            subject="明日方舟兑换码") == [])
check("真中文码（≤9 字）仍然保留",
      codes("兑换码\n往冥府的安魂歌\n风仙薇斯纳为你效劳", subject="原神7.1前瞻兑换码")
      == ["往冥府的安魂歌", "风仙薇斯纳为你效劳"])
check("空行隔开的角色名不会被算进码组",
      codes("原神7.1前瞻兑换码\n往冥府的安魂歌\n风仙薇斯纳为你效劳\n首席女高音沃雅妮莎\n\n沃雅妮莎", subject="原神7.1前瞻兑换码")
      == ["往冥府的安魂歌", "风仙薇斯纳为你效劳", "首席女高音沃雅妮莎"],
      str(codes("原神7.1前瞻兑换码\n往冥府的安魂歌\n风仙薇斯纳为你效劳\n首席女高音沃雅妮莎\n\n沃雅妮莎", subject="原神7.1前瞻兑换码")))

print("\n=== [3] resolve_game / make_ds ===")
check("中文名解析", core.resolve_game("原神") == "genshin" and core.resolve_game("崩铁") == "starrail" and core.resolve_game("绝区零") == "zzz")
# 别名需求：「星铁=崩铁=星穹铁道=崩坏：星穹铁道」——这一串必须全认
check("星铁全套别名都认（含全角/半角冒号）",
      all(core.resolve_game(a) == "starrail" for a in
          ["星铁", "崩铁", "星穹铁道", "崩坏星穹铁道", "崩坏：星穹铁道",
           "崩坏:星穹铁道", "starrail", "sr", "hkrpg"]),
      str([(a, core.resolve_game(a)) for a in ["星铁", "崩坏：星穹铁道", "崩坏:星穹铁道"]]))
check("崩坏3 的口语别名都认",
      all(core.resolve_game(a) == "hi3" for a in
          ["崩坏3", "崩坏三", "崩3", "崩三", "崩坏3rd", "bh3", "bbb", "honkai impact 3rd"]),
      str([(a, core.resolve_game(a)) for a in ["崩坏3", "崩坏三", "崩坏3rd"]]))
check("长别名不被短别名抢走（崩坏3rd ≠ 崩坏星穹铁道）",
      core.resolve_game("崩坏3rd前瞻") == "hi3" and core.resolve_game("崩坏：星穹铁道前瞻") == "starrail",
      f'{core.resolve_game("崩坏3rd前瞻")} / {core.resolve_game("崩坏：星穹铁道前瞻")}')
check("带后缀也能解析（用户常带一句话）",
      core.resolve_game("崩铁的码") == "starrail" and core.resolve_game("崩坏三兑换码") == "hi3",
      f'{core.resolve_game("崩铁的码")} / {core.resolve_game("崩坏三兑换码")}')
check("游戏提示文案由注册表动态生成（加游戏不用改文案）",
      all(core.GAMES[g]["short"] in core.GAME_HINT for g in core.GAMES), core.GAME_HINT)
check("空输入=全部", core.resolve_game("") is None)
check("无匹配返回 None", core.resolve_game("塞尔达") is None)
ds = core.make_ds("原神前瞻兑换码", t=1700000000, r=123456)
parts = ds.split(",")
check("DS 格式 <t>,<r>,<md5>", len(parts) == 3 and parts[0] == "1700000000" and len(parts[2]) == 32, ds)
check("DS 可复现", core.make_ds("abc", t=1, r=2) == core.make_ds("abc", t=1, r=2))
check("DS 随 q 变化", core.make_ds("a", t=1, r=2) != core.make_ds("b", t=1, r=2))

print("\n=== [4] collect_codes：按「不同作者」分层 ===")


def rec(pid: str, uid: str, title: str, code: str, kind: str = "ascii") -> dict:
    return {"post_id": pid, "uid": uid, "title": title, "candidates": [core.Candidate(code, kind, "cn")]}


hits = core.collect_codes([
    rec("p1", "u1", "【原神7.1】前瞻兑换码", "往冥府的安魂歌", "cn"),
    rec("p2", "u2", "原神7.1前瞻兑换码", "往冥府的安魂歌", "cn"),
], "genshin")
check("2 位作者 ⇒ A 级", hits and hits[0].tier == "A" and hits[0].authors == 2, str(hits))

hits_same = core.collect_codes([
    rec("p1", "u1", "绝区零前瞻兑换码", "CLARET0909"),
    rec("p2", "u1", "绝区零3.2前瞻兑换码", "CLARET0909"),  # 同一作者发两帖
], "zzz")
check("⚠️ 同一作者发 2 帖只算 1 位作者（不能按帖数）", hits_same[0].authors == 1, str(hits_same[0].authors))

hits_b = core.collect_codes([
    {"post_id": "p1", "uid": "u1", "title": "绝区零3.2前瞻兑换码",
     "candidates": [core.Candidate("CLARET0909", "ascii", "cn"), core.Candidate("ZZZINK32", "ascii", "cn")]},
], "zzz")
check("单作者 + 标题带版本号 + 同帖多候选 ⇒ B 级", hits_b[0].tier == "B", str(hits_b[0].tier))

hits_c = core.collect_codes([rec("p1", "u1", "随便聊聊", "RANDOMSTR1")], "zzz")
check("单作者 + 标题无版本号 ⇒ C 级（默认不推）", hits_c[0].tier == "C", str(hits_c[0].tier))

hits_f4 = core.collect_codes([
    # ⚠️ 必须提供正文：标题复读过滤只在「词只出现在标题、正文里没有」时才丢（没正文时保守不丢）
    {"post_id": "p1", "uid": "u1", "title": "关于兑换码的那些事", "text": "随便聊聊别的话题",
     "candidates": [core.Candidate("关于兑换码的那些事", "cn", "cn")]},
], "zzz")
check("标题复读（正文里没有）被丢掉", hits_f4 == [], str(hits_f4))
check("真码写进标题但正文也有 ⇒ 不丢",
      core.collect_codes([
          {"post_id": "p1", "uid": "u1", "title": "原神7.1前瞻兑换码 往冥府的安魂歌",
           "text": "往冥府的安魂歌",
           "candidates": [core.Candidate("往冥府的安魂歌", "cn", "cn")]},
      ], "genshin") != [])

check("排序：A 在 B 前、B 在 C 前",
      [h.tier for h in core.collect_codes([
          rec("pc", "u9", "先", "CODEAAAAAA1"),
          rec("pa", "u1", "后", "CODEBBBBBB2"), rec("pa2", "u2", "后", "CODEBBBBBB2"),
      ], "zzz")] == ["A", "C"])

print("\n=== [5] 新码判定 / 状态 ===")
seen: dict = {}
new1 = core.diff_new_codes(seen, hits)
check("空 seen 时全部算新（调用方决定是否静默建基线）", len(new1) == len(hits))
core.mark_seen(seen, hits)
check("mark_seen 后无新码", core.diff_new_codes(seen, hits) == [])
check("seen 计数正确", core.seen_count(seen) == 1)
fresh_hits = core.collect_codes([rec("p9", "u7", "标题", "NEWCODE123"), rec("p10", "u8", "标题", "NEWCODE123")], "genshin")
new2 = core.diff_new_codes(seen, fresh_hits)
check("只报真正的新码", [h.code for h in new2] == ["NEWCODE123"])
core.mark_seen(seen, fresh_hits)
check("新码入库后不再报", core.diff_new_codes(seen, fresh_hits) == [])
core.mark_seen(seen, hits)  # 幂等
check("mark_seen 幂等", core.seen_count(seen) == 2)
check("大小写不敏感去重",
      core.diff_new_codes({"zzz": {"claret0909": {}}}, [core.CodeHit(code="CLARET0909", game="zzz")]) == [])
core.mark_seen(seen, core.collect_codes([rec("pX", "uX", "t", "NEWCODE123")], "genshin"))
check("mark_seen 累加新作者", len(seen["genshin"]["newcode123"]["uids"]) == 3, str(seen["genshin"]["newcode123"]["uids"]))

print("\n=== [5b] 保留策略（默认清 180 天以上 + 指纹墓碑）===")
import datetime as _dt

_now = _dt.datetime(2026, 9, 15, 12, 0, 0, tzinfo=core.TZ)
_old = (_now - _dt.timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S")
_fresh = (_now - _dt.timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
store: dict = {
    "zzz": {
        "ancient01": {"code": "ANCIENT01", "first_seen": _old, "last_seen": _old, "uids": ["u1"]},
        "recent01": {"code": "RECENT01", "first_seen": _fresh, "last_seen": _fresh, "uids": ["u1"]},
        "badts": {"code": "BADTS1", "first_seen": "乱七八糟", "last_seen": "乱七八糟", "uids": []},
    }
}
tomb: dict = {}
removed = core.prune_seen(store, retention_days=180, tombstones=tomb, now=_now)
check("清掉超期条目", removed == ["ANCIENT01"], str(removed))
check("保留新鲜条目", "recent01" in store["zzz"])
check("时间戳非法不清理（宁留不错清）", "badts" in store["zzz"])
check("墓碑记下指纹", core.code_fingerprint("ANCIENT01") in tomb["zzz"])
check("被清的旧码重现→不误报",
      core.diff_new_codes(store, [core.CodeHit(code="ANCIENT01", game="zzz")], tombstones=tomb) == [])
check("被清后新码照常上报",
      [h.code for h in core.diff_new_codes(store, [core.CodeHit(code="BRANDNEW99", game="zzz")], tombstones=tomb)] == ["BRANDNEW99"])
check("retention_days<=0 = 不清理",
      core.prune_seen({"zzz": {"x": {"code": "X1", "first_seen": _old, "last_seen": _old}}}, retention_days=0, now=_now) == [])
t2: dict = {"zzz": []}
for i in range(2100):
    core.prune_seen(
        {"zzz": {f"k{i}": {"code": f"CODE{i:05d}", "first_seen": _old, "last_seen": _old}}},
        retention_days=180, tombstones=t2, now=_now,
    )
check("墓碑有上限，不会无限增长", len(t2["zzz"]) <= 2000, str(len(t2["zzz"])))

print("\n=== [6] 渲染 ===")
msg = core.render_new_codes_message(hits)
check("推送消息含标题", "发现新的前瞻兑换码" in msg)
check("推送消息含码与作者数", "往冥府的安魂歌" in msg and "2 位作者确认" in msg)
check("推送按游戏分组", "【原神】" in msg)
q = core.render_codes("genshin", hits, header="当前兑换码")
check("查询渲染含游戏名与码", "【原神】" in q and "往冥府的安魂歌" in q)
check("查询默认展示 C 级（直播刚结束时单作者真码不该被藏起来）",
      "RANDOMSTR1" in core.render_codes("zzz", hits_c, header="当前兑换码"))
check("可用 show_tier_c=False 关掉 C 级",
      core.render_codes("zzz", hits_c, header="当前兑换码", show_tier_c=False).count("RANDOMSTR1") == 0)
check("空结果有兜底文案", "没有查到" in core.render_codes("genshin", [], header="当前兑换码"))
# 2026-09-16 沿革：① 最早已过期的码排在**最前面**（用户第一眼看到的是没用的）
#   ② 改成"降到末尾单独成组" ③ 需求「**已经明确过期的话是不是能不展示了**」
#   ⇒ 现在**默认压根不显示**，要核对旧码得显式传 `show_expired=True`。
_h_dead = core.CodeHit(code="EXPIREDCODE1", game="genshin", uids=["u1", "u2"],
                       expire_at=_dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=core.TZ))
_h_live = core.CodeHit(code="LIVECODE123", game="genshin", uids=["u1", "u2"])
_q = core.render_codes("genshin", [_h_dead, _h_live], header="当前兑换码")
check("默认**不显示**已过期的码",
      "EXPIREDCODE1" not in _q and "LIVECODE123" in _q, _q)
check("默认连「另有 N 个已过期」也不提", "已过期，仅供核对" not in _q, _q)
_q2 = core.render_codes("genshin", [_h_dead, _h_live], header="当前兑换码", show_expired=True)
check("show_expired=True 时才列出，且降到末尾（第一眼看到的是能用的）",
      _q2.index("LIVECODE123") < _q2.index("EXPIREDCODE1") and "已过期，仅供核对" in _q2, _q2)
check("全部过期时给出兜底文案",
      "当前没有未过期的兑换码" in core.render_codes("genshin", [_h_dead], header="当前兑换码"))
check("截断工具", core.truncate("abcdef", 4) == "abc…" and core.truncate("ab", 4) == "ab")

print("\n=== [6c] 查询结果拆成多条（合并转发用；每个码独占一条）===")
_mlive = core.CodeHit(code="LIVECODE123", game="genshin", uids=["u1", "u2"])
_mdead = core.CodeHit(code="EXPIREDCODE1", game="genshin", uids=["u1"],
                      expire_at=_dt.datetime(2026, 9, 1, 0, 0, 0, tzinfo=core.TZ))
_msgs3 = core.render_codes_messages(
    "genshin", [_mlive, _mdead], header="当前兑换码", show_expired=True
)
print(f"    拆成 {len(_msgs3)} 条：")
for _m in _msgs3:
    print(f"      | {_m!r}")
check("第一条是头部（游戏名 + 图例 + 汇总）",
      _msgs3[0].startswith("【原神】当前兑换码") and "交叉验证" in _msgs3[0], _msgs3[0])
check("每个码**独占一条**（内容就是码本身，长按复制不会带上别的）",
      any(m.strip() == "LIVECODE123" for m in _msgs3)
      and any(m.strip() == "EXPIREDCODE1" for m in _msgs3), str(_msgs3))
check("紧跟一条写来源与时效", any(("官方活动源" in m or "位作者" in m) and "已过期" in m for m in _msgs3),
      str(_msgs3))
check("最后一条是免责说明", _msgs3[-1] == core.DISCLAIMER, _msgs3[-1])
check("空结果有兜底", "没有查到" in core.render_codes_messages("genshin", [], header="当前兑换码")[0])

print("\n=== [6b] 有效期识别（实测语料的真实写法）===")
_T = _dt.datetime(2026, 9, 15, 10, 0, 0, tzinfo=core.TZ)
exp_cases = [
    ("原神7.1前瞻兑换码有效期至2026年9月15日12：00", _dt.datetime(2026, 9, 15, 12, 0, 59, tzinfo=core.TZ)),
    ("2026/08/30 23:59:59前有效", _dt.datetime(2026, 8, 30, 23, 59, 59, tzinfo=core.TZ)),  # 过去的也提，用来标「已过期」
    ("(兑换码将于2026年8月15日23:59:59后失效)", None),  # 31 天前 → 超出 30 天窗口，按误提丢弃
    ("4-兑换码将于2026年10月31日 00:00 失效，记得尽快兑换哦~", _dt.datetime(2026, 10, 31, 0, 0, 59, tzinfo=core.TZ)),
    ("有效期至2026-09-16", _dt.datetime(2026, 9, 16, 23, 59, 59, tzinfo=core.TZ)),
]
for text, expect in exp_cases:
    got = core.extract_expiry(text, now=_T)
    check(f"提「{text[:26]}…」", got == expect, f"实际={got} 期望={expect}")

check("邮件有效期（30天）不会被当成码的有效期",
      core.extract_expiry("使用兑换码后奖励以邮件发送，邮件有效期为30天", now=_T) is None)
check("没有时间信息 ⇒ None", core.extract_expiry("兑换码有效期短，尽快使用", now=_T) is None)
check("「（已过期）」标记可识别",
      core.has_expired_marker("1.2版本「火狱骑行」前瞻直播兑换码（已过期）")
      and not core.has_expired_marker("原神7.1前瞻兑换码"))
check("该帖没有别的日期时不会误提活动时间",
      core.extract_expiry("活动时间：2026年9月20日 19:30 开播", now=_T) is None)

# ⭐ 星铁 4.6 前瞻当晚挖出的两个**实测缺陷**（用例：「如果明确知道 9.21 23:59
# 过期的话 为啥不写上呢」）：
#   ① **有效期只写在标题里** ⇒ 旧实现只扫正文（对照：`extract_live_start` 一直支持 `subject=`）
#      ⇒ 星铁 3 个 4.6 码的 `expire_at` 全是 None；而同一批帖里正文写了绝对时间的就能提到
#      ⇒ **同一枚码在两条链路上结果不一致**，就是从这里露的马脚。
#   ② **相对日期不认**（明天/今晚/次日）⇒ `DATE_TIME_RE` 只吃绝对日期，玩家最高频的写法反而提不到。
_POSTED = _dt.datetime(2026, 9, 20, 20, 26, 0, tzinfo=core.TZ)
_EXP = _dt.datetime(2026, 9, 21, 23, 59, 59, tzinfo=core.TZ)
check("【回归】有效期写在**标题**里也能提到（此前只看正文）",
      core.extract_expiry("兑换码\nKXHN8W7FGB6U",
                          subject="星穹铁道4.6前瞻直播兑换码 截止时间明天晚上23:59",
                          posted_at=_POSTED, now=_POSTED) == _EXP)
check("【回归】相对日期以**发帖时间**为基准（抓晚了也不会算成「明天」）",
      core.extract_expiry("兑换码\nKXHN8W7FGB6U\n截止时间明天晚上23:59",
                          posted_at=_POSTED,
                          now=_dt.datetime(2026, 9, 25, 12, 0, tzinfo=core.TZ)) == _EXP)
check("【回归】「今晚」= 发帖当天 23:59",
      core.extract_expiry("兑换码\nX\n今晚23:59过期", posted_at=_POSTED, now=_POSTED)
      == _dt.datetime(2026, 9, 20, 23, 59, 59, tzinfo=core.TZ))
check("【回归】「次日」带具体时刻也能提",
      core.extract_expiry("兑换码\nX\n有效期至次日12:00", posted_at=_POSTED, now=_POSTED)
      == _dt.datetime(2026, 9, 21, 12, 0, 59, tzinfo=core.TZ))
check("版本号「4.6」不会被当成日期误提",
      core.extract_expiry("兑换码\nKXHN8W7FGB6U\n有效期见图片",
                          subject="崩坏星穹铁道4.6版本前瞻兑换码",
                          posted_at=_POSTED, now=_POSTED) is None)

print("\n=== [6d] 账本刷新：mark_seen(create=False) 只更新、不新增 / tier 只升不降 ===")
# 2026-09-20：修好「有效期写在标题里也能提到」之后，**存量账本仍是旧的**（expire 为空），
# 因为 check_once 平时只对"新码"调 mark_seen。所以加了 create=False 的刷新路径。
_seen = {"starrail": {"kxhn8w7fgb6u": {
    "code": "KXHN8W7FGB6U", "game": "starrail", "tier": "A",
    "first_seen": "2026-09-20 22:55:58", "last_seen": "2026-09-20 22:55:58",
    "uids": ["u1"], "posts": ["p1"], "expire_at": ""}}}
_h_refresh = core.CodeHit(code="KXHN8W7FGB6U", game="starrail", uids=["u2"], posts=["p2"])
_h_refresh.tier = "A"
_h_refresh.expire_at = _EXP
core.mark_seen(_seen, [_h_refresh], create=False)
check("create=False：给**已有**条目补上有效期",
      _seen["starrail"]["kxhn8w7fgb6u"]["expire_at"].startswith("2026-09-21"),
      repr(_seen["starrail"]["kxhn8w7fgb6u"]["expire_at"]))
check("create=False：合并新的作者与帖子",
      "u2" in _seen["starrail"]["kxhn8w7fgb6u"]["uids"]
      and "p2" in _seen["starrail"]["kxhn8w7fgb6u"]["posts"])
_h_new = core.CodeHit(code="BRANDNEW999", game="starrail", uids=["u9"])
core.mark_seen(_seen, [_h_new], create=False)
check("create=False：**绝不新增**条目（否则 C 级码会挡住将来它升到 A 级时的推送）",
      "brandnew999" not in _seen["starrail"] and "brandnew999" not in (_seen.get("_global") or {}),
      str(list(_seen["starrail"])))
_h_down = core.CodeHit(code="KXHN8W7FGB6U", game="starrail")
_h_down.tier = "C"
core.mark_seen(_seen, [_h_down], create=False)
check("tier **只升不降**（样本变少不该让码在查询里直接消失）",
      _seen["starrail"]["kxhn8w7fgb6u"]["tier"] == "A",
      repr(_seen["starrail"]["kxhn8w7fgb6u"]["tier"]))
_h_early = core.CodeHit(code="KXHN8W7FGB6U", game="starrail")
_h_early.tier = "A"
_h_early.expire_at = _dt.datetime(2026, 9, 21, 6, 0, tzinfo=core.TZ)
core.mark_seen(_seen, [_h_early], create=False)
check("create=False：有效期取**更早**的那个（宁可提示人早换）",
      _seen["starrail"]["kxhn8w7fgb6u"]["expire_at"].startswith("2026-09-21T06"),
      repr(_seen["starrail"]["kxhn8w7fgb6u"]["expire_at"]))

print("\n=== [6e] 有效期来源优先级：社区明写 > 官方估算 ===")
# 背景：官方 miyolive **并不提供码的有效期**，代码里的值是「直播结束 + 3 天」的**自行估算**；
# 社区帖写的是精确失效时间。原神 7.1 实测：估算 = 09-15 21:16:10、社区明写 = 09-15 12:00 ——
# 差 **9 小时**，用估算的话那 9 小时里查询会误报"还能换"。
# 需求：「按照社区帖子的时间吧 社区一般更准 官方那个没更新过期时间其实」。
_EST = _dt.datetime(2026, 9, 15, 21, 16, 10, tzinfo=core.TZ)
_SRC = _dt.datetime(2026, 9, 15, 12, 0, 59, tzinfo=core.TZ)
check("裁决：明确写的顶掉估算的（即便估算更早）", core._prefer_expiry(_SRC, False, _EST, True))
check("裁决：估算的**不顶**明确写的", not core._prefer_expiry(_EST, True, _SRC, False))
check("裁决：同级取更早",
      core._prefer_expiry(_SRC, False, _EST, False)
      and not core._prefer_expiry(_EST, False, _SRC, False))
check("裁决：旧的为空 ⇒ 接受新的", core._prefer_expiry(_EST, True, None, None))

_official_rec = {
    "post_id": "miyolive:CODE123", "title": "官方前瞻直播活动", "uid": "official:miyolive",
    "candidates": [core.Candidate("CODE123", "ascii", "cn")],
    "expire_at": _EST, "expire_estimated": True,
}
_community_rec = {
    "post_id": "p1", "title": "原神7.1前瞻兑换码", "uid": "u1",
    "candidates": [core.Candidate("CODE123", "ascii", "cn")],
    "expire_at": _SRC, "expire_estimated": False,
}
_hit = core.collect_codes([_official_rec, _community_rec], "genshin")[0]
check("聚合：官方估算**不会盖掉**社区的明确时间", _hit.expire_at == _SRC, repr(_hit.expire_at))
check("聚合：结果被标成「非估算」", _hit.expire_estimated is False, repr(_hit.expire_estimated))
_hit2 = core.collect_codes([_official_rec], "genshin")[0]
check("只有官方源时，估算照旧兜底（只是被标为估算）",
      _hit2.expire_at == _EST and _hit2.expire_estimated is True,
      f"{_hit2.expire_at} est={_hit2.expire_estimated}")

_seen2 = {"genshin": {"code123": {
    "code": "CODE123", "game": "genshin", "tier": "A",
    "first_seen": "2026-09-16 05:29:03", "last_seen": "2026-09-16 05:29:03",
    "uids": ["official:miyolive"], "posts": ["miyolive:CODE123"],
    "expire_at": _EST.isoformat()}}}
_h3 = core.CodeHit(code="CODE123", game="genshin", uids=["u1"])
_h3.expire_at, _h3.expire_estimated = _SRC, False
core.mark_seen(_seen2, [_h3], create=False)
check("刷新：社区的明确时间能覆盖**老账本**里的官方估算值",
      _seen2["genshin"]["code123"]["expire_at"].startswith("2026-09-15T12"),
      repr(_seen2["genshin"]["code123"]["expire_at"]))
check("刷新：并记下「这是明确写的」",
      _seen2["genshin"]["code123"].get("expire_estimated") is False,
      repr(_seen2["genshin"]["code123"].get("expire_estimated")))
_back = [h for h in core.hits_from_seen(_seen2, "genshin") if h.code == "CODE123"][0]
check("还原：expire_estimated 从账本读回", _back.expire_estimated is False, repr(_back.expire_estimated))
check("还原：老账本缺这个键 ⇒ 未知（不是「明确」）",
      core.hits_from_seen({"genshin": {"old1": {
          "code": "OLD1", "game": "genshin", "tier": "A", "uids": ["u1"], "posts": ["p"],
          "expire_at": _EST.isoformat()}}}, "genshin")[0].expire_estimated is None)

print("\n=== [6f] 对抗性审查的回归用例（时间写法 / 词边界 / 长行 / 跨行 / 账本）===")
# 这些是对抗性审查挖出的问题，多数是「**偏晚**」这一**危险方向**
# （用户以为还有时间、其实早废了）。当年自测 125/0 全绿却一条都覆盖不到。
_P2_POSTED = _dt.datetime(2026, 9, 20, 21, 0, tzinfo=core.TZ)
_P2_NOW = _dt.datetime(2026, 9, 20, 23, 20, tzinfo=core.TZ)


def _p2(text, subject=""):
    return core.extract_expiry(text, subject=subject, posted_at=_P2_POSTED, now=_P2_NOW)


for _label, _text, _want in [
    ("9月21号12:00", "兑换码\nX\n有效期9月21号12:00", "2026-09-21 12:00"),
    ("9月21日12点", "兑换码\nX\n有效期9月21日12点", "2026-09-21 12:00"),
    ("9月21日12时00分", "兑换码\nX\n有效期9月21日12时00分", "2026-09-21 12:00"),
    ("明天中午", "兑换码\nX\n有效期至明天中午", "2026-09-21 12:00"),
    ("次日凌晨", "兑换码\nX\n有效期至次日凌晨", "2026-09-21 00:00"),
    ("9月21日0点", "兑换码\nX\n有效期至9月21日0点", "2026-09-21 00:00"),
]:
    _got = _p2(_text)
    check(f"时间写法「{_label}」不再退化成 23:59（偏晚方向）",
          _got is not None and _got.strftime("%Y-%m-%d %H:%M") == _want,
          f"实际={_got} 期望={_want}")

check("「有效期24小时」不凭空猜出日期", _p2("兑换码\nX\n有效期24小时") is None)
check("「明日方舟」不凭空造有效期",
      _p2("明日方舟联动开启，兑换码有效期以游戏内为准") is None)
check("「《明天》」不凭空造有效期",
      _p2("《明天》兑换码有效期以游戏内为准") is None)
check("排除词出现在**日期之后**不该杀整行",
      _p2("兑换码\nX\n有效期至9月21日23:59（版本更新后请重进游戏）") is not None)
check("标题「（有效期见下）」+ 正文首行能组合",
      _p2("明天23:59\n兑换码\nX", subject="星铁4.6前瞻兑换码（有效期见下）") is not None)
_LONG_LINE = "兑换码 X\n" + "啰嗦" * 80 + " 有效期至9月21日23:59 " + "废话" * 80
check("超长段落里的有效期能提到（原来整行跳过）", _p2(_LONG_LINE) is not None)
_p2_sec = _p2("兑换码\nX\n有效期至2026年9月21日23:59:30")
check("写全秒要保留（绝对与相对路径同口径）",
      _p2_sec is not None and _p2_sec.second == 30, repr(_p2_sec))
check("naive datetime 不偏一天",
      core.extract_expiry("兑换码\nX\n截止时间明天晚上23:59",
                          posted_at=_P2_POSTED.replace(tzinfo=None),
                          now=_P2_NOW.replace(tzinfo=None)) is not None)

# 账本侧（不建空桶 / 截断到上限 / 统一大写落账；「值没变时保留标记」由 mark_seen() 内部处理）
_s_f4 = {}
core.mark_seen(_s_f4, [core.CodeHit(code="Q1", game="zzz")], create=False)
check("create=False 不建空桶", _s_f4 == {}, str(_s_f4))
_s_f5 = {}
core.mark_seen(_s_f5, [core.CodeHit(code="Q5", game="zzz",
                                    uids=[f"u{i}" for i in range(50)],
                                    posts=[f"p{i}" for i in range(50)])])
check("uids/posts 落账时截断到上限",
      len(_s_f5["zzz"]["q5"]["uids"]) == core.SEEN_LIST_LIMIT
      and len(_s_f5["zzz"]["q5"]["posts"]) == core.SEEN_LIST_LIMIT,
      f"uids={len(_s_f5['zzz']['q5']['uids'])}")
_s_f5b = {"zzz": {"old1": {
    "code": "OLD1", "game": "zzz", "tier": "A",
    "first_seen": (_P2_NOW - _dt.timedelta(days=200)).strftime("%Y-%m-%d %H:%M:%S"),
    "last_seen": _P2_NOW.strftime("%Y-%m-%d %H:%M:%S"),
    "uids": [], "posts": [], "expire_at": ""}}}
check("last_seen 被每轮刷新也照样回收（改用 first_seen 判据）",
      "OLD1" in core.prune_seen(_s_f5b, retention_days=180, tombstones={}, now=_P2_NOW))
_s_f6 = {}
core.mark_seen(_s_f6, [core.CodeHit(code="Q6", game="zzz", tier="a")])
check("tier 统一大写落账（小写会让渲染标记变成孤零零的 `·`）",
      _s_f6["zzz"]["q6"]["tier"] == "A", repr(_s_f6["zzz"]["q6"]["tier"]))

_h_future = core.CodeHit(code="AAA1112223", game="zzz", uids=["u1", "u2"])
_h_future.expire_at = _T + _dt.timedelta(hours=5)
check("渲染：小时级倒计时", "小时后过期" in core.expiry_suffix(_h_future, now=_T), core.expiry_suffix(_h_future, now=_T))
_h_soon = core.CodeHit(code="BBB1112223", game="zzz")
_h_soon.expire_at = _T + _dt.timedelta(minutes=20)
check("渲染：分钟级倒计时", "分钟后过期" in core.expiry_suffix(_h_soon, now=_T), core.expiry_suffix(_h_soon, now=_T))
_h_past = core.CodeHit(code="CCC1112223", game="zzz")
_h_past.expire_at = _T - _dt.timedelta(minutes=1)
check("渲染：已过期", "已过期" in core.expiry_suffix(_h_past, now=_T), core.expiry_suffix(_h_past, now=_T))
check("没有有效期就不渲染后缀", core.expiry_suffix(core.CodeHit(code="D", game="zzz"), now=_T) == "")
_real = _dt.datetime.now(core.TZ)
_hits_exp = core.collect_codes([
    {"post_id": "p1", "uid": "u1", "title": "原神7.1前瞻兑换码", "expire_at": _real + _dt.timedelta(hours=9),
     "candidates": [core.Candidate("往冥府的安魂歌", "cn", "cn")]},
    {"post_id": "p2", "uid": "u2", "title": "原神7.1前瞻兑换码", "expire_at": _real + _dt.timedelta(hours=4),
     "candidates": [core.Candidate("往冥府的安魂歌", "cn", "cn")]},
], "genshin")
check("同一码取最早的有效期（宁可提示早换）",
      abs((_hits_exp[0].expire_at - (_real + _dt.timedelta(hours=4))).total_seconds()) < 2,
      str(_hits_exp[0].expire_at))
check("推送渲染带上有效期", "小时后过期" in core.render_new_codes_message(_hits_exp),
      core.render_new_codes_message(_hits_exp)[:140])

print("\n=== [6c] 合并转发推送的消息拆分（每个码独占一条，方便长按复制）===")
_msgs = core.render_push_messages(hits)
check("条数 = 游戏数 + 码数 + 1（来源）", len(_msgs) == 1 + len(hits) + 1, str(_msgs))
check("每个码独占一条消息", all(h.code in _msgs for h in hits), str(_msgs))
check("标题带「前瞻兑换码」", "前瞻兑换码" in _msgs[0], _msgs[0])
check("末尾是来源", _msgs[-1].startswith("来源："), _msgs[-1])
check("两作者写「2 位作者交叉验证」", "2 位作者" in _msgs[-1], _msgs[-1])
_msgs2 = core.render_push_messages([core.CodeHit(code="X1", game="zzz", uids=["official:miyolive"], official=True)])
check("官方源标「官方活动源」", "官方活动源" in _msgs2[-1], _msgs2[-1])
check("多游戏时每个游戏各有一条标题",
      len(core.render_push_messages([
          core.CodeHit(code="A1", game="genshin", uids=["u1", "u2"]),
          core.CodeHit(code="B1", game="zzz", uids=["u3", "u4"]),
      ])) == 2 + 2 + 1)

print("\n=== [6d] 前瞻直播时间 / 直播间识别 ===")
_T2 = _dt.datetime(2026, 9, 16, 10, 0, 0, tzinfo=core.TZ)
check("从官方预告帖提开播时间",
      core.extract_live_start(
          "《崩坏：星穹铁道》4.6版本前瞻特别节目预告\n9月20日 19:30 开播", now=_T2
      ) == _dt.datetime(2026, 9, 20, 19, 30, 0, tzinfo=core.TZ),
      str(core.extract_live_start("9月20日 19:30 开播", now=_T2)))
check("普通兑换码帖不会被当成前瞻预告",
      core.extract_live_start("原神7.1前瞻兑换码\n往冥府的安魂歌", subject="原神7.1前瞻兑换码", now=_T2) is None)
check("提 B站直播间 room_id",
      core.extract_live_room("直播间：https://live.bilibili.com/21987615") == "21987615")
check("标题判定 is_live_title",
      core.is_live_title("《绝区零》3.2版本前瞻特别节目预告") and not core.is_live_title("绝区零3.2前瞻兑换码"))
_live_msg = core.render_live_notice(
    [{"game": "genshin", "title": "原神7.1前瞻", "start": _T2 + _dt.timedelta(minutes=30), "room": "21987615"}],
    now=_T2,
)
check("提醒含 B站直播间链接", "live.bilibili.com/21987615" in _live_msg, _live_msg)
check("提醒写「分钟后开播」", "分钟后开播" in _live_msg, _live_msg)
check("提醒兜底用配置里的 room",
      "live.bilibili.com/32805602" in core.render_live_notice(
          [{"game": "zzz", "title": "绝区零3.2前瞻", "start": None}], now=_T2, rooms={"zzz": "32805602"}
      ))

if "--online" in sys.argv:
    print("\n=== [7] 在线冒烟（真实米游社，只读）===")
    q = "绝区零前瞻兑换码"
    url = f"https://bbs-api.mihoyo.com/post/wapi/searchPosts?keyword={urllib.parse.quote(q)}&size=5&offset=0"
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
        "Referer": "https://www.miyoushe.com/",
        "DS": core.make_ds(q),
        "x-rpc-app_version": "2.71.1",
        "x-rpc-client_type": "4",
        "x-rpc-language": "zh-cn",
    })
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        check("搜索接口 retcode=0", data.get("retcode") == 0, str(data.get("message")))
        payload = data.get("data") or {}
        lst = payload.get("list") or payload.get("posts") or []
        check("拿到帖子列表（data.posts）", len(lst) > 0, f"len={len(lst)}")
        if lst:
            first = lst[0].get("post") or {}
            print(f"     首条: {core.truncate(str(first.get('subject')), 40)} (id={first.get('post_id')}, uid={first.get('uid')})")
    except Exception as exc:  # noqa: BLE001
        check("在线请求成功", False, repr(exc))

print(f"\n{'=' * 46}\n通过 {PASS} / 失败 {FAIL}")
if FAILED:
    print("失败项：")
    for f in FAILED:
        print("  -", f)
sys.exit(1 if FAIL else 0)
