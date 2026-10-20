/* =============================================================================
   极简 Markdown 渲染器

   ============================ 为什么必须自己写 ============================
   因为模型**天然会输出 Markdown**，而前端原本用 textContent 渲染，
   结果是用户看到满屏的 `**` 和 `|` —— 每一条回复看起来都是坏的。

   两条路：
     A. 禁止模型用 Markdown
     B. 渲染它

   选 B。因为 Markdown 里有一件事对我们特别有用：**表格**。
   环境检查、数据源状态、文件清单 —— 用表格展示比一堆句子清楚得多。

   ⚠️ 为什么不用 marked / markdown-it？
      · 要引 CDN → 违背"必须能离线"
      · 要打包进 node_modules → 违背"不要构建步骤"
      · 它们的体积和我们要的功能（六种语法）不成比例

   ============================ 两条硬规则 ============================
   ① **先转义 HTML，再做替换。**

      顺序反了就是 XSS 漏洞。模型输出里如果带了
      `<img onerror=...>`，直接写进 innerHTML 就会执行。
      而模型的输出来自用户输入的内容（比如站名），
      所以这条路径**真的可达**，不是理论风险。

   ② **所有块级语法检测都要按"转义后的形态"来写。**

      这是规则 ① 的必然副作用：转义之后 `>` 变成了 `&gt;`、`<` 变成了 `&lt;`。
      如果检测还在找原始的 `>`，那段语法就**永远匹配不上** ——
      而且症状极隐蔽：不报错，只是静默退化成普通段落。
      （引用渲染不出来就是这么来的。）
   ============================================================================= */

(function (global) {
  'use strict';

  // 行内代码的占位符。
  //
  // ⚠️ 用不可见字符是为了保证**绝不和正文冲突**（正文来自模型，
  //    理论上可能包含任何可见字符）。
  //
  // ⚠️⚠️ 但它必须写成**可见的转义序列** `\x01`，不能直接写那个控制字符。
  //    直接写的话它在编辑器里是一片空白，后来的人根本看不出来那里有东西，
  //    改这个文件时很容易把它删掉 —— 而删掉之后的症状是
  //    "行内代码偶尔渲染错"，极难联想到是这个原因。
  const CODE_MARK = '\x01';

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) => ({
      '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;',
    }[c]));
  }

  // ---- 行内语法 ----
  //
  // ⚠️ 顺序有讲究：行内代码**最先**处理，
  //    否则 `**not bold**` 这种"代码里包含 Markdown 符号"的内容会被误处理。
  //    处理完用占位符换掉，最后再换回来。
  function inline(text, codes) {
    text = text.replace(/`([^`]+)`/g, (_, code) => {
      codes.push(`<code>${code}</code>`);
      return `${CODE_MARK}${codes.length - 1}${CODE_MARK}`;
    });

    // 粗体（** 或 __）—— 放在斜体之前，否则 ** 会被当斜体吃掉
    text = text.replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
    text = text.replace(/__([^_]+)__/g, '<strong>$1</strong>');

    // 斜体
    text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, '$1<em>$2</em>');

    // 链接 [文字](地址)
    // ⚠️ 只认 https?:// —— `javascript:` 之类的伪协议根本不会变成链接。
    // ⚠️ rel="noopener" 是必须的：target="_blank" 但没 rel 的话，
    //    新页面能通过 window.opener 反向控制我们的页面。
    text = text.replace(
      /\[([^\]]+)\]\((https?:\/\/[^)\s]+)\)/g,
      '<a href="$2" target="_blank" rel="noopener noreferrer">$1</a>'
    );

    // 裸 URL
    text = text.replace(
      /(^|[\s(])(https?:\/\/[^\s<)]+)/g,
      '$1<a href="$2" target="_blank" rel="noopener noreferrer">$2</a>'
    );

    // 还原行内代码
    const re = new RegExp(`${CODE_MARK}(\\d+)${CODE_MARK}`, 'g');
    return text.replace(re, (_, i) => codes[+i]);
  }

  // ---- 表格 ----
  //
  // 表格是这里**最值钱**的部分 —— 状态汇报用它最清楚。
  //
  // 识别方式（CommonMark 的规定）：
  //   表头行   | 项目 | 状态 |
  //   分隔行   |---|---|        ← 每个单元格至少两个 -
  //   数据行   | Ginan | ✅ |
  function isTableSep(line) {
    return /^\s*\|?\s*:?-{2,}:?\s*(\|\s*:?-{2,}:?\s*)*\|?\s*$/.test(line);
  }

  function splitRow(line) {
    let s = line.trim();
    if (s.startsWith('|')) s = s.slice(1);
    if (s.endsWith('|')) s = s.slice(0, -1);
    return s.split('|').map((c) => c.trim());
  }

  // 对齐：分隔行里的 :--- :---: ---: 决定左中右
  function alignments(sep) {
    return splitRow(sep).map((c) => {
      const l = c.startsWith(':'), r = c.endsWith(':');
      if (l && r) return 'center';
      if (r) return 'right';
      return '';
    });
  }

  // =============================================================================
  // 主函数
  // =============================================================================

  function render(src) {
    if (!src) return '';

    // ⚠️ 第一件事就是转义 —— 见文件头的规则 ①
    const text = escapeHtml(src);
    const lines = text.split('\n');

    const out = [];
    const codes = [];
    let i = 0;

    while (i < lines.length) {
      const line = lines[i];

      // ---- 围栏代码块 ----
      //
      // ⚠️ 按**行**处理，而不是先用正则整块抽出来再放占位符。
      //    那样做需要发明一个"绝不会和正文冲突"的占位符，
      //    而占位符一旦用不可见字符就很难维护（踩过）。
      //    按行处理则和表格、列表用同一套机制，不需要任何占位符。
      if (/^\s*```/.test(line)) {
        i++;
        const buf = [];
        while (i < lines.length && !/^\s*```/.test(lines[i])) {
          buf.push(lines[i]);
          i++;
        }
        i++;   // 吃掉收尾的 ```
        out.push(`<pre class="md-code"><code>${buf.join('\n')}</code></pre>`);
        continue;
      }

      // ---- 表格 ----
      // ⚠️ 必须**先**看下一行是不是分隔行，才能确定这一行是表头。
      //    只看到 | 就当成表格的话，普通文本里的竖线会误判。
      if (i + 1 < lines.length && line.includes('|') && isTableSep(lines[i + 1])) {
        const header = splitRow(line);
        const aligns = alignments(lines[i + 1]);
        i += 2;

        let html = '<table class="md-table"><thead><tr>';
        header.forEach((h, k) => {
          const a = aligns[k] ? ` style="text-align:${aligns[k]}"` : '';
          html += `<th${a}>${inline(h, codes)}</th>`;
        });
        html += '</tr></thead><tbody>';

        while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
          const cells = splitRow(lines[i]);
          html += '<tr>';
          // ⚠️ 按**表头**的列数渲染，而不是按数据行的列数 ——
          //    模型偶尔会少写或多写一列，按表头补齐才不会让表格错位。
          for (let k = 0; k < header.length; k++) {
            const a = aligns[k] ? ` style="text-align:${aligns[k]}"` : '';
            html += `<td${a}>${inline(cells[k] || '', codes)}</td>`;
          }
          html += '</tr>';
          i++;
        }
        html += '</tbody></table>';
        out.push(html);
        continue;
      }

      // ---- 标题 ----
      const h = line.match(/^(#{1,6})\s+(.*)$/);
      if (h) {
        // h1→h3：别让模型的一级标题压过界面本身的层级
        const lvl = Math.min(h[1].length + 2, 6);
        out.push(`<h${lvl} class="md-h">${inline(h[2], codes)}</h${lvl}>`);
        i++;
        continue;
      }

      // ---- 分隔线 ----
      if (/^\s*([-*_])\s*\1\s*\1[\s\-*_]*$/.test(line)) {
        out.push('<hr class="md-hr">');
        i++;
        continue;
      }

      // ---- 引用 ----
      // ⚠️ 匹配的是 `&gt;` 而不是 `>` —— 转义已经把 > 换掉了（规则 ②）
      if (/^\s*&gt;\s?/.test(line)) {
        const buf = [];
        while (i < lines.length && /^\s*&gt;\s?/.test(lines[i])) {
          buf.push(lines[i].replace(/^\s*&gt;\s?/, ''));
          i++;
        }
        out.push(
          `<blockquote class="md-quote">${inline(buf.join(' '), codes)}</blockquote>`
        );
        continue;
      }

      // ---- 列表 ----
      const isUl = /^\s*[-*+]\s+/.test(line);
      const isOl = /^\s*\d+[.)]\s+/.test(line);
      if (isUl || isOl) {
        const items = [];
        const re = isOl ? /^\s*\d+[.)]\s+(.*)$/ : /^\s*[-*+]\s+(.*)$/;
        while (i < lines.length) {
          const m = lines[i].match(re);
          if (!m) break;
          items.push(m[1]);
          i++;
        }
        const tag = isOl ? 'ol' : 'ul';
        out.push(
          `<${tag} class="md-list">` +
          items.map((t) => `<li>${inline(t, codes)}</li>`).join('') +
          `</${tag}>`
        );
        continue;
      }

      // ---- 空行 ----
      if (!line.trim()) { i++; continue; }

      // ---- 段落 ----
      // 连续的非空行合成一段 —— Markdown 里换行不等于分段。
      const buf = [];
      while (i < lines.length && lines[i].trim()) {
        // 遇到其他块级语法的开头就停
        // ⚠️ 引用用 &gt;、围栏用 ```（转义不影响它们）
        if (buf.length && (
          /^\s*```/.test(lines[i]) ||
          /^\s*(#{1,6})\s/.test(lines[i]) ||
          /^\s*[-*+]\s/.test(lines[i]) ||
          /^\s*\d+[.)]\s/.test(lines[i]) ||
          /^\s*&gt;\s?/.test(lines[i]) ||
          lines[i].includes('|')
        )) break;
        buf.push(lines[i]);
        i++;
      }
      if (buf.length) {
        out.push(`<p class="md-p">${inline(buf.join(' '), codes)}</p>`);
      }
    }

    return out.join('');
  }

  global.renderMarkdown = render;
})(typeof window !== 'undefined' ? window : globalThis);
