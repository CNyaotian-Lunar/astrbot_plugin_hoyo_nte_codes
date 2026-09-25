// 测「微博」这条完全独立的备用线（前瞻当晚官微/玩家都会发国服码）
const UA = 'Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1';
const H = { 'User-Agent': UA, Referer: 'https://m.weibo.cn/' };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function j(url) {
  const r = await fetch(url, { headers: H });
  const t = await r.text();
  try { return { http: r.status, json: JSON.parse(t) }; } catch { return { http: r.status, raw: t.slice(0, 120) }; }
}

console.log('===== 微博关键词搜索（不需要登录/uid）=====');
for (const kw of ['原神兑换码', '绝区零前瞻兑换码']) {
  const url = `https://m.weibo.cn/api/container/getIndex?containerid=${encodeURIComponent(`100103type=1&q=${kw}`)}&page_type=searchall`;
  const d = await j(url);
  const cards = d.json?.data?.cards || [];
  console.log(`\n关键词「${kw}」 http=${d.http} ok=${d.json?.ok} cards=${cards.length} ${d.raw ?? ''}`);
  let shown = 0;
  for (const c of cards) {
    for (const g of c.card_group || []) {
      const text = String(g.mblog?.text || '').replace(/<[^>]+>/g, '');
      if (!text) continue;
      console.log(`   - ${text.replace(/\s+/g, ' ').slice(0, 90)}`);
      shown++;
      if (shown >= 5) break;
    }
    if (shown >= 5) break;
  }
  await sleep(1500);
}

console.log('\n===== 官微时间线（原神，uid=1953109485）=====');
{
  const uid = '1953109485';
  const d = await j(`https://m.weibo.cn/api/container/getIndex?type=uid&value=${uid}&containerid=107603${uid}`);
  const cards = d.json?.data?.cards || [];
  console.log(`http=${d.http} ok=${d.json?.ok} cards=${cards.length}`);
  for (const c of cards.slice(0, 5)) {
    const text = String(c.mblog?.text || '').replace(/<[^>]+>/g, '');
    if (text) console.log(`   - ${text.replace(/\s+/g, ' ').slice(0, 90)}`);
  }
}
