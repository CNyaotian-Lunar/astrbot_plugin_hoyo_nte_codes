// 拉真实的兑换码帖，看玩家/攻略作者到底怎么写「过期时间」，并测正则命中率
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36';
const H = { 'User-Agent': UA, Referer: 'https://www.miyoushe.com/', Accept: 'application/json' };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
import crypto from 'node:crypto';

function ds(q) {
  const salt = 'xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs';
  const t = Math.floor(Date.now() / 1000);
  const r = Math.floor(Math.random() * 100000 + 100000);
  return `${t},${r},${crypto.createHash('md5').update(`salt=${salt}&t=${t}&r=${r}&b=&q=${q}`).digest('hex')}`;
}

function deltaToText(sc) {
  try {
    const arr = JSON.parse(sc);
    if (!Array.isArray(arr)) return String(sc);
    return arr.map((b) => (typeof b?.insert === 'string' ? b.insert : '')).join('');
  } catch {
    return String(sc || '').replace(/<[^>]+>/g, ' ');
  }
}

const KEYWORDS = ['原神兑换码', '绝区零兑换码', '崩铁兑换码'];
const TIME_HINT = /失效|过期|有效期|截止|24点|24:00|23:59|次日|兑换时间|兑换截止/;
const lines = new Set();

for (const kw of KEYWORDS) {
  const url = `https://bbs-api.mihoyo.com/post/wapi/searchPosts?keyword=${encodeURIComponent(kw)}&size=20&offset=0`;
  const r = await fetch(url, { headers: { ...H, DS: ds(kw), 'x-rpc-app_version': '2.71.1', 'x-rpc-client_type': '4', 'x-rpc-language': 'zh-cn' } });
  const j = await r.json();
  const posts = j?.data?.posts || j?.data?.list || [];
  console.log(`搜「${kw}」→ ${posts.length} 条`);
  for (const item of posts.slice(0, 8)) {
    const p = item.post || item;
    const text = deltaToText(p.structured_content);
    const ts = p.created_at ? new Date(p.created_at * 1000).toISOString().slice(0, 10) : '';
    let found = 0;
    for (const raw of text.split('\n')) {
      const t = raw.replace(/_\\([^)]{0,40}\\)/g, '').trim();
      if (!t || t.length > 70) continue;
      if (TIME_HINT.test(t)) { lines.add(t); found++; }
    }
    if (found) console.log(`   [${ts}] ${String(p.subject || '').slice(0, 34)} → ${found} 行时间信息`);
  }
  await sleep(1300);
}

console.log(`\n===== 语料里出现的「过期时间」写法（${lines.size} 条）=====`);
for (const l of lines) console.log('  ·', l);

console.log('\n===== 正则命中率测试 =====');
const RE = /(?:(?:有效期|截止|兑换时间|失效时间)[^\d]{0,6})?(20\d{2})?[-/年]?(\d{1,2})[-/月](\d{1,2})日?(?:\s*(\d{1,2}[:：]\d{2}(?::\d{2})?))?/;
let hit = 0;
for (const l of lines) {
  const m = l.match(RE);
  if (m) { hit++; console.log(`  ✅ ${m[0].trim()}  ← ${l.slice(0, 44)}`); }
  else console.log(`  ❌ 提不出 ← ${l.slice(0, 44)}`);
}
console.log(`\n命中 ${hit} / ${lines.size}`);
