// 前端高亮函数的回归测试。
// 实现从 cacx/web/static/index.html 里正则抽取，不手抄 ——
// 手抄的副本会在前端改掉后继续「通过」，变成永远抓不到 bug 的假测试。
//
// 用法：node tests_mark.js
// 退出码非 0 表示有回归。
const fs = require('fs');
const path = require('path');

const html = path.join(__dirname, 'cacx', 'web', 'static', 'index.html');
const src = fs.readFileSync(html, 'utf8');

const escFn = src.match(/function esc\(s\)\{[\s\S]*?\n\}/);
const markFn = src.match(/function mark\(text, terms\)\{[\s\S]*?\n\}/);
if (!escFn || !markFn) {
  console.error('FAIL: 没能从 index.html 抽出 esc()/mark()，前端结构变了？');
  process.exit(1);
}
eval(escFn[0]);
eval(markFn[0]);

const cases = [
  ['开展清朗专项行动', ['清朗'], '<mark>清朗</mark>', '基础中文高亮'],
  ['清朗行动与清朗网络', ['清朗'],
   '<mark>清朗</mark>行动与<mark>清朗</mark>网络', '多处命中'],
  ['<script>alert(1)</script> 清朗', ['清朗'], '&lt;script&gt;', '尖括号转义'],
  ['清朗 <img src=x>', ['清朗'], '&lt;img', '标签不被还原'],
  ['a.b 清朗', ['a.b'], '<mark>a.b</mark>', '正则元字符转义'],
  ['清朗行动', ['清朗', '清朗行动'], '<mark>清朗行动</mark>', '长词优先不切碎'],
  ['清朗行动', ['清朗行动', '清朗'], '<mark>清朗行动</mark>', '长短词顺序颠倒'],
  ['A|B 清朗', ['A|B'], '<mark>A|B</mark>', '竖线元字符'],
  ['(测试) 清朗', ['(测试)'], '<mark>(测试)</mark>', '括号元字符'],
  ['没有命中词', ['不存在'], '没有命中词', '未命中原样返回'],
  ['清朗', [], '清朗', '空词表不炸'],
  ['清朗清朗清朗', ['清朗'],
   '<mark>清朗</mark><mark>清朗</mark><mark>清朗</mark>', '连续三处'],
  ['abc清朗abc', ['清朗', 'abc'],
   '<mark>abc</mark><mark>清朗</mark><mark>abc</mark>', '相邻多词不丢'],
];

let pass = 0, fail = 0;
for (const [input, terms, expect, name] of cases) {
  const got = mark(input, terms);
  const ok = got.includes(expect);
  ok ? pass++ : fail++;
  console.log(`  ${ok ? 'PASS' : 'FAIL'}  ${name.padEnd(22)} -> ${got}`);
}

for (const [input, terms] of cases) {
  const got = mark(input, terms);
  // 嵌套检测不能用 /<mark>.*<mark>/ —— 相邻两个 mark 也会命中，那是正常的。
  // 正解：剥掉所有完整的 <mark>..</mark> 对，剩下的里不该再有标签。
  const stripped = got.replace(/<mark>[\s\S]*?<\/mark>/g, '');
  if (stripped.includes('<mark>')) {
    fail++; console.log('  FAIL  嵌套高亮:', got);
  }
  if (/<[a-zA-Z/]/.test(stripped)) {
    fail++; console.log('  FAIL  存在未转义标签:', got);
  }
  const open = (got.match(/<mark>/g) || []).length;
  const close = (got.match(/<\/mark>/g) || []).length;
  if (open !== close) {
    fail++; console.log(`  FAIL  标签不配平 open=${open} close=${close}:`, got);
  }
}

console.log(`\n结果: ${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);