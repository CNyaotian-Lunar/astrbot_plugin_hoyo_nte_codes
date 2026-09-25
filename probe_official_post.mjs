// 最后的验证：星铁/绝区零官方号发的「前瞻特别节目」帖，正文里到底有没有活动 id / 链接
import crypto from 'node:crypto';

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36';
const H = { 'User-Agent': UA, Referer: 'https://www.miyoushe.com/', Accept: 'application/json' };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

function ds(query) {
  const salt = 'xV8v4Qu54lUKrEYFZkJhB8cuOh9Asafs';
  const t = Math.floor(Date.now() / 1000);
  const r = Math.floor(Math.random() * 100000 + 100000);
  const md5 = crypto.createHash('md5').update(`salt=${salt}&t=${t}&r=${r}&b=&q=${query}`).digest('hex');
  return `${t},${r},${md5}`;
}

const OFFICIAL = { 75276539: '原神官方', 288909600: '星铁官方', 152039148: '绝区零官方' };
const KEYWORDS = ['前瞻特别节目', '版本前瞻', '前瞻直播'];

async function search(kw) {
  const url = `https://bbs-api.mihoyo.com/post/wapi/searchPosts?keyword=${encodeURIComponent(kw)}&size=20&offset=0`;
  const r = await fetch(url, {
    headers: {
      ...H,
      DS: ds(kw),
      'x-rpc-app_version': '2.71.1',
      'x-rpc-client_type': '4',
      'x-rpc-language': 'zh-cn',
    },
  });
  const j = await r.json();
  return j?.data?.posts || j?.data?.list || [];
}

const seen = new Set();
for (const kw of KEYWORDS) {
  const posts = await search(kw);
  console.log(`\n===== 搜「${kw}」→ ${posts.length} 条 =====`);
  for (const item of posts) {
    const p = item.post || item;
    const uid = String(p.uid || '');
    const tag = OFFICIAL[uid];
    if (!tag) continue;
    if (seen.has(p.post_id)) continue;
    seen.add(p.post_id);
    const sc = String(p.structured_content || '');
    const urls = [...new Set([...sc.matchAll(/https?:\/\/[^"\\\s]+/g)].map((m) => m[0]))];
    const actIds = [...new Set([...sc.matchAll(/act_id=([^&"\\]+)/g)].map((m) => m[1]))];
    const ts = p.created_at ? new Date(p.created_at * 1000).toISOString().slice(0, 10) : '';
    console.log(`  [${tag}] [${ts}] ${String(p.subject || '').slice(0, 40)} (id=${p.post_id})`);
    console.log(`     act_id: ${actIds.length ? actIds.join(', ') : '（无）'}`);
    const nonImg = urls.filter((u) => !/upload-bbs|prod-vod|hdslb|bbs-static/.test(u));
    if (nonImg.length) for (const u of nonImg.slice(0, 4)) console.log(`     链接: ${u.slice(0, 130)}`);
    else console.log(`     链接: （只有图片/视频）`);
  }
  await sleep(1300);
}
console.log('\n（完）');
