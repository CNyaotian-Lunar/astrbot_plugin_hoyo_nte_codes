// 补测：米游社「版块帖子列表」是否能当搜索的备用源
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36';
const H = { 'User-Agent': UA, Referer: 'https://www.miyoushe.com/', Accept: 'application/json' };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function j(url) {
  const r = await fetch(url, { headers: H });
  const t = await r.text();
  try { return { http: r.status, json: JSON.parse(t) }; } catch { return { http: r.status, raw: t.slice(0, 100) }; }
}

console.log('===== 米游社版块帖子列表（搜索的容灾备份）=====');
for (const fid of [26, 53, 61, 45, 8, 6]) {
  const d = await j(`https://bbs-api.mihoyo.com/post/wapi/getForumPostList?forum_id=${fid}&is_good=true&page_size=5`);
  const list = d.json?.data?.list || [];
  console.log(`forum_id=${fid} http=${d.http} retcode=${d.json?.retcode} 条数=${list.length}`);
  for (const p of list.slice(0, 3)) {
    const post = p.post ?? p;
    console.log(`   - ${String(post.subject || '').slice(0, 40)} | 带正文=${!!post.structured_content}`);
  }
  await sleep(1200);
}

console.log('\n===== 米游社「话题/标签」接口（另一个角度）=====');
{
  const d = await j('https://bbs-api.mihoyo.com/post/wapi/getTopicList?page_size=5');
  console.log(`getTopicList retcode=${d.json?.retcode} ${d.json?.message ?? d.raw ?? ''}`);
}
