"""真机冒烟：用真实米游社数据跑一遍三游戏抓取，人工核对候选码质量。

与插件同款规则：7 天时效窗口 + 连续成组的中文码 + 排除作者 uid。
    python smoke_online.py           # 只读，不发任何东西
"""

from __future__ import annotations

import json
import sys
import time
import urllib.parse
import urllib.request

import hoyo_core as core

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
)
BASE_H = {"User-Agent": UA, "Referer": "https://www.miyoushe.com/", "Accept": "application/json"}
SLEEP = 1.4
MAX_AGE_DAYS = 7
MAX_POSTS = 4


def retry(fn, times: int = 3, label: str = ""):
    last = None
    for i in range(times):
        try:
            return fn()
        except Exception as exc:  # noqa: BLE001
            last = exc
            if i < times - 1:
                time.sleep(1.5 * (i + 1))
    raise RuntimeError(f"{label} 重试 {times} 次仍失败：{last}")


def search(kw: str, size: int = 10) -> dict:
    url = f"https://bbs-api.mihoyo.com/post/wapi/searchPosts?keyword={urllib.parse.quote(kw)}&size={size}&offset=0"
    headers = dict(BASE_H)
    headers.update({
        "DS": core.make_ds(kw),
        "x-rpc-app_version": "2.71.1",
        "x-rpc-client_type": "4",
        "x-rpc-language": "zh-cn",
    })

    def _do() -> dict:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    return retry(_do, label=f"搜索 {kw}")


def post(pid: str) -> dict:
    url = f"https://bbs-api.mihoyo.com/post/wapi/getPostFull?post_id={pid}&read=1"

    def _do() -> dict:
        with urllib.request.urlopen(urllib.request.Request(url, headers=BASE_H), timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    return retry(_do, label=f"取帖 {pid}")


cutoff = time.time() - MAX_AGE_DAYS * 86400
total = 0
for game, meta in core.GAMES.items():
    print("=" * 66)
    print(f"{game}  ({meta['name']})   时效窗口：{MAX_AGE_DAYS} 天")
    ids: list[str] = []
    for kw in meta["keywords"][:2]:
        try:
            data = search(kw)
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ 搜索失败：{exc}")
            continue
        payload = data.get("data") or {}
        got = payload.get("list") or payload.get("posts") or []
        fresh = 0
        for item in got:
            p = item.get("post") or {}
            created = p.get("created_at") or 0
            if created and created < cutoff:
                continue
            fresh += 1
            if p.get("post_id"):
                ids.append(str(p["post_id"]))
        print(f"  搜索『{kw}』→ {len(got)} 条，其中 {fresh} 条在时效窗口内")
        time.sleep(SLEEP)

    ids = list(dict.fromkeys(ids))[:MAX_POSTS]
    per: dict[str, tuple[str, list[str]]] = {}
    for pid in ids:
        try:
            data = post(pid)
        except Exception as exc:  # noqa: BLE001
            print(f"  ❌ 取帖失败 {pid}：{exc}")
            continue
        wrapper = (data.get("data") or {}).get("post") or {}
        p = wrapper.get("post") or {}
        u = wrapper.get("user") or {}
        text = core.delta_to_text(p.get("structured_content"))
        codes = core.extract_candidates(text, exclude=[pid, str(u.get("uid") or "")], min_cn_group=2)
        title = str(p.get("subject") or "")
        per[pid] = (title, codes)
        print(f"  帖 {pid}｜{core.truncate(title, 34)}｜→ {codes}")
        time.sleep(SLEEP)

    hits = core.collect_codes(per, game)
    total += len(hits)
    print("  >>> 汇总（码, 来源数）：", [(h.code, h.confidence) for h in hits])

print("=" * 66)
print(f"合计候选码 {total} 个")
