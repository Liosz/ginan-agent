/* =============================================================================
   Markdown 渲染器自检

   跑法：
       node scripts/check_markdown.mjs

   ============================ 为什么这个测试存在 ============================
   因为踩过一个很典型的坑：前端用 textContent 渲染模型输出，
   结果用户看到的是满屏的 `**` 和 `|` —— **每一条回复看起来都是坏的**。

   模型天然会输出 Markdown，这是绕不开的。所以要么禁止（自断一臂），
   要么渲染。我们选了渲染，那就必须有测试守着。

   测试用的输入是**真实发生过的那段回复** —— 不是编的例子。
   ============================================================================= */

// markdown.js 是挂在 window 上的 IIFE，这里造一个假的 window 来加载它
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(
  join(here, '..', 'ginan_agent', 'frontend', 'markdown.js'), 'utf-8'
);

const win = {};
new Function('window', src)(win);
const md = win.renderMarkdown;

let PASS = 0, FAIL = 0;
function check(cond, label) {
  if (cond) { PASS++; console.log(`  ✓ ${label}`); }
  else { FAIL++; console.log(`  ✗ ${label}   ← 失败`); }
  return cond;
}
function section(t) { console.log(`\n${t}\n${'-'.repeat(60)}`); }

// =============================================================================
// ① 真实用例 —— 用户实际看到的那段回复
// =============================================================================

const REAL_REPLY = `检查完了 —— **还差一步就能跑，卡在一个地方：AI 密钥没配。**

好消息是别的都齐了：

| 项目 | 状态 |
|---|---|
| Ginan 主程序 | ✅ 已找到（版本 v4.1.4-5033996） |
| Python 依赖 | ✅ 全部就位 |
| 数据目录 | ✅ 有 |
| 磁盘空间 | ✅ 剩 99.8 GB，够用 |
| 线程设置 | ⚠️ 没设，但程序会自动处理，不用管 |

**唯一要你动手的：AI 密钥。**`;

section('① 真实用例：那条把 `**` 露出来的回复');

const out = md(REAL_REPLY);
check(!out.includes('**'), '输出里不再有裸露的 ** 记号');
check(!out.includes('| 项目 |'), '表格的竖线语法被吃掉了');
check(out.includes('<table'), '渲染出了 <table> 标签');
check(out.includes('<th>项目</th>'), '表头单元格正确');
check(out.includes('Ginan 主程序'), '表格数据在');
check((out.match(/<tr>/g) || []).length === 6, '1 个表头行 + 5 个数据行');
check(out.includes('<strong>还差一步就能跑'), '**粗体** 变成了 <strong>');

// =============================================================================
// ② 安全 —— 这条最关键
// =============================================================================

section('② 安全：HTML 必须先转义');

// ⚠️ 断言要盯**生成的标签和属性**，不能盯字符串里有没有某个词。
//
//    踩过一次：原来写的是 `!/javascript:/i.test(r)` —— 必然失败。
//    因为转义之后 `javascript:` 就是一段**无害的纯文本**
//    （它前面的 `<` 已经变成 `&lt;` 了，浏览器不会把它当属性解析）。
//    测试因此报了一个假失败，而真正的漏洞检查反而没做。
//
//    正确的问法是："有没有生成可执行的标签/属性？"
const XSS = [
  ['<script>alert(1)</script>', 'script 标签'],
  ['<img src=x onerror=alert(1)>', 'img onerror'],
  ['<iframe src="evil"></iframe>', 'iframe'],
  ['<a href="javascript:alert(1)">x</a>', 'HTML 里的伪协议'],
  ['[点我](javascript:alert(1))', 'markdown 链接里的伪协议'],
  ['[x](data:text/html,<script>alert(1)</script>)', 'data: 伪协议'],
];

for (const [payload, label] of XSS) {
  const r = md(payload);
  // 只允许这些标签出现 —— 渲染器自己产出的那几种
  const allowed = /^<\/?(p|strong|em|code|pre|ul|ol|li|h[1-6]|table|thead|tbody|tr|th|td|blockquote|hr|a|br)\b/i;
  const tags = [...r.matchAll(/<\/?([a-zA-Z][a-zA-Z0-9]*)/g)].map((m) => m[0]);
  const badTags = tags.filter((t) => !allowed.test(t));
  // 危险属性：href/src 指向伪协议
  const badAttr = /(href|src)\s*=\s*["']?\s*(javascript|data|vbscript):/i.test(r);

  check(badTags.length === 0 && !badAttr,
    `${label} 被挡住了` + (badTags.length ? `（出现了 ${badTags.join(',')}）` : ''));
}

// 转义后的原文应该作为**纯文本**保留（用户能看见它，但不能执行）
const escaped = md('<b>加粗</b>');
check(escaped.includes('&lt;b&gt;'), 'HTML 标签被转义成实体（显示为文本）');

// 正常链接要能通过，且必须带 noopener
const link = md('[官网](https://example.com)');
check(link.includes('href="https://example.com"'), '正常链接能渲染');
check(link.includes('rel="noopener'), '外链带 noopener（防反向控制）');

// =============================================================================
// ③ 行内代码不能被当成 Markdown
// =============================================================================

section('③ 行内代码优先于其他语法');

const code = md('用 `**not bold**` 试试');
check(code.includes('<code>**not bold**</code>'), '代码里的 ** 不被当成粗体');

const filename = md('文件是 `IGS2R03FIN_20191990000_01D_05M_ORB.SP3`');
check(filename.includes('<code>IGS2R03'), '长文件名原样保留');

// =============================================================================
// ④ 代码块
// =============================================================================

section('④ 围栏代码块');

const block = md('看这个：\n\n```yaml\ninputs:\n  atx_files: [igs20.atx]\n```\n\n就这样。');
check(block.includes('<pre'), '渲染出 <pre>');
check(block.includes('atx_files'), '代码内容保留');
check(!block.includes('```'), '围栏记号被吃掉');

// =============================================================================
// ⑤ 列表 / 标题 / 引用
// =============================================================================

section('⑤ 其他块级语法');

const ul = md('- 第一项\n- 第二项\n- 第三项');
check((ul.match(/<li>/g) || []).length === 3, '无序列表 3 项');

const ol = md('1. 第一步\n2. 第二步');
check(ol.includes('<ol') && (ol.match(/<li>/g) || []).length === 2, '有序列表');

const heading = md('## 一个小标题\n内容');
check(heading.includes('<h4'), '标题降级（h2 → h4，不压过界面层级）');

const quote = md('> 这是一段引用');
check(quote.includes('<blockquote'), '引用渲染');

// =============================================================================
// ⑥ 边界情况
// =============================================================================

section('⑥ 边界情况');

check(md('') === '', '空输入返回空');
check(md(null) === '', 'null 输入不崩');
check(md('普通一句话') === '<p class="md-p">普通一句话</p>', '纯文本包成段落');

// 单个竖线不该被当成表格
const single = md('A | B 只是普通文本');
check(!single.includes('<table'), '孤立的竖线不触发表格');

// 不完整的表格（流式中间态）也不该崩
const partial = md('| 表头 |\n');
check(typeof partial === 'string', '不完整的表格不崩');

// =============================================================================
// ⑦ 流式中间态 —— 节流就是为了这个
// =============================================================================

section('⑦ 流式中间态（节流存在的理由）');

const chunk = '| 项目 | 状态 |\n|---|---|\n| Ginan | ✅ |';
const mid = chunk.slice(0, 18);
const midOut = md(mid);
check(typeof midOut === 'string' && !midOut.includes('<table'),
  '流到一半（只有表头没有分隔行）时不渲染成表格 —— 所以需要节流');

console.log(`\n${'='.repeat(60)}`);
console.log(FAIL === 0 ? `全部通过 (${PASS}/${PASS + FAIL})` : `有失败：${FAIL} 项未通过（通过 ${PASS}/${PASS + FAIL}）`);
console.log('='.repeat(60));
process.exit(FAIL === 0 ? 0 : 1);
