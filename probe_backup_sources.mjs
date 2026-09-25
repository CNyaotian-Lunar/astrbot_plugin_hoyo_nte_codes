// 找「米游社搜索」之外的备用数据源
// 1) 米游社其它端点：盯作者 userPost / 版块列表 / 官方 miyolive 直播活动
// 2) 米哈游官方「游戏公告」接口（完全不同的域名与系统）
const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36';
const H = { 'User-Agent': UA, Referer: 'https://www.miyoushe.com/', Accept: 'application/json' };
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

async function j(url, headers = {}) {
  const r = await fetch(url, { headers: { ...H, ...headers } });
  const t = await r.text();
  try { return { http: r.status, json: JSON.parse(t) }; }
  catch { return { http: r.status, raw: t.slice(0, 120) }; }
}

console.log('===== 备用 1：米游社 userPost（盯专发兑换码的攻略作者）=====');
for (const uid of ['280612327', '191438027']) {
  const d = await j(`https://bbs-api.mihoyo.com/post/wapi/userPost?uid=${uid}&size=20&offset=0`);
  const list = d.json?.data?.list || [];
  console.log(`uid=${uid} http=${d.http} retcode=${d.json?.retcode} 帖子数=${list.length}`);
  for (const item of list.slice(0, 6)) {
    const p = item.post?.post ?? item.post ?? {};
    const ts = p.created_at ? new Date(p.created_at * 1000).toISOString().slice(0, 10) : '';
    console.log(`   [${ts}] ${String(p.subject || '').slice(0, 40)} | 带正文=${!!p.structured_content}`);
  }
  await sleep(1300);
}

console.log('\n===== 备用 2：米游社 miyolive 官方直播活动（原神）=====');
{
  const d = await j('https://bbs-api.mihoyo.com/painter/api/user_instant/list?offset=0&size=20&uid=75276539');
  let actId = '';
  for (const item of d.json?.data?.list || []) {
    const post = item?.post?.post ?? {};
    const m = String(post.structured_content || '').match(/act_id=([^&"\\]+)/);
    if (m) { actId = m[1]; console.log(`   找到 act_id=${actId}（帖：${String(post.subject || '').slice(0, 30)}）`); break; }
  }
  if (!actId) console.log('   ❌ 没找到 act_id');
  else {
    const idx = await j('https://api-takumi.mihoyo.com/event/miyolive/index', { 'x-rpc-act_id': actId });
    console.log(`   miyolive/index retcode=${idx.json?.retcode} title=${idx.json?.data?.live?.title ?? ''}`);
    const ver = idx.json?.data?.live?.code_ver;
    if (ver) {
      const rc = await j(`https://api-takumi-static.mihoyo.com/event/miyolive/refreshCode?version=${ver}&time=${Math.floor(Date.now() / 1000)}`, { 'x-rpc-act_id': actId });
      const codes = rc.json?.data?.code_list || [];
      console.log(`   refreshCode retcode=${rc.json?.retcode} 码数=${codes.length}`);
      for (const c of codes) console.log(`     ${c.code}`);
    }
  }
}

console.log('\n===== 备用 3：米哈游官方「游戏公告」接口（独立域名/系统）=====');
const ANNS = {
  原神: 'https://hk4e-api.mihoyo.com/common/hk4e_cn/announcement/api/getAnnList?game=hk4e&game_biz=hk4e_cn&lang=zh-cn&bundle_id=hk4e_cn&platform=pc&region=cn_gf01&level=60&uid=100000000',
  星铁: 'https://hkrpg-api.mihoyo.com/common/hkrpg_cn/announcement/api/getAnnList?game=hkrpg&game_biz=hkrpg_cn&lang=zh-cn&bundle_id=hkrpg_cn&platform=pc&region=prod_gf_cn&level=70&uid=100000000',
  绝区零: 'https://nap-api.mihoyo.com/common/nap_cn/announcement/api/getAnnList?game=nap&game_biz=nap_cn&lang=zh-cn&bundle_id=nap_cn&platform=pc&region=prod_gf_cn&level=60&uid=100000000',
};
for (const [name, url] of Object.entries(ANNS)) {
  const d = await j(url);
  const rc = d.json?.retcode;
  const total = d.json?.data?.total ?? 0;
  console.log(`${name}: http=${d.http} retcode=${rc} ${d.json?.message ?? d.raw ?? ''} 公告数=${total}`);
  for (const l of d.json?.data?.list || []) {
    for (const a of l.list || []) {
      console.log(`   - ${String(a.title || '').slice(0, 40)}`);
    }
  }
  await sleep(1300);
}
