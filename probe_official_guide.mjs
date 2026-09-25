// 验证「官方频道攻略接口」circle/channel/guide/material —— 三游戏是否都有官方码源
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36';
const H = { 'User-Agent': UA, Referer: 'https://www.miyoushe.com/', Accept: 'application/json' };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

const TARGETS = [
  ['国服(mihoyo)-原神 gid=2', 'https://bbs-api.mihoyo.com/community/painter/wapi/circle/channel/guide/material?game_id=2'],
  ['国服(mihoyo)-星铁 gid=6', 'https://bbs-api.mihoyo.com/community/painter/wapi/circle/channel/guide/material?game_id=6'],
  ['国服(mihoyo)-绝区零 gid=8', 'https://bbs-api.mihoyo.com/community/painter/wapi/circle/channel/guide/material?game_id=8'],
  ['国服(miyoushe)-星铁 gid=6', 'https://bbs-api.miyoushe.com/community/painter/wapi/circle/channel/guide/material?game_id=6'],
  ['国际(hoyolab)-星铁 gid=6', 'https://bbs-api-os.hoyolab.com/community/painter/wapi/circle/channel/guide/material?game_id=6'],
  ['国际(hoyolab)-绝区零 gid=8', 'https://bbs-api-os.hoyolab.com/community/painter/wapi/circle/channel/guide/material?game_id=8'],
];

const CODE_RE = /[A-Z0-9]{8,16}/g;

for (const [label, url] of TARGETS) {
  try {
    const r = await fetch(url, { headers: H });
    const t = await r.text();
    let j;
    try { j = JSON.parse(t); } catch { console.log(`${label}: HTTP ${r.status} 非JSON ${t.slice(0, 80)}`); await sleep(1200); continue; }
    console.log(`\n===== ${label} =====`);
    console.log(`HTTP ${r.status} retcode=${j.retcode} ${j.message ?? ''} dataKeys=${JSON.stringify(Object.keys(j.data || {}))}`);
    const raw = JSON.stringify(j.data || {});
    console.log(`返回体大小=${raw.length} 字符`);
    // 粗扫候选码
    const cands = [...new Set(raw.match(CODE_RE) || [])].filter((c) => /[A-Z]/.test(c));
    console.log(`疑似码 ${cands.length} 个:`, cands.slice(0, 25).join(', '));
    // 看看有哪些标题
    const titles = [...raw.matchAll(/"title":"([^"]{2,60})"/g)].map((m) => m[1]);
    if (titles.length) console.log('标题样例:', [...new Set(titles)].slice(0, 10).join(' | '));
  } catch (e) {
    console.log(`${label}: ❌ ${e.message}`);
  }
  await sleep(1300);
}
