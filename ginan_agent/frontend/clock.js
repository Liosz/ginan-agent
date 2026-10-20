/* =============================================================================
   页头的读数 —— UTC 时刻 + GPS 周

   ============================ 为什么页头要放这个 ============================
   这不是装饰。**整个程序内部就是用 GPS 周说话的**：

       catalog.BKG_PRODUCTS_FROM_WEEK      = 2238   （BKG 从哪一周开始有产品）
       catalog.CDDIS_OLD_NAMING_BEFORE_WEEK = 2237   （IGS 改长命名的那一周）
       sources._beidou_week_dow()                    （iGMAS 用的是**北斗周**）

   这些东西会出现在给用户的取数计划、缓存目录名和报错里。
   页头把「现在是多少周」摆在那儿，用户看到「周 2238」时就有个参照 ——
   不会觉得那是随机数字。

   ⚠️ 本文件是**纯函数 + 一个启动器**，和 markdown.js 一样挂在 window 上、
      不碰 DOM 之外的东西 —— 这样 node 能直接把它加载进来测（见
      scripts/check_clock.mjs）。界面逻辑留在 app.js 里。

   ⚠️⚠️ 这里算出来的周号**必须和 ginan/catalog.py:gps_week() 一致**。
      两边是各自独立实现的（Python 用 date 相减，JS 用毫秒相减），
      所以守卫用**同一组契约值**把它们钉在一起 —— 见
      scripts/check_clock.mjs 和 check_ui_text.py 的 ⑬。
   ============================================================================= */

(() => {
  'use strict';

  // GPS 纪元：1980-01-06 00:00:00 UTC。
  // ⚠️ 和 ginan/catalog.py 的 _GPS_EPOCH 是同一个日子，改一处必须改两处。
  const GPS_EPOCH_MS = Date.UTC(1980, 0, 6);
  const DAY_MS = 86400000;

  const DOW_NAMES = ['周日', '周一', '周二', '周三', '周四', '周五', '周六'];

  // ===========================================================================
  // 纯函数
  // ===========================================================================

  /**
   * 从纪元起过了多少个**整天**。
   *
   * ⚠️ 用 UTC 的日期，不是本地的。
   *    GPS 周是 UTC 概念 —— 用本地日期的话，东八区用户晚上 8 点之后
   *    就会看到周号比实际早一天。这个偏差只在特定时段出现，最难查。
   */
  function daysSinceEpoch(d) {
    // ⚠️ 向下取整而不是四舍五入：一天之内任何时刻都属于**同一个** GPS 日，
    //    从 00:00:00 到 23:59:59 必须落在同一格。
    return Math.floor((d.getTime() - GPS_EPOCH_MS) / DAY_MS);
  }

  /** GPS 周号。和 catalog.gps_week() 是同一个定义。 */
  function gpsWeek(d) {
    return Math.floor(daysSinceEpoch(d) / 7);
  }

  /** 周内的第几天，0 = 周日（GPS 的约定，不是 ISO 的周一）。 */
  function gpsDayOfWeek(d) {
    // ⚠️ 两次取模：JS 的 % 对负数返回负数，而纪元之前的日期会算出负值。
    //    实际用不到（没人会往前算到 1980 年），但一个会返回 -3 的函数
    //    比一个返回 4 的函数难查得多 —— 所以照规矩补上。
    return ((daysSinceEpoch(d) % 7) + 7) % 7;
  }

  function pad2(n) {
    return String(n).padStart(2, '0');
  }

  /** `2026-09-20` */
  function formatDate(d) {
    return d.getUTCFullYear() + '-' + pad2(d.getUTCMonth() + 1) + '-' + pad2(d.getUTCDate());
  }

  /** `14:32:07` */
  function formatUtc(d) {
    return pad2(d.getUTCHours()) + ':' + pad2(d.getUTCMinutes()) + ':' + pad2(d.getUTCSeconds());
  }

  // ===========================================================================
  // 启动器
  // ===========================================================================

  /**
   * 让页头那块读数走起来。
   *
   * 要填的节点用 `data-clock="date|time|week"` 标出来 —— 缺哪个就跳过哪个，
   * 不报错（页头不该因为少个 span 就整块不显示）。
   */
  function startClock(root) {
    if (!root) return;

    const node = (key) => root.querySelector('[data-clock="' + key + '"]');
    const nDate = node('date');
    const nTime = node('time');
    const nWeek = node('week');

    let lastTime = '';

    function render() {
      const now = new Date();
      const t = formatUtc(now);

      // ⚠️ 秒没变就别写 DOM。
      //    定时器是 1 秒一次，但浏览器在后台标签页里会把它压到一分钟一次，
      //    回到前台时可能连跳好几秒 —— 这个判断让"写"和"走"解耦。
      if (t !== lastTime) {
        lastTime = t;
        if (nTime) nTime.textContent = t;
        if (nDate) nDate.textContent = formatDate(now);
      }

      if (nWeek) {
        const w = gpsWeek(now);
        const dow = gpsDayOfWeek(now);
        // `周.日` 是 GNSS 界的写法（2437.0 = 第 2437 周的第 0 天）。
        nWeek.textContent = w + '.' + dow;
        nWeek.title = 'GPS 周 ' + w + ' · ' + DOW_NAMES[dow] + '\n每天 0 点（UTC）换周，换的是这个数字';
      }
    }

    render();
    setInterval(render, 1000);
  }

  window.gpsWeek = gpsWeek;
  window.gpsDayOfWeek = gpsDayOfWeek;
  window.formatUtc = formatUtc;
  window.formatDate = formatDate;
  window.startClock = startClock;
})();
