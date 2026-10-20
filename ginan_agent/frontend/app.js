/* =============================================================================
   Ginan Agent —— 前端逻辑

   ============================ 为什么不用框架 ============================
   同 styles.css 里的理由：不要构建步骤、要能离线、要能读懂。

   这个文件的全部工作就是把 SSE 事件流渲染成界面。
   没有虚拟 DOM、没有状态管理库 —— 因为**状态本来就很少**：
   一个正在流式的消息、几个工具卡片、一份环境报告。

   ⚠️ 一个刻意的取舍：**不做增量更新**。
      每来一个 text_delta 就重设一次 textContent，浏览器自己会优化。
      为了"性能"去手写 DOM diff，在这个数据量下是纯粹的复杂度负担。

   ============================ 事件语义 ============================
   服务端推送的事件和 core/types.py 里定义的九种一一对应：

     text_delta     模型吐了一小段文字    → 追加到当前气泡
     tool_start     工具开始执行          → 建一个工具卡片
     tool_progress  工具报进度            → 更新卡片的进度条
     tool_end       工具执行完毕          → 卡片标记完成，折叠内容
     tool_denied    工具被拒绝            → 卡片标记为被拒
     turn_end       一轮结束              → 收尾，解锁输入框
     retry          正在重试              → 显示提示条
     compact_*      历史压缩              → 显示提示条
     error          出错了                → 显示红色提示条
   ============================================================================= */

(() => {
  'use strict';

  const TOKEN = document.querySelector('meta[name="ginan-token"]').content;
  const $ = (sel) => document.querySelector(sel);

  const el = {
    messages:  $('#messages'),
    empty:     $('#empty'),
    input:     $('#input'),
    send:      $('#btn-send'),
    stop:      $('#btn-stop'),
    hint:      $('#composer-hint'),
    refresh:   $('#btn-refresh'),
    envList:   $('#env-list'),
    envPill:   $('#env-verdict'),
    netList:   $('#net-list'),
    netPill:   $('#net-verdict'),
    resultPill: $('#result-pill'),
    resultBody: $('#result-body'),
    figView:   $('#fig-view'),
    figStation: $('#fig-station'),
    figHeadline: $('#fig-headline'),
    figTabs:   $('#fig-tabs'),
    figBody:   $('#fig-body'),
    figFoot:   $('#fig-foot'),
    figExport: $('#fig-export'),
    figClose:  $('#fig-close'),
    proxyNote: $('#proxy-note'),
    inboxPath: $('#inbox-path'),
    inboxList: $('#inbox-list'),
  };

  // 当前这一轮的渲染状态。
  //
  // ⚠️ 用一个对象收着，而不是散在各处 —— 因为 turn_end 时要一起清掉。
  //    散着写的话，迟早会漏清一个，表现为"下一轮的文字跑进了上一轮的气泡里"。
  let turn = null;

  // ===========================================================================
  // 请求
  // ===========================================================================

  async function api(path, opts = {}) {
    let res;
    try {
      res = await fetch(path, {
        ...opts,
        headers: {
          'X-Token': TOKEN,
          'Content-Type': 'application/json',
          ...(opts.headers || {}),
        },
      });
    } catch (e) {
      // ⚠️ fetch 抛异常 = **网络层失败**，不是 HTTP 错误。
      //
      //    最常见的成因：**后台服务已经停了**（用户关了那个命令行窗口），
      //    但浏览器页面还开着、看起来一切正常。
      //
      //    这时浏览器给的原话是 "Failed to fetch" —— 一句技术黑话，
      //    用户看了完全不知道该怎么办。
      //
      //    所以这里翻译成：出了什么事 + 怎么办。
      markDisconnected();
      throw new Error('与后台的连接已断开。程序可能已经关闭 —— 重新双击 run.bat 启动即可。');
    }

    if (!res.ok) {
      let msg = `HTTP ${res.status}`;
      try { msg = (await res.json()).detail || msg; } catch { /* 响应不是 JSON */ }
      throw new Error(msg);
    }
    return res.json();
  }

  // ===========================================================================
  // 连接状态 —— 服务在不在
  // ===========================================================================
  //
  // ⚠️ 这个机制解决一个很具体、很容易发生的困惑：
  //
  //     用户双击 run.bat → 浏览器打开 → 用了一会儿
  //     → 关掉那个黑色的命令行窗口（他以为那只是个日志）
  //     → **服务停了，但网页还在**
  //     → 再点任何按钮 → "Failed to fetch"
  //
  //    他完全想不到"那个黑窗口"就是程序本体。
  //
  // 所以界面要主动发现并说清楚。

  let connected = true;

  function markDisconnected() {
    if (!connected) return;
    connected = false;
    let bar = document.getElementById('offline-bar');
    if (!bar) {
      bar = document.createElement('div');
      bar.id = 'offline-bar';
      bar.className = 'offline-bar';
      bar.innerHTML =
        '<b>与后台的连接已断开。</b>' +
        '程序可能已经关闭 —— 那个黑色的命令行窗口就是它。' +
        '<span class="offline-hint">重新双击 <code>run.bat</code> 即可继续。</span>';
      document.querySelector('.app').prepend(bar);
    }
    el.send.disabled = true;
    el.input.disabled = true;
  }

  function markConnected() {
    if (connected) return;
    connected = true;
    const bar = document.getElementById('offline-bar');
    if (bar) bar.remove();
    setBusy(false);
  }

  // 每 5 秒问一次"还在吗"。
  //
  // ⚠️ 用心跳而不是等用户点按钮才发现 ——
  //    主动告知比被动报错好得多：用户一看到横幅就知道是怎么回事了，
  //    不用等到某次操作失败才困惑。
  async function heartbeat() {
    try {
      await api('/api/ping');
      markConnected();
    } catch {
      markDisconnected();
    }
  }
  setInterval(heartbeat, 5000);

  // ===========================================================================
  // 侧栏：环境与网络
  // ===========================================================================

  const STATUS_ICON = { ok: '✓', warn: '!', fail: '✕', info: '·' };

  function renderChecks(listEl, items) {
    listEl.innerHTML = '';
    for (const it of items) {
      const li = document.createElement('li');
      const dot = document.createElement('span');
      dot.className = `check-dot ${it.status || it.icon || 'info'}`;

      const body = document.createElement('div');
      const name = document.createElement('div');
      name.className = 'check-name';
      name.textContent = it.label;
      body.appendChild(name);

      const detail = document.createElement('div');
      detail.className = 'check-detail';
      detail.textContent = it.detail || '';
      body.appendChild(detail);

      // ⚠️ advice 是**整份界面上最有价值的字段** ——
      //    它是"该怎么办"的具体指引。空的时候不要渲染空盒子。
      if (it.advice) {
        const adv = document.createElement('div');
        adv.className = 'check-advice';
        adv.textContent = it.advice;
        body.appendChild(adv);
      }

      li.append(dot, body);
      listEl.appendChild(li);
    }
  }

  function setPill(pillEl, text, status) {
    pillEl.textContent = text;
    pillEl.className = 'pill ' + (status || '');
  }

  async function loadEnvironment() {
    try {
      const data = await api('/api/environment');
      renderChecks(el.envList, data.items);
      setPill(el.envPill, data.ready ? '就绪' : '待处理',
              data.ready ? 'ok' : 'warn');

      // 收件箱路径从 paths 里拿（**不是**从 items 里找 —— 见下面 loadInbox）
      if (data.paths && data.paths.inbox) {
        el.inboxPath.textContent = data.paths.inbox;
      }
    } catch (e) {
      setPill(el.envPill, '检查失败', 'fail');
      el.envList.innerHTML = `<li><span class="check-dot fail"></span>
        <div><div class="check-name">读不到环境信息</div>
        <div class="check-detail">${escapeHtml(e.message)}</div></div></li>`;
    }
  }

  // ⚠️ 拆成两半：**取数据** 和 **画**。
  //
  //    因为现在有两个来源会把数据送过来：
  //      ① 打开界面时自己拉一次（loadNetwork）
  //      ② 模型刚跑完 `probe_data_sources`，结果就在事件里（renderNetwork）
  //
  //    ② 那条**不该再请求一遍** —— 再请求就是又测一次，
  //    慢，而且两次结果可能不一样（网络一直在变）。
  //    一份数据、一个画法，两个入口。
  async function loadNetwork() {
    setPill(el.netPill, '探测中…', '');
    el.proxyNote.textContent = '';
    try {
      renderNetwork(await api('/api/probe'));
    } catch (e) {
      setPill(el.netPill, '探测失败', 'fail');
      el.netList.innerHTML = `<li><span class="check-dot fail"></span>
        <div><div class="check-name">探测出错</div>
        <div class="check-detail">${escapeHtml(e.message)}</div></div></li>`;
    }
  }

  function renderNetwork(data) {
      // ⚠️ 按**用途**分组渲染，不是把所有源平铺成一列。
      //
      //    区别在于回答了哪个问题：
      //      平铺   → "这几台服务器通不通"（用户不关心）
      //      分组   → "我要的这类文件能不能拿到"（用户真正关心的）
      //
      //    分组标题里那句 help 尤其重要 —— 它解释"这一类是干什么的"，
      //    让不懂 GNSS 的用户也能判断"缺了它要不要紧"。
      el.netList.innerHTML = '';
      for (const g of data.groups || []) {
        const head = document.createElement('li');
        head.className = 'group-head';
        head.innerHTML =
          `<span class="check-dot ${g.icon}"></span>` +
          `<div><div class="check-name">${escapeHtml(g.purpose)}` +
          `<span class="group-sum">${escapeHtml(g.summary)}</span></div>` +
          `<div class="check-detail">${escapeHtml(g.help)}</div></div>`;
        el.netList.appendChild(head);

        for (const s of g.sources) {
          const li = document.createElement('li');
          li.className = 'group-item';
          const status = s.icon === 'ok' ? 'ok' : (s.icon === 'auth' ? 'warn' : 'fail');
          li.innerHTML =
            `<span class="check-dot ${status}"></span>` +
            `<div><div class="check-name">${escapeHtml(s.label)}</div>` +
            `<div class="check-detail">${escapeHtml(s.detail)}` +
            `${s.latency_ms ? ` · ${s.latency_ms} ms` : ''}</div>` +
            (s.advice ? `<div class="check-advice">${escapeHtml(s.advice)}</div>` : '') +
            `</div>`;
          el.netList.appendChild(li);
        }
      }

      // 顶部标签说**能不能干活**，不说"几台通几台不通"
      setPill(el.netPill, data.ok_for_work ? '就绪' : '需处理',
              data.ok_for_work ? 'ok' : 'warn');

      el.proxyNote.textContent = data.proxy ? `连接方式：${data.proxy}` : '';
  }

  // ===========================================================================
  // 消息渲染
  // ===========================================================================

  function escapeHtml(s) {
    return String(s ?? '').replace(/[&<>"']/g, (c) =>
      ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  }

  function hideEmpty() {
    if (el.empty && el.empty.parentNode) el.empty.remove();
  }

  // 是不是"跟着底部走"。
  //
  // ⚠️⚠️ 这个标志**只能由真实的滚动事件改写**，
  //     不能在渲染时按当时的几何距离临时算出来 —— 那会踩一个很隐蔽的坑，
  //     见下面 scrollDown 里的说明。
  let stickToBottom = true;

  el.messages.addEventListener('scroll', () => {
    const m = el.messages;
    // ⚠️ 我们自己设 scrollTop 时也会触发这个事件 —— 那没关系：
    //    设完之后的距离就是 0，算出来仍然是"跟底"，不会来回抖。
    stickToBottom = m.scrollHeight - m.scrollTop - m.clientHeight < 120;
  });

  function scrollDown() {
    // ⚠️ 只在用户**本来就贴着底部**时才自动滚动。
    //    用户往上翻看历史时，新内容不该把他拽回来 —— 这个需求是真的。
    //
    // ⚠️⚠️ 但"贴着底部没有"**不能在这里现算**。原来就是现算的，踩了坑：
    //
    //     渲染是**节流**的（120ms 一次，见 scheduleRender）。
    //     两次渲染之间模型可能吐出好几百像素的内容，于是这一次检查
    //     看到的距离**已经超过阈值** → 判定成"用户翻上去了" → **不滚了**。
    //
    //     而之后只会离得更远，于是**再也回不来**。
    //
    //     现象：回复一长，视野就停在**那条回复的标题**上（刚好是
    //     停止跟随的那一刻），内容在下面越长越多。用户的原话：
    //     "滚动到它回话的标题，不是最新的内容"。
    //
    //     👉 判断改由**真实的滚动事件**驱动（上面那个监听器）：
    //        只有用户**真的动手滚了**，标志才会变。
    //        内容自己变长**不触发 scroll 事件**，所以标志保持"跟底"。
    //
    //     **几何量会骗人，事件不会。**
    if (stickToBottom) el.messages.scrollTop = el.messages.scrollHeight;
  }

  function addUserMessage(text) {
    hideEmpty();
    const wrap = document.createElement('div');
    wrap.className = 'msg msg-user';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    bubble.textContent = text;
    wrap.appendChild(bubble);
    el.messages.appendChild(wrap);
    scrollDown();
  }

  // ---------------------------------------------------------------------------
  // 文字气泡 —— **一段一个**，不是"一轮一个"
  // ---------------------------------------------------------------------------
  //
  // ⚠️⚠️ 这里原来是一轮只建**一个**气泡，然后一路复用它。
  //     而工具卡片是在 tool_start 时**追加到末尾**的。
  //
  //     于是屏幕上变成：所有的话挤在第一个气泡里（位置在第一次
  //     出现文字的地方），所有工具卡片堆在下面 ——
  //     **模型说 → 调工具 → 再说** 这个顺序在界面上完全看不出来。
  //
  //     用户看到的是一段读不通的文字，后面跟着一堆不知道什么时候发生的工具。
  //
  //     现在照 Claude Code 的模型：**一次工具调用就是一次分段**。
  //        模型说一段  → 气泡 A
  //        调工具      → 卡片插在 A 下面
  //        接着说      → **新建气泡 B**，出现在卡片下面
  //     新元素永远追加在末尾，所以 **DOM 顺序天然等于事件顺序**。

  function ensureAgentBubble() {
    if (turn && turn.seg) return turn.seg.el;
    hideEmpty();
    const wrap = document.createElement('div');
    wrap.className = 'msg msg-agent streaming';
    const bubble = document.createElement('div');
    bubble.className = 'bubble';
    wrap.appendChild(bubble);
    el.messages.appendChild(wrap);

    // ⚠️ 节流状态挂在**气泡**上，不是挂在 turn 上。
    //    挂 turn 上的话，第二段会把第一段的文字**重新渲染一遍**
    //    （因为 raw 是共享的）。
    turn.seg = { el: bubble, wrap, raw: '', timer: null };
    return bubble;
  }

  /** 把当前这段文字**封口** —— 后面再有文字就开新的一段。 */
  function sealBubble() {
    if (!turn || !turn.seg) return;
    // ⚠️ 封口时要**立刻**做完那次挂起的渲染，不留节流的尾巴 ——
    //    否则这一段的最后 120ms 内容会永远不显示。
    if (turn.seg.timer) {
      clearTimeout(turn.seg.timer);
      turn.seg.timer = null;
    }
    if (turn.seg.raw) {
      turn.seg.el.innerHTML = window.renderMarkdown(turn.seg.raw);
    }
    turn.seg.wrap.classList.remove('streaming');
    turn.seg = null;

    // ⚠️ 封口这次渲染**没有**走 scheduleRender，所以它不会自己滚 ——
    //    而这次渲染恰恰是最后一段文字落地、高度变化最大的一次
    //    （Markdown 一渲染，表格/列表会撑高一大截）。
    //    不补这一下，一轮的**结尾**就留在屏幕外面了。
    scrollDown();
  }

  // 把累积的原始文本渲染成 HTML。
  //
  // ⚠️ 为什么要**节流**，不能每来一个 text_delta 就渲染一次？
  //
  //    因为模型一次回复会产生几十上百个 text_delta。
  //    每次都跑一遍 Markdown 解析 + 重设 innerHTML，是把一个
  //    本来很轻的操作做成了 O(n²)。
  //
  //    更实际的问题是**视觉抖动**：表格在流到一半时是残缺的
  //    （只有表头没有分隔行），会被当成普通段落渲染成一堆竖线，
  //    等下一片数据到了再"跳"成表格。节流到 120ms 后，
  //    这种中间态基本看不见。
  function scheduleRender() {
    if (!turn || !turn.seg || turn.seg.timer) return;
    const seg = turn.seg;
    seg.timer = setTimeout(() => {
      seg.timer = null;
      seg.el.innerHTML = window.renderMarkdown(seg.raw);
      scrollDown();
    }, 120);
  }

  function renderNow() {
    if (!turn || !turn.seg) return;
    const seg = turn.seg;
    if (seg.timer) {
      clearTimeout(seg.timer);
      seg.timer = null;
    }
    seg.el.innerHTML = window.renderMarkdown(seg.raw);
  }

  /** 往对话里插一条提示条。
   *
   *  ⚠️ 返回元素本身 —— 因为**有些提示必须能被撤掉**（见 stopNotice）。
   *     不返回的话调用方就只能自己去 DOM 里找，那太脆了。 */
  function addNotice(text, kind = '') {
    hideEmpty();
    const div = document.createElement('div');
    div.className = 'notice ' + kind;
    div.textContent = text;
    el.messages.appendChild(div);
    scrollDown();
    return div;
  }

  // ===========================================================================
  // 工具卡片
  // ===========================================================================

  // ⚠️ **15 个工具全都要有中文名。**
  //
  //    原来只映射了 4 个 —— 剩下的（fetch_data、run_processing、
  //    summarize_results…）在界面上显示的是**英文原名**。
  //    而用户是"完全不懂 GNSS 的人"，他不会知道 `run_processing`
  //    是在干什么，只能看着一个陌生的词干等。
  //
  //    ⚠️ 漏掉一个的表现很隐蔽：界面上就那一行是英文的，
  //       看着像"没写完"，但不会报错。
  const TOOL_LABEL = {
    check_environment:  '检查运行环境',
    probe_data_sources: '测试数据源',
    plan_run:           '制定取数计划',
    fetch_data:         '下载数据',
    generate_config:    '生成配置',
    validate_config:    '检查配置',
    run_processing:     '运行解算',
    run_residual_diagnostics: '看观测质量（残差）',
    summarize_results:  '解读结果',
    compare_orbits:     '比对轨道',
    advise_from_inbox:  '看看这些数据能算什么',
    scan_inbox:         '检查收件箱',
    list_local_obs:     '查看已有的观测数据',
    get_data_dir:       '查看数据目录',
    inspect_rinex:      '检查 RINEX 文件',
    repair_rinex:       '补全 RINEX 头部',
    list_antennas:      '查找天线型号',
    workspace_status:   '查看当前进度',
    ask_user:           '询问你',
  };

  function toolCard(name) {
    hideEmpty();
    const card = document.createElement('div');
    card.className = 'tool collapsed';

    const head = document.createElement('div');
    head.className = 'tool-head';

    const spin = document.createElement('div');
    spin.className = 'tool-spinner';
    head.appendChild(spin);

    const label = document.createElement('span');
    label.className = 'tool-name';
    label.textContent = TOOL_LABEL[name] || name;
    head.appendChild(label);

    const status = document.createElement('span');
    status.className = 'tool-status';
    status.textContent = '执行中…';
    head.appendChild(status);

    const progress = document.createElement('div');
    progress.className = 'tool-progress hidden';
    progress.innerHTML = '<div class="progress-track"><div class="progress-fill"></div></div><div class="progress-text"></div>';

    const body = document.createElement('div');
    body.className = 'tool-body';

    // 点击展开/折叠详情
    head.addEventListener('click', () => card.classList.toggle('collapsed'));

    card.append(head, progress, body);
    el.messages.appendChild(card);
    scrollDown();

    return { card, head, spin, status, progress, body };
  }

  /** 按 `call_id` 找卡片；找不到就**当场补一张**。
   *
   * ⚠️⚠️ 两个理由，都踩过：
   *
   *    ① **同一轮里同一个工具被调两次**（先试跑再完整跑）——
   *       原来按 `name` 当 key，第二张卡片会把第一张顶掉，
   *       进度也会串到错的那张上。
   *
   *    ② **有些事件没有配对的 tool_start** ——
   *       比如取消时给"还没轮到的工具"补的那条 tool_end。
   *       那时 `turn.tools.get(...)` 是空的，于是**什么都不显示** ——
   *       用户点了停止，"剩下的几个工具"就那么无声无息地消失了。
   *
   *    👉 补建卡片比"找不到就跳过"诚实：**发生了的事就该看得见。**
   */
  function cardFor(ev) {
    const key = ev.call_id || ev.name;
    let c = turn.tools.get(key);
    if (!c) {
      c = toolCard(ev.name);
      turn.tools.set(key, c);
    }
    return c;
  }

  /** 按状态收尾。
   *
   * ⚠️ 状态**来自服务端**，不在前端猜。
   *    原来这里硬编码"收到 tool_end 就是成功"，
   *    于是两种根本没跑的情况也画着绿勾"完成"：
   *      · `not_run`   —— 回复被长度截断，工具**没有执行**
   *      · `cancelled` —— 用户点了停止
   *    **一个说"完成了"的失败，比说"失败了"糟得多。**
   */
  const STATUS_STYLE = {
    ok:        { icon: 'M20 6 9 17l-5-5',            cls: 'done',     text: '完成' },
    denied:    { icon: 'M18 6 6 18M6 6l12 12',       cls: 'denied',   text: '已拒绝' },
    cancelled: { icon: 'M8 8h8v8H8z',                cls: 'cancelled', text: '已停止' },
    not_run:   { icon: 'M12 8v5M12 16h.01',          cls: 'not-run',  text: '未执行' },
    failed:    { icon: 'M18 6 6 18M6 6l12 12',       cls: 'failed',   text: '失败' },
  };

  function finishTool(c, status, text) {
    const style = STATUS_STYLE[status] || STATUS_STYLE.ok;

    if (c.spin.parentNode) c.spin.remove();
    const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    icon.setAttribute('class', 'tool-icon');
    icon.setAttribute('viewBox', '0 0 24 24');
    icon.setAttribute('fill', 'none');
    icon.setAttribute('stroke', 'currentColor');
    icon.setAttribute('stroke-width', '2.4');
    icon.setAttribute('stroke-linecap', 'round');
    icon.setAttribute('stroke-linejoin', 'round');
    const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
    path.setAttribute('d', style.icon);
    icon.appendChild(path);
    c.head.insertBefore(icon, c.head.firstChild);

    c.card.classList.remove('collapsed');
    c.card.classList.add(style.cls);
    c.status.textContent = style.text;
    c.progress.classList.add('hidden');
    c.body.textContent = text || '';
    scrollDown();
  }

  // ===========================================================================
  // SSE 事件处理
  // ===========================================================================

  // 工具跑完之后，哪些**侧栏面板**要跟着刷新。
  //
  // ⚠️ 原来侧栏只在三个时刻更新：启动、手动点"重新检查"、存完凭据。
  //    于是**模型自己测了一遍数据源，右边还显示着几十分钟前的旧状态** ——
  //    用户看到的是"上面说 CDDIS 通了，右边说需要登录"，
  //    而正确的那个在对话里，右边那个是过期的。
  //
  //    ⚠️ 关键是**用工具返回的结果直接渲染**，不要再去请求一遍 ——
  //       再请求一次就是又测一遍，慢，而且两次结果可能不一样。
  const AFTER_TOOL = {
    probe_data_sources: (ev) => {
      try {
        const d = JSON.parse(ev.result);
        if (d && d.groups) { renderNetwork(d); return; }
      } catch (e) { /* 解析不了就退回重新拉一次 */ }
      loadNetwork();
    },
    check_environment: () => loadEnvironment(),
    run_processing:    () => loadResults(),
    summarize_results: () => loadResults(),
    scan_inbox:        () => loadInbox(),
  };

  function handleEvent(ev) {
    switch (ev.type) {
      // ⚠️ 这一轮真的开始了 —— 服务端确认过的那种开始。
      //    见 send() 里的说明：前端也会乐观地先显示一次，
      //    两条路都指向同一句话，不冲突。
      case 'turn_start':
        hint('已发送，正在等模型回应…');
        break;

      case 'text_delta':
        hint('');                      // 有字出来了，就不用再提示"在等"
        ensureAgentBubble();
        turn.sawText = true;
        turn.seg.raw += ev.text;
        scheduleRender();
        break;

      case 'tool_start': {
        // ⚠️ **先把当前这段文字封口**，卡片才会落在它下面。
        //    这就是"一次工具调用 = 一次分段"那一条的落点。
        sealBubble();
        const card = toolCard(ev.name);
        turn.tools.set(ev.call_id || ev.name, card);
        hint(`正在执行：${TOOL_LABEL[ev.name] || ev.name}…`);
        break;
      }

      case 'tool_progress': {
        const c = turn.tools.get(ev.call_id || ev.name);
        if (!c) break;
        c.progress.classList.remove('hidden');
        const fill = c.progress.querySelector('.progress-fill');
        const text = c.progress.querySelector('.progress-text');
        if (typeof ev.fraction === 'number') {
          fill.classList.remove('indeterminate');
          fill.style.width = Math.round(ev.fraction * 100) + '%';
        } else {
          // ⚠️ fraction 为 null = 不知道总量。
          //    这时显示来回滑动的动画，而**不是**假装知道百分比 ——
          //    假装的话用户会盯着一个永远不动的 50% 发愁。
          fill.classList.add('indeterminate');
          fill.style.width = '';
        }
        text.textContent = ev.message || '';
        hint(ev.message || '');
        break;
      }

      case 'tool_end': {
        const c = cardFor(ev);
        finishTool(c, ev.status || 'ok', ev.result);
        // ⚠️ 工具结束**不等于**这一轮结束 —— 模型还要接着想。
        //    不把提示改回来的话，界面会一直停在上一条工具进度的文字上，
        //    而实际上早就在等模型了。
        hint('继续思考中…');
        const after = AFTER_TOOL[ev.name];
        if (after) { try { after(ev); } catch (e) { /* 侧栏刷新失败不该影响对话 */ } }
        break;
      }

      case 'tool_denied': {
        const c = cardFor(ev);
        finishTool(c, 'denied', ev.reason);
        hint('继续思考中…');
        break;
      }

      // ⚠️ 工具在**等用户回答**，而且它是阻塞的 —— 不回答就一直卡着。
      //    所以这里必须画出可点的东西，并且把提示改成"等你"。
      case 'ask_user':
        sealBubble();
        renderAsk(ev);
        hint('等你回答 ↑');
        break;

      case 'retry':
        addNotice(`网络不稳，正在重试（${ev.attempt}/${ev.max_attempts}）…`, 'warn');
        hint('网络不稳，正在重试…');
        break;

      case 'compact_start':
        addNotice(`对话较长，正在压缩历史（${ev.message_count} 条）…`);
        hint('正在压缩对话历史…');
        break;

      case 'compact_end':
        addNotice('历史已压缩。');
        break;

      case 'error':
        addNotice(`出错了：${ev.message}`, 'fail');
        hint('');
        break;

      case 'turn_end':
        // ⚠️⚠️ **这一轮结束了，可屏幕上可能什么都没有。**
        //
        //     模型的字是走 `text_delta` 一条条画上去的。
        //     但有些话**根本不是模型说的**，而是服务端那边加的：
        //
        //        · 撞到单轮步数上限   （"已达到最大步数…"）
        //        · 用户点了停止        （"（已停止。…）"）
        //
        //     这两种情况**一条 text_delta 都没有** —— 它们只存在于
        //     `turn_end.text` 里。而这里原来是 `endTurn(); break;`，
        //     **那个字段从来没被用过**。
        //
        //     于是：用户干了一堆活（下载了 6 个文件、全成功），
        //     然后**一个字都不显示**。他问的原话就是"下载成功后什么都不显示"。
        //
        //     ⚠️ 这不是"少显示一句话"，而是**整轮的结论凭空消失** ——
        //        用户不知道是成功了、失败了、还是卡住了。
        //
        //     ⚠️ 为什么必须判断 `sawText`：`turn_end.text` 在正常一轮里
        //        是**已经吐过的那些字的完整版**（给脚本/测试用的）。
        //        不分青红皂白地渲染，正常回答会被**画两遍**。
        if (turn && !turn.sawText) {
          if (ev.text) {
            // 这一轮压根没吐过字，而结束时带着话 → 那就是这一轮要说的话
            ensureAgentBubble();
            turn.seg.raw += ev.text;
            turn.sawText = true;
          } else if (turn.tools.size === 0) {
            // 一个字没有、一个工具也没跑 —— 真的什么都没发生。
            // 这多半是上游出问题了（网络断了、服务商抽风），
            // **不能就这么静静地结束**，否则和"程序卡死"没法区分。
            addNotice(
              '模型这次没有回任何内容 —— 多半是网络或服务商那边的问题。' +
              '再发一次试试。',
              'warn'
            );
          }
        }
        endTurn();
        break;
    }
  }

  /** 显示/清掉输入框下面那行小字。 */
  function hint(text) {
    el.hint.textContent = text || '';
  }

  // ---------------------------------------------------------------------------
  // 提问块
  // ---------------------------------------------------------------------------

  function renderAsk(ev) {
    hideEmpty();
    const box = document.createElement('div');
    box.className = 'ask';

    const q = document.createElement('p');
    q.className = 'ask-q';
    q.textContent = ev.question;
    box.appendChild(q);

    const row = document.createElement('div');
    row.className = 'ask-opts';

    const buttons = [];

    function settle(chosen, label) {
      buttons.forEach((b) => { b.disabled = true; b.classList.remove('picked'); });
      if (chosen) chosen.classList.add('picked');
      box.classList.add('answered');
      // ⚠️ 把用户的选择**留在对话里** —— 否则刷新或被顶上去之后，
      //    就看不出来他当时选了什么。和 Claude Code 一样。
      const note = document.createElement('div');
      note.className = 'ask-answer';
      note.textContent = `你选了：${label}`;
      box.appendChild(note);
      scrollDown();
    }

    function submit(value, label, btn) {
      api('/api/answer', {
        method: 'POST',
        body: JSON.stringify({ call_id: ev.call_id, value }),
      }).then(() => {
        settle(btn, label);
        hint('继续思考中…');
      }).catch((err) => {
        // ⚠️ 409 = 这个问题已经不在了（超时或已答过）。
        //    如实说，不要假装成功 —— 假装的话用户会以为他的选择生效了。
        hint('这个问题的回答没能送出去：' + (err.message || err));
      });
    }

    (ev.options || []).forEach((opt, i) => {
      const b = document.createElement('button');
      b.type = 'button';
      b.className = 'ask-btn' + (i === 0 ? ' primary' : '');
      b.textContent = opt.label;
      if (opt.detail) {
        const d = document.createElement('span');
        d.className = 'ask-detail';
        d.textContent = opt.detail;
        b.appendChild(d);
      }
      b.addEventListener('click', () => submit(opt.label, opt.label, b));
      buttons.push(b);
      row.appendChild(b);
    });

    box.appendChild(row);

    // 允许自己写一段的情况（比如问天线型号，选项列不全）
    if (ev.allow_text !== false) {
      const free = document.createElement('div');
      free.className = 'ask-free';
      const input = document.createElement('input');
      input.type = 'text';
      input.placeholder = '也可以自己写…';
      const go = document.createElement('button');
      go.type = 'button';
      go.className = 'ask-btn';
      go.textContent = '发送';
      const fire = () => {
        const v = input.value.trim();
        if (v) submit(v, v, null);
      };
      go.addEventListener('click', fire);
      // ⚠️⚠️ **必须排除输入法组合状态** —— 和主输入框同一个理由
      //    （见下面 el.input 那处：中文用户按 Enter 是在**选词**，
      //      不是要发送）。
      //
      //    这个框里用户会打**天线型号、测站名** —— 正是中文用户会
      //    用输入法的地方。少了这一条，他选词的那一下就把半截话发出去了。
      //
      //    ⚠️ 同一份文件里，主输入框有这一条、这个框没有 ——
      //       "一个原则只要有一处不照做，那一处就是全部"。
      input.addEventListener('keydown', (e) => {
        if (e.key === 'Enter' && !e.isComposing) fire();
      });
      free.append(input, go);
      box.appendChild(free);
      buttons.push(go);
    }

    el.messages.appendChild(box);
    scrollDown();
    buttons[0] && buttons[0].focus();
  }

  /** 把上次的对话画回界面（页面加载时调一次）。 */
  async function renderHistory() {
    let d;
    try {
      d = await api('/api/history');
    } catch (e) {
      return;                     // 画不出来就不画，别挡住新的对话
    }
    const msgs = (d && d.messages) || [];
    if (!msgs.length) return;     // 没有历史：保持空状态卡片

    hideEmpty();
    for (const m of msgs) {
      if (m.role === 'user') {
        // ⚠️ 画历史时**不要再跑一遍 addUserMessage 的副作用** ——
        //    比如 hideEmpty 和滚动。这里只要节点。
        addUserMessage(m.text);
      } else {
        // 助手消息要**完整渲染 Markdown**（和实时流式走同一个渲染器），
        // 否则历史里会露出字面的 ** 和 |。
        const wrap = document.createElement('div');
        wrap.className = 'msg msg-agent';
        // ⚠️ 变量名要**说得出它是模型输出气泡** ——
        //    叫 `bubble` 的话，将来某个工具卡片也叫 bubble，
        //    而"Markdown 只进模型气泡"那条守卫就分不出来了。
        const histBubble = document.createElement('div');
        histBubble.className = 'bubble';
        histBubble.innerHTML = window.renderMarkdown(m.text);
        wrap.appendChild(histBubble);
        el.messages.appendChild(wrap);
      }
    }
    const m = el.messages;
    m.scrollTop = m.scrollHeight;
  }

  /** 刷新页面后，把还在等回答的问题画回来。 */
  async function restorePendingAsk() {
    try {
      const d = await api('/api/pending');
      if (d && d.question) {
        newTurn();                 // 那一轮在服务端还活着
        setBusy(true);
        renderAsk(d.question);
        hint('等你回答 ↑');
      }
    } catch (e) { /* 没有就算了 */ }
  }

  // ===========================================================================
  // 一轮的生命周期
  // ===========================================================================

  function newTurn() {
    // ⚠️ `seg` = **当前正在接收文字的那一段**（可能没有）。
    //    它和 `tools` 一起构成"这一轮的当前状态"。
    //
    // ⚠️ `sawText` = 这一轮**有没有吐过字**。
    //    它专门用来处理"这一轮结束了，可屏幕上什么都没有"那件事 ——
    //    见 turn_end 里的说明。不加这个标志就分不清
    //    "字早就画上去了"和"压根没收到过字"。
    turn = { seg: null, tools: new Map(), sawText: false };

    // ⚠️ **一轮开始 = 重新跟底。**
    //
    //    用户刚说了句话，他要看的就是接下来的回答。如果这时候
    //    还保持"他之前在翻历史所以不跟底"，那刚发出去的话
    //    和整个回答都会在屏幕外面 —— 而他明明刚刚才发了消息。
    //
    //    **发送这个动作本身就是"我要看下面"的意思。**
    stickToBottom = true;
  }

  function endTurn() {
    // ⚠️ 封口会把挂起的那次渲染立刻做完，并去掉光标 —— 见 sealBubble。
    sealBubble();
    turn = null;
    setBusy(false);
  }

  function setBusy(busy) {
    el.send.classList.toggle('hidden', busy);
    el.stop.classList.toggle('hidden', !busy);
    el.input.disabled = busy;
    if (!busy) {
      // ⚠️⚠️ 收工时把所有"进行中"的痕迹清掉 —— **这是唯一一处收工点**。
      //    不清的话下一轮开始时会带着上一轮的残留。
      //
      //    ⚠️ 这里必须包含 clearStopNotice()：那条提示说的是"此刻正在停"，
      //       停完了还挂着，用户就会以为**还没停**。见 stopNotice 的说明。
      //       （用户原话："这个提示就该删除或隐藏"。）
      stopping = false;
      el.stop.disabled = false;
      el.stop.classList.remove('stopping');
      clearStopNotice();
      el.hint.textContent = '';
      el.input.focus();
    }
  }

  async function send(text) {
    text = (text ?? el.input.value).trim();
    if (!text || turn) return;

    el.input.value = '';
    autoGrow();
    addUserMessage(text);
    newTurn();
    setBusy(true);

    // ⚠️⚠️ **乐观地先显示"在等"**，不等服务端确认。
    //
    //     用户的原话："发完消息不知道他在干什么，没有回馈，
    //                 比如思考中，出东西了才知道他在干活"。
    //
    //     而从"按下发送"到"第一个字"之间是模型首字延迟 + 网络往返 ——
    //     几秒到几十秒都可能，期间**原来一个信号都没有**。
    //
    //     服务端也会发一条 `turn_start`（那条更可靠，能区分"真的开始了"
    //     和"上一轮还没跑完"），但**它要等一个来回才到**。
    //     用户按下去的那一瞬间就该有东西可看，所以两边都做：
    //     这里负责"立刻有反应"，`turn_start` 负责"确认真的开始了"。
    hint('已发送，正在等模型回应…');

    try {
      await api('/api/turn', { method: 'POST', body: JSON.stringify({ text }) });
    } catch (e) {
      addNotice(String(e.message), 'fail');
      endTurn();
    }
  }

  // 正在停止中 —— 全局标志，turn_end 时清掉。
  let stopping = false;

  // 「正在停止……」那条提示的引用，**只为了在停完之后把它撤掉**。
  //
  // ⚠️⚠️ 这条是用户点出来的：
  //         "点停止之后……这个提示就该删除或隐藏"
  //
  //     原来它是 `addNotice()` 写进对话里的，**然后就一直在那儿**。
  //     于是停止完成之后，屏幕上还挂着一句"正在停止……会等当前这一步
  //     跑完（最长几十秒）"—— 它描述的是**此刻正在发生的事**，
  //     而此刻早就停完了。用户看到它只会以为"还没停"。
  //
  //     👉 它是**状态**，不是一句话。状态在事情结束时就该消失。
  //
  //     ⚠️ 但也不能干脆删掉它：取消是**协作式**的，最长要等几十秒
  //        （见 stop() 里那段说明）。那几十秒里用户必须有反馈，
  //        否则"点了没反应"和"程序卡死"在他眼里是一回事。
  //        所以是**留到停完为止**，不是不留。
  let stopNotice = null;

  async function stop() {
    if (stopping) return;
    stopping = true;

    // ⚠️⚠️ 这里的每一处反馈都是必要的，原因值得说清楚：
    //
    //    取消是**协作式**的 —— 循环层只能在"工具之间"检查取消标志。
    //    如果用户点停止时正好有个长工具在跑（比如探 11 个数据源，
    //    最坏 40 秒），那一步**必须跑完**才会停下来。
    //
    //    实测用户的原话是"我点击停止他不停" ——
    //    而它其实在停，只是**看起来毫无反应**。
    //
    //    "点了没反应"和"程序卡死了"在用户眼里是一回事。
    //
    //    所以要说清楚：① 收到了 ② 要等多久 ③ 现在在等什么

    el.stop.disabled = true;
    el.stop.classList.add('stopping');
    el.hint.textContent = '';

    // ⚠️ 存下元素 —— 停完之后要撤掉它（下一行的那句说明在 setBusy(false) 里）
    stopNotice = addNotice(
      '正在停止…… 会等当前这一步跑完（最长几十秒）。' +
      '如果正在探测数据源或下载，那一步不会被打断。',
      'warn'
    );

    try {
      await api('/api/stop', { method: 'POST' });
    } catch (e) {
      stopping = false;
      el.stop.disabled = false;
      el.stop.classList.remove('stopping');
      // 请求都失败了，就别再留着"正在停止"那句话 —— 它已经不是在发生的事。
      clearStopNotice();
      addNotice('停止请求失败了：' + e.message, 'fail');
    }
  }

  /** 撤掉「正在停止……」那条提示。没在显示的话什么都不做。 */
  function clearStopNotice() {
    if (stopNotice) {
      stopNotice.remove();
      stopNotice = null;
    }
  }

  // ===========================================================================
  // 事件流
  // ===========================================================================

  function connect() {
    const es = new EventSource(`/api/events?x_token=${encodeURIComponent(TOKEN)}`);

    es.onmessage = (m) => {
      try { handleEvent(JSON.parse(m.data)); }
      catch (e) { console.error('事件解析失败', e, m.data); }
    };

    // 服务端在每轮结束时发这个
    // ⚠️⚠️ `done` = 服务端那一轮**结束了**（TURN_DONE）。
    //
    //    这里原来是一句空注释：「turn_end 已经处理过了」——
    //    **在正常那一条路上是成立的，在出错那条路上不成立。**
    //
    //    服务端 `Hub._run` 的结构是：
    //
    //        try:    for event in agent.run(...): publish(event)
    //        except: publish({"type": "error", ...})      ← 只推 error
    //        finally: publish(TURN_DONE)                  ← 然后就是 done
    //
    //    异常一抛，生成器就断了，**`turn_end` 永远不会来** ——
    //    而 `turn_end` 是**唯一**调 `endTurn()` 的地方。
    //
    //    后果（首次使用最常见的路径：没配 API Key 就发了句话）：
    //      · `turn` 一直非空、`setBusy(false)` 从来没跑
    //      · 发送按钮 hidden、输入框 disabled —— **发不出第二句话**
    //      · 「停止」按钮点一下会把自己 disabled 掉（只有 setBusy(false)
    //        能恢复），于是**连停止也点不动了**
    //      · 「正在停止……会等当前这一步跑完」**永久留在屏幕上**
    //        （它只在 setBusy(false) → clearStopNotice() 里撤）
    //
    //    ⚠️ 所以 `done` 必须兜底。而 `endTurn()` 是幂等的
    //       （`sealBubble` 有 `if (!turn...) return`，`setBusy(false)` 只是设状态），
    //       正常那条路上 `turn` 已经是 null，这里就是个空操作。
    es.addEventListener('done', () => {
      if (turn) endTurn();
    });

    es.onerror = () => {
      // ⚠️ EventSource 会自动重连，不用手动重建。
      //    但如果正在跑一轮而连接断了，界面会永远卡在"处理中"——
      //    所以这里主动解一下锁。
      if (turn) {
        addNotice('与后台的连接断开了，请刷新页面。', 'fail');
        endTurn();
      }
    };
  }

  // ===========================================================================
  // 输入框
  // ===========================================================================

  function autoGrow() {
    el.input.style.height = 'auto';
    el.input.style.height = Math.min(el.input.scrollHeight, 180) + 'px';
  }

  el.input.addEventListener('input', autoGrow);

  el.input.addEventListener('keydown', (e) => {
    // Enter 发送，Shift+Enter 换行。
    // ⚠️ 必须排除输入法组合状态 —— 中文用户按 Enter 选词时
    //    不应该把半截话发出去。isComposing 就是干这个的。
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) {
      e.preventDefault();
      send();
    }
  });

  el.send.addEventListener('click', () => send());
  el.stop.addEventListener('click', stop);

  el.refresh.addEventListener('click', () => {
    loadEnvironment();
    loadNetwork();
    loadResults();
    loadInbox();
  });

  // ===========================================================================
  // 新任务
  // ===========================================================================
  //
  // ⚠️ 要**说清楚保留了什么**。
  //
  //    用户看到"清空"会担心两件事：① 我的对话没了 ② 我下了一小时的
  //    数据是不是也没了。
  //
  //    第一件是他想要的，第二件是他最怕的。所以确认框里必须写明
  //    "已下载的数据会保留" —— 否则他不敢点。

  // ⚠️⚠️ 「新任务」删了什么，**必须报出来** —— 而它原来一个字都没报。
  //
  //     两个原因叠在一起，正好把那份报告吞掉：
  //       ① `newTask()` 里 `const r = await api('/api/reset')` 拿到手就丢了
  //          （`grep 'r.cleared' app.js` 是空的）
  //       ② 紧接着 `location.reload()` 把 DOM 整个换掉
  //
  //     所以后端算的那份「删了什么 / 留了什么 / **什么没删掉**」
  //     从头到尾没有到过用户眼前。
  //
  //     2026-09-21 用户报的正是这个：点了新任务，收件箱里**还剩一个文件**
  //     （当时被别的程序占着，删不掉）。故障本身几分钟后自己好了 ——
  //     但"用户不知道发生了什么"这件事不会自己好。
  //
  //     👉 报告先存进 `sessionStorage`（它能活过 reload），
  //        页面回来后立刻显示，**读完就删**（只报一次）。
  function showNewTaskReport() {
    let rep = null;
    try {
      const raw = sessionStorage.getItem('ginan-newtask');
      if (!raw) return;
      rep = JSON.parse(raw);
    } catch (e) {
      return;                       // 存坏了就当没有，别让首屏炸掉
    } finally {
      sessionStorage.removeItem('ginan-newtask');
    }
    if (!rep) return;

    // ⚠️ **没删掉的排在最前面，而且是警告色。**
    //    这份报告里只有它需要用户做点什么（重试一次），
    //    其余的（删掉了什么、留了什么）都只是告知。
    for (const f of rep.failed || []) {
      // ⚠️ 要说**下一步干什么**。用户不懂 WinError 32 是什么，
      //    但他能"关掉正在跑的计算、再点一次"。
      addNotice(
        `⚠️ 收件箱里这个没删掉：${f}。` +
        `多半是有程序正占着它，关掉正在跑的计算，再点一次「新任务」。`,
        'warn',
      );
    }
    for (const c of rep.cleared || []) addNotice(`新任务：${c}`);
    for (const k of rep.kept || []) addNotice(`保留：${k}`);
    if (rep.note) addNotice(rep.note);
  }

  async function newTask() {
    // ⚠️⚠️ 这段文案要**和实际行为一致**，而且要说全 —— 它会删掉用户的数据。
    //
    //     这是整个程序里**唯一**会删用户文件的地方，所以文案的准确度
    //     不是"体验好不好"的问题，是"用户会不会丢东西"的问题。
    //
    //     原来这里写着"会保留：已下载的数据文件" —— **那句话现在是错的**。
    //     观测数据（他自己测的那份）会被删掉。
    //     一个说得比实际温柔的文案，比没有文案更危险：
    //     用户会基于它做出"点了没事"的判断。
    //
    //     ⚠️ 所以"会删掉"那一条要放在**最前面**，而且要点名是观测数据：
    //        "数据文件"这个词太笼统，用户不会把它和自己那份 50 MB 的观测文件
    //        对起来。
    //
    //     ⚠️⚠️ 这里**一个 `**` 都不能有**。`confirm()` 是浏览器的原生对话框，
    //        它**不渲染 Markdown** —— 写了 `**观测数据**`，用户看到的就是
    //        一排星号。原来这段文案里就有（`**已经认领过**`），一直露着。
    //        要强调就换个说法或者加 `⚠️`，别用 Markdown。
    //        （`scripts/check_ui_text.py` 现在会拦这一种，见 ⑥）
    // ⚠️⚠️ 「会保留：收件箱里还没认领过的文件」那一行**删掉了** —— 它现在是假话。
    //
    //     用户 2026-09-21 的原话：
    //        「inbox文件夹重置应该是清空，我点新任务里边还有东西」
    //        「没认领也删除」「点新任务什么都不留」
    //
    //     👉 收件箱现在是**整个清空**，不分认领没认领。
    //        一份"会保留"的清单里挂着一条已经会删的 ——
    //        用户会照着它做"点了没事"的判断，然后东西就没了。
    //
    //     ⚠️ 它排在最前面，因为它是**用户自己拖进来的东西**：
    //        "数据文件"这种词太笼统，用户不会把它和自己那份 50 MB 的
    //        观测文件/收件箱对起来。
    const ok = confirm(
      '开始新任务？\n\n' +
      '⚠️ 会删掉：\n' +
      '  · 收件箱里的全部文件 —— 认领过的、没认领过的都删，\n' +
      '      里面的文件夹也一并撤掉。收件箱会变成空的。\n' +
      '  · 观测数据 —— 你自己测的那份（例如 ALIC.rnx）\n' +
      '      缓存不永久留观测数据，这是定好的规则。\n' +
      '      要重算的话，把原始文件重新放进收件箱即可。\n' +
      '  · 对话记录\n' +
      '  · 运行记录 —— 上一次解算的全部产物：结果（.POS / .SP3 /\n' +
      '      .CLK）、日志、中间文件，整个目录清空。\n' +
      '      ⚠️ 那是磁盘上的大头（一次解算几百 MB 到几 GB）。\n' +
      '      删了就真的没了，要再看到那份结果只能重跑一次。\n' +
      '  · 任务状态（计划 / 配置）\n\n' +
      '会保留：\n' +
      '  · 卫星星历 / 钟差 / 天线表这些产品（不用重下）\n' +
      '  · API 密钥等设置'
    );
    if (!ok) return;

    try {
      const r = await api('/api/reset', { method: 'POST' });

      // ⚠️⚠️ **先把报告存下来，再 reload。**
      //    顺序反了就什么都没了 —— reload 会把这份 `r` 一起带走。
      //    （这份报告里最关键的一条是"什么没删掉"，见 `showNewTaskReport`。）
      try {
        sessionStorage.setItem('ginan-newtask', JSON.stringify({
          cleared: r.cleared || [],
          kept: r.kept || [],
          failed: r.failed || [],
          note: r.note || '',
        }));
      } catch (e) {
        // 存不下（隐私模式之类）也不该挡住"新任务"本身 ——
        // 内存里那份状态已经清了，这是主要目的。
      }

      // 清空界面上的对话
      el.messages.innerHTML = '';
      // ⚠️ 空状态卡片要恢复 —— 否则清完之后是一大片空白，
      //    用户不知道能干什么。
      location.reload();
    } catch (e) {
      alert('开始新任务失败：' + e.message);
    }
  }

  $('#btn-newtask').addEventListener('click', newTask);

  // ===========================================================================
  // 收件箱
  // ===========================================================================
  //
  // ⚠️ 这一块**之前和环境检查里的"收件箱"条目重复了**，已经合并到这里。
  //
  //    一个东西出现在两个地方，用户会疑惑"哪个是真的"。
  //    而且环境检查里那条永远显示为 info，会稀释真正要看的问题信号。
  //
  // ⚠️ 面板里必须有**能用的按钮**。
  //    只显示一句"把文件放进去，程序会认领"而没有认领按钮，
  //    和之前 CDDIS 那条"在设置里填用户名密码"（但设置里没那个字段）
  //    是同一类问题：**让用户去做一件做不到的事**。

  // ===========================================================================
  // 结果面板 —— 结论在侧栏，图在全屏浮层
  // ===========================================================================
  //
  // ⚠️⚠️ 为什么要有图。用户完全不懂 GNSS，而"重复性 95.2 厘米"这句话
  //     对他不构成任何画面。**一张"它一开始飘着一米、十几分钟后贴住
  //     最终位置"的图，一句话都不用解释**。
  //
  // ⚠️ 数据是**从产物直接读出来的**（`/api/figures` → `ginan/charts.py`），
  //     不经过模型。图上的每个点都来自那次解算的文件。
  //
  // ⚠️⚠️ **侧栏里不画图**。踩过：一开始把折线画在这个 232 px 宽的
  //     格子里 —— 挤成一条看不出任何东西的线。科学作图要的误差棒、
  //     置信带、缩放、hover 读数，那个尺寸里全都施展不开。
  //     现在侧栏只放结论 + 一个「查看图」按钮。

  function renderResults(d) {
    const body = el.resultBody;
    body.innerHTML = '';

    if (!d.has_result) {
      setPill(el.resultPill, '还没跑', '');
      const p = document.createElement('p');
      p.className = 'panel-foot';
      p.textContent = d.why_not || '还没有可以画的结果。';
      body.appendChild(p);
      return;
    }

    // 标签说结论，不说"有几张图"
    const first = d.series[0] || {};
    setPill(el.resultPill, first.quality === 'good' ? '好'
            : (first.quality === 'poor' ? '偏差大'
               : (first.quality === 'fair' ? '一般' : '待看')),
            { good: 'ok', fair: 'warn', poor: 'fail' }[first.quality] || '');

    for (const s of d.series) {
      const wrap = document.createElement('div');
      wrap.className = 'result-item';

      const head = document.createElement('div');
      head.className = 'result-station';
      // ⚠️ 站号和"前向解/平滑解"都要写出来 —— 一次运行会产出两份，
      //    不标出来用户会以为程序跑了两遍、或者把两份当成一份。
      const sol = s.solution === 'smoothed' ? '（平滑解）'
                : (s.solution === 'forward' ? '（前向解）' : '');
      head.textContent = (s.station || s.file) + sol;
      wrap.appendChild(head);

      if (s.why_not) {
        const p = document.createElement('p');
        p.className = 'result-why';
        p.textContent = s.why_not;
        wrap.appendChild(p);
      } else {
        // ⚠️ 侧栏里**不画图**（那个宽度画不出能读的东西），只给一个入口。
        //    图在全屏浮层里 —— 那里有误差棒、置信带、缩放和 hover 读数。
        const btn = document.createElement('button');
        btn.className = 'btn result-open';
        btn.textContent = '查看图（' + (s.figure_count || 0) + ' 张）';
        btn.disabled = !s.figure_count;
        btn.addEventListener('click', () => openFigures(s.file));
        wrap.appendChild(btn);
      }

      const line = document.createElement('p');
      line.className = 'result-headline';
      line.textContent = s.headline || '';
      wrap.appendChild(line);
      body.appendChild(wrap);
    }

    // ⚠️ **残差那一行单独一块**，挂在结果列表后面 —— 它和"这次算得怎么样"
    //    是两个问题：位置图回答"稳不稳"，残差回答"数据本身好不好"。
    addResidualRow(body);
  }

  // 残差：有就一个「看残差」按钮，没有就说清"要跑一次"。
  //
  // ⚠️ 说清它**会再跑一遍**（一天约 2 分钟）—— 用户点了才会发生，
  //    而不是我们偷偷跑。但也要说清它**不改变结果**（实测：trace 级别
  //    高低，.POS 逐字节相同），否则用户会以为"这下结果不一样了"。
  async function addResidualRow(body) {
    let d = { has_result: false };
    try {
      d = await api('/api/residuals');
    } catch (e) {
      return;                      // 读不到就不显示这块，不打扰
    }
    const wrap = document.createElement('div');
    wrap.className = 'result-item result-resid';

    const head = document.createElement('div');
    head.className = 'result-station';
    head.textContent = '观测质量（残差）';
    wrap.appendChild(head);

    if (d.has_result) {
      const p = document.createElement('p');
      p.className = 'result-headline';
      p.textContent = (d.verdict || {}).headline || '';
      wrap.appendChild(p);
      const parts = [];
      for (const [, r] of Object.entries(d.residuals || {})) {
        if (r.normalized_available && r.normalized_std != null) {
          parts.push(r.label + ' ' + r.n + ' 条，归一化残差标准差 '
                     + r.normalized_std.toFixed(2));
        } else {
          parts.push(r.label + ' ' + r.n + ' 条');
        }
      }
      if (parts.length) {
        const q = document.createElement('p');
        q.className = 'result-why';
        q.textContent = parts.join('；');
        wrap.appendChild(q);
      }
      const btn = document.createElement('button');
      btn.className = 'btn result-open';
      btn.textContent = '看残差图';
      btn.addEventListener('click', openResidualFigures);
      wrap.appendChild(btn);
    } else {
      const p = document.createElement('p');
      p.className = 'result-why';
      p.textContent = '还没看过观测质量。残差要重跑一遍才知道 —— '
        + '同一个站、同一天，只是把 Ginan 的 trace 打开。'
        + '解算结果不会变（实测逐字节相同），但会多花约 2 分钟、'
        + '留下一个几百 MB 的文件。想看就对我说「看看残差」。';
      wrap.appendChild(p);
    }
    body.appendChild(wrap);
  }

  async function loadResults() {
    try {
      renderResults(await api('/api/results'));
    } catch (e) {
      setPill(el.resultPill, '读不到', 'fail');
      el.resultBody.innerHTML = '';
      const p = document.createElement('p');
      p.className = 'panel-foot fail';
      p.textContent = '读不到结果：' + e.message;
      el.resultBody.appendChild(p);
    }
  }

  // ===========================================================================
  // 全屏图视图
  // ===========================================================================
  //
  // ⚠️ 图是 plotly 画的（`ginan/charts.py` 在服务端拼好 figure 的 JSON，
  //    这里 `Plotly.newPlot`）。**不能**用服务端的 `fig.to_html()`：
  //    那产出的是 `<div>` + `<script>`，而 script 插进 innerHTML 不执行。
  //
  // ⚠️ plotly.min.js 是**随包**的（`/static/plotly.min.js`，4.6 MB）——
  //    这个程序要在离网/内网环境下可用，用 CDN 等于图全都不出来。

  let figState = null;     // { byFile: {...}, file: "...", tab: "time_series" }

  function closeFigures() {
    el.figView.classList.add('hidden');
    // ⚠️ 关掉时**清空 plotly 的 DOM** —— 不然每开一次就在内存里留一张图
    //    （plotly 会挂监听、建 canvas），开十次就是十份。
    el.figBody.innerHTML = '';
    figState = null;
  }

  function renderFigureTabs() {
    const info = figState.byFile[figState.file];
    el.figTabs.innerHTML = '';
    for (const [key, label] of Object.entries(info.labels || {})) {
      if (!(key in (info.figures || {}))) continue;
      const b = document.createElement('button');
      b.className = 'fig-tab' + (key === figState.tab ? ' active' : '');
      b.textContent = label;
      b.addEventListener('click', () => {
        figState.tab = key;
        renderFigureTabs();
        drawFigure();
      });
      el.figTabs.appendChild(b);
    }
  }

  function drawFigure() {
    const info = figState.byFile[figState.file];
    const fig = (info.figures || {})[figState.tab];
    el.figBody.innerHTML = '';
    if (!fig) {
      el.figFoot.textContent = '这一张画不出来。';
      return;
    }
    const div = document.createElement('div');
    div.className = 'fig-plot';
    el.figBody.appendChild(div);
    // ⚠️ `responsive: true` —— 窗口改大小图跟着走，不用重画
    window.Plotly.newPlot(div, fig.data || [], fig.layout || {},
                          { responsive: true, displaylogo: false,
                            toImageButtonOptions: { format: 'png', scale: 2 } });
    el.figFoot.textContent = info.foot || '';
  }

  // ⚠️ 拆成"取数据"和"画"两半：残差图走的是**另一条接口**
  //    （`/api/residual_figures`），但画的过程一模一样 ——
  //    复制一份的话，改了一边漏一边是迟早的事。
  function showFigures(d, pick, defaultTab) {
    if (!d.ok) {
      el.figStation.textContent = '没有可画的图';
      el.figHeadline.textContent = d.why_not || '';
      el.figTabs.innerHTML = '';
      return;
    }
    {
      const byFile = {};
      for (const [name, info] of Object.entries(d.files)) {
        const sol = info.solution === 'smoothed' ? '（平滑解）'
                  : (info.solution === 'forward' ? '（前向解）' : '');
        byFile[name] = {
          figures: info.figures,
          labels: info.labels || {},
          foot: info.foot || '',
          station: (info.station || name) + sol,
          headline: info.headline || '',
        };
      }
      figState = { byFile, file: pick && byFile[pick] ? pick
                                 : Object.keys(byFile)[0],
                   tab: defaultTab };
      const info = byFile[figState.file];
      el.figStation.textContent = info.station;
      el.figHeadline.textContent = info.headline;
      if (!(figState.tab in info.figures)) {
        figState.tab = Object.keys(info.figures)[0];
      }
      renderFigureTabs();
      drawFigure();
    }
  }

  async function openFigures(fileName) {
    el.figView.classList.remove('hidden');
    el.figBody.innerHTML = '';
    el.figStation.textContent = '正在画…';
    el.figHeadline.textContent = '';
    try {
      showFigures(
        await api('/api/figures?file=' + encodeURIComponent(fileName || '')),
        fileName, 'time_series');
    } catch (e) {
      el.figStation.textContent = '画不出来';
      el.figHeadline.textContent = e.message;
    }
  }

  // 残差图 —— 同一个浮层，另一条数据来源
  async function openResidualFigures() {
    el.figView.classList.remove('hidden');
    el.figBody.innerHTML = '';
    el.figStation.textContent = '正在画…';
    el.figHeadline.textContent = '';
    try {
      showFigures(await api('/api/residual_figures'), '', 'time');
    } catch (e) {
      el.figStation.textContent = '画不出来';
      el.figHeadline.textContent = e.message;
    }
  }

  function exportFigurePng() {
    const div = el.figBody.querySelector('.fig-plot');
    if (!div || !window.Plotly) return;
    // ⚠️ 默认尺寸按**当前这张图**，scale=2 出的是两倍分辨率的图 ——
    //    投屏/放论文里都不糊。
    window.Plotly.downloadImage(div, {
      format: 'png', scale: 2,
      width: div.clientWidth || 1200, height: div.clientHeight || 600,
      filename: (figState && figState.file ? figState.file : 'result') + '-'
                + ((figState && figState.tab) || 'figure'),
    });
  }

  async function loadInbox() {
    try {
      const d = await api('/api/inbox');
      el.inboxPath.textContent = d.dir || '—';

      // ⚠️⚠️ 按钮看的是 `unclaimed`，**不是** `count`。
      //
      //     认领是**复制** —— 原件留在收件箱（用户不会丢东西，见 inbox.claim）。
      //     所以认领完之后收件箱里那些文件**还在**，只看 count 的话
      //     按钮会**一直在**，用户以为没成功，反复点。
      //
      //     （用户原话："程序认领了我的数据后，右下角收件箱就不要提示认领了"。）
      //
      // ⚠️ `d.unclaimed` 可能是 undefined（旧的后端、或者缓存里的旧页面）——
      //    退回 `known` 只是让按钮多显示一次，**不会误删或误判**。
      //    宁可多问一次，也不要因为读不到字段就把它当成"已经认领"。
      const unclaimed = (d.unclaimed === undefined) ? d.known : d.unclaimed;

      const btn = document.getElementById('btn-claim-inbox');
      if (unclaimed > 0) {
        btn.classList.remove('hidden');
        btn.textContent = `认领 ${unclaimed} 个文件`;
        setPill(document.getElementById('inbox-pill'),
                `${d.known}/${d.count} 认得出来`, 'ok');
      } else {
        btn.classList.add('hidden');
        if (d.known > 0) {
          // 认得出的都已经在数据目录里了 —— 这条是好消息，用 ok 色
          setPill(document.getElementById('inbox-pill'), '已就绪', 'ok');
        } else {
          setPill(document.getElementById('inbox-pill'),
                  d.count ? '认不出来' : '空的',
                  d.count ? 'warn' : '');
        }
      }

      // 列出认出来的文件；认不出的也显示，但说明为什么
      el.inboxList.innerHTML = '';
      for (const it of d.items || []) {
        const li = document.createElement('li');
        li.className = 'group-item';
        li.innerHTML =
          `<span class="check-dot ${it.known ? 'ok' : 'warn'}"></span>` +
          `<div><div class="check-name">${escapeHtml(it.name)}` +
          `<span class="group-sum">${escapeHtml(it.size)}</span></div>` +
          `<div class="check-detail">${
            it.claimed
              // ⚠️ 说"已在数据目录"，不说"已认领"——
              //    用户不关心我们内部管这个动作叫什么，
              //    他关心的是**文件到底在哪儿、能不能用了**。
              ? '已在数据目录里 · ' + escapeHtml(it.label)
              : it.known
                ? '识别为 ' + escapeHtml(it.label) +
                  (it.marker ? ` · 测站 ${escapeHtml(it.marker)}` : '')
                : '认不出来 —— ' + escapeHtml(it.evidence)
          }</div></div>`;
        el.inboxList.appendChild(li);
      }
    } catch (e) {
      setPill(document.getElementById('inbox-pill'), '读不到', 'fail');
    }
  }

  async function claimInbox() {
    const btn = document.getElementById('btn-claim-inbox');
    btn.disabled = true;
    const old = btn.textContent;
    btn.textContent = '搬运中…';
    try {
      const r = await api('/api/inbox/claim', {
        method: 'POST',
        body: JSON.stringify({ dry_run: false }),
      });
      addNotice(`收件箱：${r.summary}`, r.unknown.length ? 'warn' : '');
      for (const m of r.moved) {
        addNotice(
          m.renamed
            ? `　${m.from} → ${m.to}（按内容改名）`
            : `　${m.from} → 数据目录`
        );
      }
      for (const u of r.unknown) {
        addNotice(`　认不出来，留在原地：${u.file}`, 'warn');
      }
      // ⚠️⚠️ **"跳过的"必须也说出来。**
      //
      //    后端一直在返回 `skipped`（带**原因**：数据目录里已有同名文件
      //    但大小不同、解压失败、太大……），而前端**从来没渲染过** ——
      //    `grep skipped app.js` 是空的。
      //
      //    ⚠️ 后果：用户看到"看了 3 个、认领了 2 个"，**第三个不翼而飞**，
      //       而且完全不知道去哪问。**静默漏掉一个文件，比报错糟得多。**
      //
      //    ⚠️ 它和上面 `unknown` 那条是两件事：
      //       unknown = 我们**认不出**它是什么
      //       skipped = 认得出来，但**这次没搬**，有具体原因
      for (const s of r.skipped || []) {
        addNotice(`　没搬：${s.file} —— ${s.reason}`, 'warn');
      }
      // 太大而根本没被处理的（后端单独一类，原因只有一个：超过上限）
      for (const t of r.too_big || []) {
        addNotice(`　没搬：${t.file}（${t.size}）—— 超过 500 MB 上限`, 'warn');
      }
      loadInbox();
    } catch (e) {
      addNotice('认领失败：' + e.message, 'fail');
    } finally {
      btn.disabled = false;
      btn.textContent = old;
    }
  }

  document.getElementById('btn-claim-inbox')
    .addEventListener('click', claimInbox);

  // 在文件管理器里打开收件箱
  $('#btn-open-inbox').addEventListener('click', async () => {
    try {
      await api('/api/open-folder', {
        method: 'POST',
        body: JSON.stringify({ which: 'inbox' }),
      });
    } catch (e) {
      alert('打不开文件夹：' + e.message);
    }
  });

  // 起手卡片
  el.messages.addEventListener('click', (e) => {
    const card = e.target.closest('.card');
    if (card) send(card.dataset.say);
  });

  // ===========================================================================
  // 主题
  // ===========================================================================
  // ⚠️ 这里原来有一个深/浅色切换（写 localStorage、改 html[data-theme]）。
  //    **已经删掉**：整套界面只有深色一套。
  //
  //    理由是用户的那句话 —— "界面风格太大众化了" —— 而当时那两套主题
  //    用的是**同一份 Tailwind 默认色板**，只差明度。维护两套平庸的，
  //    不如把力气全花在一套上：**一个确定的样子，不要一堆选择。**
  //
  //    ⚠️ localStorage 里可能还留着旧的 'ginan-theme' 键，那是死数据，
  //       不读了 —— 但**不用去删它**：给它写清理代码，等于把已经删掉的
  //       东西又请回代码里。

  // ===========================================================================
  // 设置弹窗
  // ===========================================================================

  const modal = {
    root:    $('#settings-modal'),
    provider: $('#f-provider'),
    baseurl: $('#f-baseurl'),
    apikey:  $('#f-apikey'),
    signup:  $('#f-signup'),
    keyNote: $('#key-note'),
    model:   $('#f-model'),
    modelManual: $('#f-model-manual'),
    modelNote:   $('#model-note'),
    fetchModels: $('#btn-fetch-models'),
    providerNote: $('#provider-note'),
    proxyMode: $('#f-proxymode'),
    proxyHttp: $('#f-proxyhttp'),
    proxyManual: $('#proxy-manual'),
    status:  $('#save-status'),
    configPath: $('#config-path'),
    // 解算改正项
    corrList:   $('#corr-list'),
    corrIntro:  $('#corr-intro'),
    corrReset:  $('#corr-reset'),
    corrStatus: $('#corr-status'),
    // CDDIS 凭据
    cddisUser:   $('#f-cddis-user'),
    cddisPass:   $('#f-cddis-pass'),
    cddisSave:   $('#btn-save-cddis'),
    cddisStatus: $('#cddis-status'),
    cddisSignup: $('#cddis-signup'),
  };

  // 预设列表由服务端给（那里是唯一的内容源）
  let presets = [];

  // 上次从服务端读到的 CDDIS 用户名。
  //
  // ⚠️ 用来判断"用户是不是新填了东西" —— 见 hasUnsavedInput。
  //    用户名是**预填**在输入框里的，所以不能只看"框里有没有内容"，
  //    要看"有没有变化"。
  let savedCddisUser = '';

  function presetByKey(k) { return presets.find((p) => p.key === k); }

  async function openSettings() {
    modal.status.textContent = '';
    try {
      const s = await api('/api/settings');
      presets = s.presets || [];

      // 服务商下拉
      modal.provider.innerHTML = '';
      for (const p of presets) {
        const o = document.createElement('option');
        o.value = p.key;
        o.textContent = p.label;
        modal.provider.appendChild(o);
      }
      modal.provider.value = s.provider || 'deepseek';

      modal.baseurl.value = s.base_url || '';
      modal.apikey.value = '';           // ⚠️ 永不回显完整 Key
      modal.apikey.placeholder = s.has_key
        ? `已保存：${s.api_key_masked}（留空表示不改）`
        : '粘贴你的密钥';

      modal.modelManual.value = s.model || '';
      modal.proxyMode.value = s.proxy_mode || 'auto';
      modal.proxyHttp.value = s.proxy_https || s.proxy_http || '';
      modal.configPath.textContent = s.config_path
        ? `设置保存在：${s.config_path}` : '';

      fillModelSelect(s.known_models || [], s.model || '');
      // ⚠️ 覆盖值来自**设置**（不是工作区）—— 设置面板里改的就是它。
      //    工作区那份是「这一跑用的」，保存时才同步过去。
      corrOverrides = Object.assign({}, s.corrections || {});
      loadCorrections();
      onProviderChange();
      onProxyModeChange();
      loadCredentials();

      modal.root.classList.remove('hidden');
      modal.apikey.focus();
    } catch (e) {
      console.error(e);
      alert('读不到设置：' + e.message);
    }
  }

  function closeSettings() { modal.root.classList.add('hidden'); }

  // ===========================================================================
  // 解算改正项 —— 服务端给清单，这里只负责画和收集
  // ===========================================================================
  //
  // ⚠️ 两层状态，别混：
  //      生效值（服务端算好的 `on`）    ← 画勾用
  //      覆盖值（用户点过的那些）        ← 提交用
  //    提交的**只有覆盖值**：档位换掉时其余项才会跟着变。
  //    提交整份的话，用户在界面上碰过一项之后，档位就再也换不动了
  //    （见 `settings.corrections` 里那段）。
  //
  // ⚠️ **缺文件的项必须显眼**：Ginan 那边"开关开着、文件却不在"
  //    = 静默少一项改正，不报错、退出码还是 0。
  //    界面上不说，用户就永远不知道。
  let corrOverrides = {};

  async function loadCorrections() {
    modal.corrList.innerHTML = '<div class="skeleton-row"></div>';
    modal.corrStatus.textContent = '';
    try {
      renderCorrections(await api('/api/corrections'));
    } catch (e) {
      modal.corrList.innerHTML = '';
      modal.corrStatus.textContent = '读不到改正项：' + e.message;
    }
  }

  function renderCorrections(d) {
    modal.corrList.innerHTML = '';
    for (const it of d.items) {
      const row = document.createElement('div');
      row.className = 'corr-item';

      const head = document.createElement('label');
      head.className = 'corr-head';
      const cb = document.createElement('input');
      cb.type = 'checkbox';
      cb.checked = !!it.on;
      cb.addEventListener('change', () => {
        corrOverrides[it.key] = cb.checked;
        noteCorrChanged();
      });
      const name = document.createElement('span');
      name.className = 'corr-name';
      name.textContent = it.label;
      head.appendChild(cb);
      head.appendChild(name);
      if (it.overridden) {
        // ⚠️ 要说清"这一项是你设的，不是档位给的" —— 否则改过一项之后，
        //    用户分不清剩下 8 项是谁定的。
        const tag = document.createElement('span');
        tag.className = 'corr-tag';
        tag.textContent = '你设的';
        head.appendChild(tag);
      }
      row.appendChild(head);

      const what = document.createElement('p');
      what.className = 'corr-what';
      what.textContent = it.what;
      row.appendChild(what);

      const eff = document.createElement('p');
      eff.className = 'corr-effect' + (it.measured ? '' : ' unknown');
      // ⚠️ **原样显示**（`corrections.py` 里每条 effect 自己带的措辞就是完整的）
      //    —— 这里再前缀一次会变成「实测：实测：解移动…」（截图里看到过）。
      //    "量过没量过"由服务端给的那个 measured 决定样式，不由文案猜。
      eff.textContent = it.effect;
      row.appendChild(eff);

      for (const f of (it.file_info || [])) {
        const line = document.createElement('p');
        line.className = 'corr-file' + (f.have ? '' : ' missing');
        line.textContent = f.have
          ? `需要的文件：${f.name}（${f.mb} MB，已在本地）`
          : `⚠️ 需要的文件还没下到：${f.name} —— 开着它也算不出来`
            + '（Ginan 不报错，结果会静默少这一项改正）';
        row.appendChild(line);
      }
      modal.corrList.appendChild(row);
    }
    modal.corrIntro.dataset.profile = d.profile_label || '';
  }

  function resetCorrections() {
    // ⚠️ 清空**覆盖值**（不是"把 9 个勾都改成 false"）——
    //    覆盖值空 = 完全按档位走，那才是"默认"。
    corrOverrides = {};
    loadCorrections();
    noteCorrChanged();
  }

  function noteCorrChanged() {
    const n = Object.keys(corrOverrides).length;
    modal.corrStatus.textContent = n
      ? `已改 ${n} 项（保存后生效）—— 其余项仍跟着档位走`
      : '全部按档位默认。';
  }

  function fillModelSelect(models, current) {
    modal.model.innerHTML = '';
    const blank = document.createElement('option');
    blank.value = '';
    blank.textContent = models.length ? '（从下面选一个）' : '（点右边「获取列表」）';
    modal.model.appendChild(blank);

    for (const m of models) {
      const o = document.createElement('option');
      o.value = m; o.textContent = m;
      modal.model.appendChild(o);
    }
    if (current && models.includes(current)) modal.model.value = current;
  }

  function onProviderChange() {
    const p = presetByKey(modal.provider.value);
    if (!p) return;
    if (p.base_url) modal.baseurl.value = p.base_url;
    modal.providerNote.textContent = p.note || '';

    // 注册链接只在预设提供时显示
    if (p.signup_url) {
      modal.signup.href = p.signup_url;
      modal.signup.classList.remove('hidden');
    } else {
      modal.signup.classList.add('hidden');
    }
  }

  function onProxyModeChange() {
    modal.proxyManual.classList.toggle('hidden', modal.proxyMode.value !== 'manual');
  }

  async function fetchModels() {
    const baseurl = modal.baseurl.value.trim();
    const key = modal.apikey.value.trim();

    modal.fetchModels.disabled = true;
    modal.modelNote.textContent = '正在向服务商查询…';
    try {
      const data = await api('/api/models', {
        method: 'POST',
        body: JSON.stringify({ base_url: baseurl, api_key: key }),
      });
      if (!data.count) {
        // ⚠️ 查不到不是错误，是**信息**。服务商可能不提供 /models 接口。
        //    这时应该引导用户手动填，而不是说"失败了"。
        modal.modelNote.textContent =
          '这个服务商没有返回模型列表 —— 直接在下面手动填模型名即可。';
        return;
      }
      fillModelSelect(data.models, modal.modelManual.value);
      modal.modelNote.textContent =
        `查到 ${data.count} 个模型。选一个，或者自己填。`;
    } catch (e) {
      modal.modelNote.textContent = `查询失败：${e.message}`;
    } finally {
      modal.fetchModels.disabled = false;
    }
  }

  async function saveSettings() {
    const chosen = modal.model.value || modal.modelManual.value.trim();
    modal.status.textContent = '保存中…';
    try {
      const res = await api('/api/settings', {
        method: 'POST',
        body: JSON.stringify({
          provider: modal.provider.value,
          base_url: modal.baseurl.value.trim(),
          api_key: modal.apikey.value.trim() || undefined,  // 空 = 不改
          model: chosen,
          proxy_mode: modal.proxyMode.value,
          proxy_http: modal.proxyHttp.value.trim(),
          proxy_https: modal.proxyHttp.value.trim(),
          // ⚠️ 只提交**用户改过的**那几项（稀疏覆盖）——
          //    提交整份会让档位从此换不动（见 settings.corrections）。
          corrections: corrOverrides,
        }),
      });
      modal.status.textContent = '已保存';

      // ⚠️ 主「保存」**只保存上面那些设置**，不含 CDDIS 凭据
      //    （那个有自己的「保存并验证」按钮）。
      //
      //    所以如果 CDDIS 那栏填了东西还没保存，直接关窗会**把它丢掉**。
      //    先问一句。
      const cddisPending = Boolean(
        modal.cddisPass.value ||
        (modal.cddisUser.value.trim() && modal.cddisUser.value.trim() !== savedCddisUser)
      );

      if (cddisPending) {
        modal.status.textContent =
          '已保存。⚠️ CDDIS 那栏还没保存 —— 记得点「保存并验证」';
        // 不自动关窗，让用户看见提示
      } else {
        setTimeout(closeSettings, 400);
      }

      // 保存后环境里的"密钥已配置"应该变成绿的
      loadEnvironment();
    } catch (e) {
      modal.status.textContent = '保存失败：' + e.message;
    }
  }

  // ===========================================================================
  // CDDIS 凭据
  // ===========================================================================

  async function loadCredentials() {
    try {
      const c = await api('/api/credentials');
      savedCddisUser = c.user || '';
      modal.cddisUser.value = savedCddisUser;
      modal.cddisPass.value = '';
      modal.cddisPass.placeholder = c.configured ? '留空表示保持不变' : '密码';
      modal.cddisSignup.href = c.signup_url;
      modal.cddisStatus.className = 'field-note';
      modal.cddisStatus.textContent = c.configured
        ? `已配置（${c.user}）。存在 ${c.paths[0]}`
        : '还没配置。侧栏提示 CDDIS 需要登录时才需要它。';
    } catch (e) {
      modal.cddisStatus.className = 'field-note fail';
      modal.cddisStatus.textContent = '读不到凭据状态：' + e.message;
    }
  }

  async function saveCredentials() {
    const user = modal.cddisUser.value.trim();
    const pass = modal.cddisPass.value;

    if (!user || !pass) {
      modal.cddisStatus.className = 'field-note fail';
      modal.cddisStatus.textContent = '用户名和密码都要填。';
      return;
    }

    // ⚠️ 按钮要禁用 + 文字变化。
    //    验证要发一个真实的网络请求，可能要几秒 ——
    //    不给反馈的话用户会重复点。
    modal.cddisSave.disabled = true;
    modal.cddisStatus.className = 'field-note';
    modal.cddisStatus.textContent = '正在验证（要联网，约几秒）…';

    try {
      const r = await api('/api/credentials', {
        method: 'POST',
        body: JSON.stringify({ user, password: pass }),
      });

      if (r.ok) {
        modal.cddisStatus.className = 'field-note ok';
        modal.cddisStatus.textContent =
          `✓ ${r.detail}　凭据已保存到 ${r.written[0]}`;
        modal.cddisPass.value = '';
        // ⚠️ 更新"已保存的用户名" —— 否则 hasUnsavedInput 会一直
        //    认为用户改了用户名，导致关窗时多问一句废话
        savedCddisUser = user;
        // 验证通过后重新探测数据源 —— 刚才那个"需要登录"应该变成绿的
        loadNetwork();
      } else {
        // ⚠️ 失败时要说清"怎么办"，不只是"错了"。
        //    密码错、域名错、网络不通，用户的下一步动作完全不同。
        modal.cddisStatus.className = 'field-note fail';
        modal.cddisStatus.textContent =
          `✗ ${r.detail}` + (r.suggestion ? `\n${r.suggestion}` : '');
      }
    } catch (e) {
      modal.cddisStatus.className = 'field-note fail';
      modal.cddisStatus.textContent = '保存失败：' + e.message;
    } finally {
      modal.cddisSave.disabled = false;
    }
  }

  modal.cddisSave.addEventListener('click', saveCredentials);

  $('#btn-settings').addEventListener('click', openSettings);
  $('#settings-close').addEventListener('click', closeSettings);
  $('#settings-cancel').addEventListener('click', closeSettings);
  $('#settings-save').addEventListener('click', saveSettings);
  modal.corrReset.addEventListener('click', resetCorrections);
  modal.provider.addEventListener('change', onProviderChange);
  modal.proxyMode.addEventListener('change', onProxyModeChange);
  modal.fetchModels.addEventListener('click', fetchModels);
  modal.model.addEventListener('change', () => {
    if (modal.model.value) modal.modelManual.value = modal.model.value;
  });

  // ===========================================================================
  // 关闭设置弹窗
  // ===========================================================================
  //
  // ⚠️⚠️ 这里**故意不支持"点背景关闭"**，虽然那是个很常见的做法。
  //
  //    原因：这个弹窗里是**表单**（API 密钥、CDDIS 用户名密码），
  //    用户可能已经输了一半。而弹窗很长（有滚动条），
  //    点边缘的空白、或者想拖动滚动条时手滑 —— 一下就关了，
  //    **输入的东西全没了，而且没有任何提示。**
  //
  //    用户的原话是"这个窗口不知道什么时候会自己关掉" ——
  //    他花了力气排查出这句话，说明这事真的发生且很难理解。
  //
  //    > 一个会**静默丢弃用户输入**的交互，再常见也不该照抄。
  //
  //    Esc 保留（那是明确的"我放弃"信号），但**有未保存内容时会先问一句**。

  /** 表单里有没有用户输入了但没保存的东西。 */
  function hasUnsavedInput() {
    // ⚠️ 只看**会丢的关键字段**：密钥和凭据。
    //    其他字段（服务商、代理模式）丢了重选一下就好，
    //    为了它们每次都弹确认框会很烦。
    return Boolean(
      modal.apikey.value.trim() ||
      modal.cddisPass.value ||
      (modal.cddisUser.value.trim() && !savedCddisUser)
    );
  }

  function maybeClose() {
    if (!hasUnsavedInput() || confirm('表单里有还没保存的内容，关掉就丢了。确定要关闭吗？')) {
      closeSettings();
    }
  }

  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape' && !modal.root.classList.contains('hidden')) maybeClose();
  });

  // ===========================================================================
  // 启动
  // ===========================================================================

  // 图视图的两个按钮 + Esc
  // ⚠️ Esc 也要能关：全屏浮层挡住整个界面，鼠标不在按钮上时
  //    用户的第一个反应就是按 Esc —— 按了没反应会以为卡住了。
  el.figClose.addEventListener('click', closeFigures);
  el.figExport.addEventListener('click', exportFigurePng);
  document.addEventListener('keydown', (ev) => {
    if (ev.key === 'Escape' && !el.figView.classList.contains('hidden')) {
      closeFigures();
    }
  });

  // 页头的 UTC / GPS 周读数。算法在 clock.js 里（纯函数，可单独测），
  // 这里只是把它挂上去。
  window.startClock($('#clock'));

  connect();
  loadEnvironment();
  loadNetwork();
  loadResults();
  // ⚠️⚠️ 这一行**必须在这里**，不能只在"新任务"那条路上调。
  //     踩过：`loadResults()` 一开始只加进了「新任务」的重置路径
  //     （`refreshAfterReset` 里那个 4 空格缩进的同名三行），
  //     而**启动序列这份**（2 空格缩进）没加 —— 于是刷新页面后结果面板
  //     永远是空的，直到用户点一次新任务。
  //     ⚠️ 自检和 curl 都看不出这件事（接口是对的、类名也都有样式），
  //        是**截图**抓到的。
  loadInbox();
  // ⚠️ 放在几个 loader **后面**：它只在上一次是"点新任务"触发的刷新时
  //    才有内容，而且它要出现在对话区**最下面**（`addNotice` 会滚到底）——
  //    排在前面的话，用户得往上翻才看得到。
  //
  // ⚠️ 查过了：下面那个 `renderHistory()` 会**清掉**我这几条吗？
  //    不会 —— 它没有历史时**直接 return**（`if (!msgs.length) return`），
  //    有历史时是 `appendChild` 而不是 `innerHTML = ''`。
  //    而"点过新任务"必然没有历史（历史刚被清掉），所以两条不会打架。
  showNewTaskReport();
  el.input.focus();

  // ⚠️⚠️ **把上次的对话画回来。**
  //
  //     用户的原话："启动软件应该显示上次的对话，除非我点新任务。"
  //
  //     在这之前历史**一直存着**（`sessions/<id>/history.jsonl`），
  //     但界面从不渲染它 —— 一刷新就是一片空白，
  //     于是用户以为"它忘了"，其实一个字都没丢。
  //
  //     ⚠️ **存下来了却不显示，和没存是一样的。**
  renderHistory();

  // ⚠️ 刷新页面时，服务端可能**还在等一个回答** ——
  //    工具是阻塞的，它不知道浏览器刷新了。
  //    不恢复的话，用户看到的是一个"啥也没发生"的界面，
  //    而那一轮其实卡在等他回答上，**永远也不会自己继续**。
  restorePendingAsk();

  // 首次运行：环境检查说"密钥没配"时，自动弹设置引导用户去填。
  //
  // ⚠️ 只在**真的没配**时才弹，而且只弹一次（用 localStorage 记）。
  //    每次打开都弹是骚扰 —— 用户可能就是想先看看界面。
  setTimeout(async () => {
    try {
      if (localStorage.getItem('ginan-settings-prompted')) return;
      const s = await api('/api/settings');
      if (!s.has_key) {
        localStorage.setItem('ginan-settings-prompted', '1');
        openSettings();
      }
    } catch { /* 忽略 */ }
  }, 900);
})();

