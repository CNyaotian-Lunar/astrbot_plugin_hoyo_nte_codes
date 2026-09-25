// 能不能自动识别兑换码的过期时间？
// A. 官方 miyolive 接口的完整字段里有没有 expire / end_time
// B. 本地语料（数据源抓取时存下的原始 JSON）里，玩家帖怎么写过期时间
// 用法：PROBE_DIR=<含原始 JSON 的目录> node probe_expiry.mjs（不设 PROBE_DIR 则跳过 B 组）
import fs from 'node:fs';
import path from 'node:path';

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36';
const H = { 'User-Agent': UA, Referer: 'https://www.miyoushe.com/', Accept: 'application/json' };

console.log('===== A. 官方 miyolive 的完整返回结构 =====');
const actId = 'ea202609041755176692';
{
  const r = await fetch('https://api-takumi.mihoyo.com/event/miyolive/index', { headers: { ...H, 'x-rpc-act_id': actId } });
  const j = await r.json();
  console.log('--- index 全文 ---');
  console.log(JSON.stringify(j, null, 1).slice(0, 2500));
  const ver = j?.data?.live?.code_ver;
  if (ver) {
    const r2 = await fetch(`https://api-takumi-static.mihoyo.com/event/miyolive/refreshCode?version=${ver}&time=${Math.floor(Date.now() / 1000)}`, { headers: { ...H, 'x-rpc-act_id': actId } });
    const j2 = await r2.json();
    console.log('\n--- refreshCode 全文 ---');
    console.log(JSON.stringify(j2, null, 1).slice(0, 2500));
  }
}

console.log('\n\n===== B. 本地语料里玩家怎么写「过期时间」 =====');
const HINT = /失效|过期|有效期|截止|24点|24:00|23:59|次日|兑换时间/;
// 本地语料目录（原始 JSON 体积大、不入库）：由环境变量 `PROBE_DIR` 指定。
// 默认留空 ⇒ 跳过 B 组，而不是去猜某个本机路径。
const PROBE_DIR = process.env.PROBE_DIR || '';
const files = [];
if (PROBE_DIR && fs.existsSync(PROBE_DIR)) {
  (function walk(dir, depth = 0) {
    if (depth > 4) return;
    for (const e of fs.readdirSync(dir, { withFileTypes: true })) {
      const p = path.join(dir, e.name);
      if (e.isDirectory()) walk(p, depth + 1);
      else if (e.name.endsWith('.json')) files.push(p);
    }
  })(PROBE_DIR);
} else {
  console.log('  ⚠️ 未提供本地语料目录（设 PROBE_DIR=<目录> 可跑 B 组），跳过');
}
console.log(`扫描 ${files.length} 个 json 文件`);

const hits = new Set();
let scanned = 0;
for (const f of files) {
  let txt;
  try { txt = fs.readFileSync(f, 'utf8'); } catch { continue; }
  // 从 JSON 里粗暴抓所有 insert 文本
  for (const m of txt.matchAll(/"insert":"((?:[^"\\]|\\.)*)"/g)) {
    const s = m[1].replace(/\\n/g, '\n').replace(/\\"/g, '"');
    for (const line of s.split('\n')) {
      const t = line.trim();
      if (!t || t.length > 60) continue;
      if (HINT.test(t)) hits.add(t);
    }
  }
  scanned++;
}
console.log(`解析 ${scanned} 个文件，命中 ${hits.size} 条含时间/失效语义的行：\n`);
for (const h of [...hits].slice(0, 40)) console.log('  ·', h);

console.log('\n===== C. 现有正则能提出多少（粗测）=====');
const PATTERNS = {
  'YYYY-MM-DD HH:MM': /(\d{4})[-/年](\d{1,2})[-/月](\d{1,2})日?\s*(\d{1,2}:\d{2})?/,
  'M月D日': /(\d{1,2})月(\d{1,2})日/,
  '仅HH:MM': /(\d{1,2}:\d{2}(?::\d{2})?)/,
};
let withTime = 0;
for (const h of hits) {
  const which = Object.entries(PATTERNS).filter(([, re]) => re.test(h)).map(([k]) => k);
  if (which.length) withTime++;
}
console.log(`含具体时间格式的行：${withTime} / ${hits.size}`);
