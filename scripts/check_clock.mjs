/* =============================================================================
   页头时钟自检

   跑法：
       node scripts/check_clock.mjs

   ============================ 为什么这个测试存在 ============================
   页头上那个 `GPS 2437.0` 看起来是个装饰，其实不是 ——
   **整个程序内部就是用 GPS 周说话的**：

       catalog.BKG_PRODUCTS_FROM_WEEK       = 2238
       catalog.CDDIS_OLD_NAMING_BEFORE_WEEK = 2237
       sources/ginan 里那一堆按周分目录的 URL

   用户会在取数计划、缓存目录名和报错里反复看到"周 2238"这种东西。
   页头那个数字就是给他一个参照 —— **所以它必须是对的**。

   ⚠️ 而它是**另一套实现**：Python 那边是 `date` 相减（catalog.py:gps_week），
      JS 这边是毫秒相减（clock.js）。两套各写各的，就可能各错各的。

   👉 守卫的办法是把**两边钉在同一组契约值上**：
      本文件测 JS，check_ui_text.py 的 ⑬ 用 catalog.gps_week() 测 Python，
      两边断言的是**同样的几个日期**。任何一个漂了，那一半就会红。

      这不是重复劳动 —— 重复的是**契约值**，不是实现。
      而"页头的周号和程序内部用的周号必须是同一个"这条约定，
      本来就只有靠两个独立实现互相对照才守得住。
   ============================================================================= */

import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

const here = dirname(fileURLToPath(import.meta.url));
const src = readFileSync(
  join(here, '..', 'ginan_agent', 'frontend', 'clock.js'), 'utf-8'
);

// clock.js 是挂在 window 上的 IIFE，这里造一个假的 window 来加载它
const win = {};
new Function('window', src)(win);
const { gpsWeek, gpsDayOfWeek, formatUtc, formatDate } = win;

let PASS = 0, FAIL = 0;
function check(cond, label) {
  if (cond) { PASS++; console.log(`  ✓ ${label}`); }
  else { FAIL++; console.log(`  ✗ ${label}   ← 失败`); }
  return cond;
}
function section(t) { console.log(`\n${t}\n${'-'.repeat(60)}`); }

// =============================================================================
// ① 契约值 —— 和 check_ui_text.py ⑬ 断言的是同一组日期
// =============================================================================

section('① ★ 契约值：这几个日期的 GPS 周号是**定死**的');

function weekDow(iso) {
  const d = new Date(iso);
  return [gpsWeek(d), gpsDayOfWeek(d)];
}

// 每一项都是独立可查的，不是"跑一遍记下来"的那种：
//
//   1980-01-06  纪元本身。GPS 周 0 从这一天开始。
//   2022-11-27  ← 这个最重要：它是 **IGS 改长命名的那一周**，
//                本项目自己的 catalog.py 里写着
//                `BKG_PRODUCTS_FROM_WEEK = 2238`。
//                页头的周号和程序内部用的周号对不上的话，就是这里红。
//   2026-09-20  写这个测试的那天。1980-01-06 起第 17059 天 = 2437×7，
//                所以它正好是**周日 0 点整**——一个周界，最容易验算。
const CONTRACT = [
  ['1980-01-06T00:00:00Z', 0, 0, '纪元本身'],
  ['2022-11-27T00:00:00Z', 2238, 0, 'IGS 改长命名那一周（= catalog.BKG_PRODUCTS_FROM_WEEK）'],
  ['2026-09-20T00:00:00Z', 2437, 0, '写测试那天，恰好是周界'],
];

for (const [iso, w, dow, why] of CONTRACT) {
  const [gw, gd] = weekDow(iso);
  check(gw === w && gd === dow, `${iso} → ${w}.${dow}   （${why}）`);
}

// =============================================================================
// ② 一天之内必须是同一格
// =============================================================================

section('② ★ 一天之内任何时候都算同一个 GPS 日');

// ⚠️ 这条看着废话，其实是这个函数最容易错的地方：
//    用"四舍五入"而不是"向下取整"的话，一天里的后半段就会跳进下一格，
//    表现为**中午之后页头的周号自己变了**。
const dayStart = weekDow('2026-09-20T00:00:00Z');
const dayEnd   = weekDow('2026-09-20T23:59:59Z');
check(
  dayStart[0] === dayEnd[0] && dayStart[1] === dayEnd[1],
  `00:00:00 和 23:59:59 落在同一格（${dayStart.join('.')} vs ${dayEnd.join('.')}）`
);

const nextDay = weekDow('2026-09-21T00:00:00Z');
check(
  nextDay[0] === dayStart[0] && nextDay[1] === dayStart[1] + 1,
  `跨过午夜的下一格是 ${nextDay.join('.')}（周号不变，星期几 +1）`
);

// =============================================================================
// ③ 性质测试：+7 天 = 周号 -1，星期几绕一圈
// =============================================================================

section('③ 性质：往前推 7 天，周号正好少 1');

const WEEK_MS = 7 * 86400000;
let propBad = 0;
let firstBad = '';
// 从 2020 到 2030，每 37 天取一个点（37 与 7 互质，能踩遍所有星期几）
for (let t = Date.UTC(2020, 0, 1); t < Date.UTC(2030, 0, 1); t += 37 * 86400000) {
  const a = new Date(t);
  const b = new Date(t + WEEK_MS);
  if (gpsWeek(b) !== gpsWeek(a) + 1 || gpsDayOfWeek(a) !== gpsDayOfWeek(b)) {
    propBad++;
    if (!firstBad) firstBad = `${a.toISOString()} vs ${b.toISOString()}`;
  }
}
check(propBad === 0, `约 100 个取样点全部满足${firstBad ? `（第一个反例：${firstBad}）` : ''}`);

// 星期几只可能是 0..6
let rangeBad = 0;
for (let t = Date.UTC(2020, 0, 1); t < Date.UTC(2024, 0, 1); t += 86400000) {
  const d = gpsDayOfWeek(new Date(t));
  if (!Number.isInteger(d) || d < 0 || d > 6) rangeBad++;
}
check(rangeBad === 0, '星期几只落在 0..6');

// =============================================================================
// ④ 格式化
// =============================================================================

section('④ 格式化：补零，而且是 **UTC**');

check(
  formatUtc(new Date('2026-09-20T04:05:07Z')) === '04:05:07',
  'formatUtc 补零：04:05:07'
);
check(
  formatDate(new Date('2026-09-20T04:05:07Z')) === '2026-09-20',
  'formatDate：2026-09-20'
);

// ⚠️⚠️ 这条是本文件里最要紧的一条。
//    GPS 周是 **UTC** 概念。用本地时间算的话，东八区用户晚上 8 点以后
//    页头的周号会比实际早一天 —— 而那个偏差**只在晚上出现**，
//    白天测永远测不出来。
//
//    构造一个"本地时间已经跨天、UTC 还没跨"的时刻来验：
//    18:00 UTC 在东八区已经是次日 02:00。
const edge = new Date('2026-09-20T18:00:00Z');
check(
  formatDate(edge) === '2026-09-20',
  '★ 18:00Z 在 UTC 下仍是 20 号（用本地时间算的话，东八区会显示 21 号）'
);
check(
  gpsDayOfWeek(edge) === 0,
  '★ 而且它的 GPS 星期几还是 0（本地算会变成 1 —— 周号也跟着错一格）'
);

const tz = -(new Date('2026-09-20T18:00:00Z')).getTimezoneOffset();
if (tz > 0) {
  console.log(`      （本机在 UTC+${tz / 60}，上面的用例是有效的）`);
} else {
  console.log(`      （本机在 UTC${tz / 60}，这条用例在本机区分不出本地/UTC —— 仍然保留）`);
}

// =============================================================================
// ⑤ document 上没有的东西不该被碰
// =============================================================================

section('⑤ clock.js 只碰自己该碰的');

check(
  !/innerHTML/.test(src),
  '不用 innerHTML（这个文件只写文本）'
);
check(
  /data-clock/.test(src),
  '按 data-clock 找要填的节点（缺哪个就跳过哪个，不报错）'
);

// =============================================================================

console.log();
console.log('='.repeat(60));
const total = PASS + FAIL;
if (FAIL === 0) {
  console.log(`全部通过 (${PASS}/${total})`);
} else {
  console.log(`有失败：${FAIL} 项未通过（通过 ${PASS}/${total}）`);
}
console.log('='.repeat(60));
process.exit(FAIL === 0 ? 0 : 1);
