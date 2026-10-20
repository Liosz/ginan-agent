"""工具向用户提问、并**阻塞等待回答**的通道。

============================ 它解决的问题 ============================
原来的流程是：模型在正文里写一句"你确认的话我就开始"，然后**停下来**。
而界面上只有一行字，没有任何可点的东西 ——
用户不知道该回什么、也不知道程序是在等他还是在卡着。

现在模型可以调用 `ask_user` 工具，把选项**结构化**地交出来，
前端画成按钮，用户点一下就行。

============================ 跨线程的会合点 ============================
这里有两个线程，各自在做自己的事：

    工作线程    agent.run() 正卡在工具函数里 → 工具调 ask() → **阻塞**
    HTTP 线程   收到 POST /api/answer → 调 answer() → 唤醒上面那个

⚠️ 所以核心是"一个人等、另一个人投递"。

============================ ⚠️ 为什么用轮询而不是等一个 Event ============================
Python 里**没法同时等一个 Event 和一条 Queue**（没有 select 那种原语）。

而我们有两个必须同时等的条件：
  · 用户回答了（队列里有东西）
  · 用户点了"停止"（cancel 标志被设）

所以只能轮询：`queue.get(timeout=0.2)` 拿不到就回头看一眼 cancel 标志。

⚠️ 0.2 秒对用户是察觉不到的，而代价是几乎为零。
   这和 `probe_all` / `runner` 里观察取消的方式是**同一套** ——
   保持一致比"用更聪明的方式"重要。

============================ ⚠️ 每条退出路径都必须注销 ============================
问完一个问题，不管是怎么结束的（答了 / 取消了 / 超时了），
都要把自己从等待表里摘掉。

漏了的话，那张表会一直涨；更糟的是：**一个泄漏的等待者会让
后来的一次 answer() 打到已经死掉的问题上** —— 返回"成功"，
而实际上没人收到。这种 bug 只在长时间运行时才出现。
"""

from __future__ import annotations

import queue
import threading
from typing import Any, Callable

# 等用户回答的上限。
#
# ⚠️ 为什么要有上限：用户可能直接关掉浏览器标签页，那时**没有任何东西**
#    会来投递答案。没有上限的话，工作线程会永远挂在那里，
#    而 Hub 认为"这一轮还在跑"，用户重新打开页面会发现啥也干不了。
#
# ⚠️ 600 秒是**故意设得长**的：用户在认真读选项、去查天线型号，
#    这些都要时间。我们宁可等久一点，也不要在他思考到一半时
#    把问题撤掉 —— 那个体验比"卡住"更让人恼火。
DEFAULT_TIMEOUT = 600.0


class QuestionBroker:
    """提问 → 等待 → 收答案。线程安全。"""

    def __init__(self, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._waiters: dict[str, queue.Queue] = {}
        self._lock = threading.Lock()
        self._timeout = timeout

        # 最近一次提问 —— 给"刷新页面后还能看到那个问题"用
        self._last: dict[str, Any] | None = None

    # -------------------------------------------------------------------------
    # 工具侧：提问并等待
    # -------------------------------------------------------------------------

    def ask(
        self,
        *,
        call_id: str,
        question: str,
        options: list[dict],
        allow_text: bool,
        cancel: threading.Event,
        emit: Callable[[Any], None],
        timeout: float | None = None,
    ) -> str | None:
        """把问题发出去，然后**阻塞**等回答。

        返回：
          · 用户选的/写的字符串
          · `None` —— 用户中止了这一轮（调用方该说"用户中止了"）
          · 超时的提示文字（不是 None —— 那和"中止"是两回事）
        """
        box: queue.Queue[str] = queue.Queue(maxsize=1)
        with self._lock:
            self._waiters[call_id] = box
            self._last = {
                "call_id": call_id,
                "question": question,
                "options": options,
                "allow_text": allow_text,
            }

        # ⚠️ 先登记再发事件。反过来的话，用户可能在登记之前就点了
        #    （事件一推出去，界面马上就画出了按钮）——
        #    那一票就丢在空处了，而用户会觉得"我明明点了"。
        emit({
            "type": "ask_user",
            "call_id": call_id,
            "question": question,
            "options": options,
            "allow_text": allow_text,
        })

        limit = self._timeout if timeout is None else timeout
        waited = 0.0
        try:
            while True:
                try:
                    got = box.get(timeout=0.2)
                except queue.Empty:
                    waited += 0.2
                else:
                    # ⚠️ 空串表示"被 clear() 叫醒了"，不是用户真的答了个空。
                    #    见 clear() 的说明 —— 统一当成"没答案"。
                    return got or None

                # ⚠️ 取消要**优先于**超时判断 —— 用户点了停止就该立刻停，
                #    而不是再等满十分钟。
                if cancel.is_set():
                    return None
                if waited >= limit:
                    return (
                        f"（用户一直没有回答，等了 {int(limit)} 秒。"
                        "请先按最合理的默认值继续，并把你的假设告诉用户。）"
                    )
        finally:
            # ⚠️⚠️ 每条路都要走到这里：答了、取消了、超时了、抛异常了。
            #     漏掉的话等待表会一直涨，而且后来的 answer() 会打到
            #     已经死掉的问题上 —— 返回"成功"而没人收到。
            with self._lock:
                self._waiters.pop(call_id, None)
                if self._last and self._last.get("call_id") == call_id:
                    self._last = None

    # -------------------------------------------------------------------------
    # HTTP 侧：投递答案
    # -------------------------------------------------------------------------

    def answer(self, call_id: str, value: str) -> bool:
        """把用户的回答投给正在等的那个工具。

        返回 False 表示"没人在等这个问题" —— 界面该告诉用户
        "这个问题已经过期了"，而不是假装成功。
        """
        with self._lock:
            box = self._waiters.get(call_id)
        if box is None:
            return False
        try:
            box.put_nowait(value)
        except queue.Full:
            # 已经有人答过了 —— 不当成错误，第二次点击忽略即可
            return False
        return True

    def pending(self) -> dict[str, Any] | None:
        """当前有没有正在等回答的问题。刷新页面后用它恢复界面。"""
        with self._lock:
            return dict(self._last) if self._last else None

    def clear(self) -> None:
        """把所有等待者叫醒（取消 / 新任务时用）。

        ⚠️ 每个等待者塞一个空串 —— 不能只是"从表里删掉"。
           删掉的话等待者还阻塞在队列上，而 `answer()` 已经找不到它了，
           于是它会一直等到超时（默认十分钟）。**叫醒才算数。**
        """
        with self._lock:
            boxes = list(self._waiters.values())
            self._waiters.clear()
            self._last = None
        for box in boxes:
            try:
                box.put_nowait("")
            except queue.Full:
                pass
