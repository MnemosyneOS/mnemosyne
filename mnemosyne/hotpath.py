# -*- coding: utf-8 -*-
"""热路径缓冲与异步巩固（Hot Path Buffer）。


## 问题

8.0.0 的 ``retain()`` 是**同步全量**的：清洗 → 抽实体 → 算置信 → 写盘 →
建图边 → 冲突检测，全部在调用线程里跑完才返回。单条写入约 1ms 尚可接受，
但一旦进入「每轮对话写 3–5 条」的节奏，延迟直接叠加到首字延迟上。

通行做法的结论很明确：**热路径只做轻量缓冲，重活全部下沉到异步巩固**。
OpenAI 的 Dreaming 就是这个思路的极致——后台进程从大量对话中合成记忆状态。

## 两条路径的分工

| | 热路径（本模块 submit） | 冷路径（本模块 worker） |
|---|---|---|
| 时机 | 对话进行中 | 会话结束 / 空闲 / 定时 |
| 做什么 | 生成 id、入队、立即返回 | 落盘、图边抽取、冲突检测、触发巩固 |
| 延迟影响 | 微秒级（一次 append） | 不影响用户感知 |

## 默认不改变既有行为

``retain()`` 默认仍走同步路径。要启用异步写入必须显式传
``async_write=True``。理由：异步写入会让「写完立刻 recall」出现短暂不一致，
这是语义变化，不能默认打开——**升级不能悄悄改变调用方看到的行为**。

启用后由三个机制兜住一致性：

1. ``submit()`` 在入队前就把 id 算好并返回，调用方拿到的 id 立即可用；
2. ``recall()`` 入口可调用 ``flush()``（默认在 ``Brain.recall`` 里已接），
   保证「读到自己刚写的」；
3. ``close()`` / 进程退出时 ``flush(timeout)``，不丢队列。

## 队列满了怎么办

不阻塞、不丢数据：``submit()`` 在队列满时**退化为同步写**（backpressure）。
这比「阻塞调用方」和「静默丢弃」都好——前者拖慢对话，后者丢记忆。
"""

from __future__ import annotations

import queue
import threading
import time
from typing import Any, Callable, Dict, List, Optional

__all__ = ["HotPathBuffer", "BufferStats"]


class BufferStats:
    """可变的计数器集合（简单起见用 dict 子类）。"""

    def __init__(self) -> None:
        self.data: Dict[str, Any] = {
            "submitted": 0,
            "written": 0,
            "degraded_sync": 0,
            "failed": 0,
            "flushes": 0,
            "consolidations": 0,
            "last_error": "",
            "last_flush_ms": 0.0,
        }

    def inc(self, key: str, n: int = 1) -> None:
        self.data[key] = int(self.data.get(key) or 0) + n

    def set(self, key: str, value: Any) -> None:
        self.data[key] = value

    def snapshot(self) -> Dict[str, Any]:
        return dict(self.data)


class HotPathBuffer:
    """写入缓冲 + 后台 worker。

    参数
    ----
    brain:
        ``MemoryBrain`` 实例。只用到 ``store.append`` / ``store.append_batch``
        与可选的 ``_detect_conflicts_at_write``。
    max_queue:
        队列容量。满了以后 ``submit()`` 退化为同步写，不阻塞调用方。
    batch_size / flush_interval:
        批量落盘的阈值。攒够 ``batch_size`` 条或等满 ``flush_interval`` 秒就落一次。
    post_write:
        可选的「冷路径重活」回调，签名 ``(record) -> None``。异常会被吞掉并计数
        —— 后台任务失败不能影响已经成功的写入。
    consolidate_every:
        每写入 N 条触发一次巩固（若提供了 ``consolidator``）。
    """

    def __init__(self, brain: Any, max_queue: int = 2000,
                 batch_size: int = 32, flush_interval: float = 0.5,
                 post_write: Optional[Callable[[Dict[str, Any]], None]] = None,
                 consolidate_every: int = 200,
                 consolidator: Optional[Any] = None):
        self.brain = brain
        self.max_queue = max(8, int(max_queue))
        self.batch_size = max(1, int(batch_size))
        self.flush_interval = max(0.05, float(flush_interval))
        self.post_write = post_write
        self.consolidate_every = max(1, int(consolidate_every))
        self.consolidator = consolidator

        self.stats = BufferStats()
        self._q: "queue.Queue[Optional[Dict[str, Any]]]" = queue.Queue(maxsize=self.max_queue)
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._started = False
        self._pending_ids: Dict[str, float] = {}
        self._pending_lock = threading.Lock()

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------

    def start(self) -> "HotPathBuffer":
        """启动后台 worker（幂等）。"""
        with self._lock:
            if self._started:
                return self
            self._started = True
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="mnemo-hotpath",
                                            daemon=True)
            self._thread.start()
        return self

    def close(self, timeout: float = 10.0) -> Dict[str, Any]:
        """停止并落盘剩余条目。返回统计快照。"""
        with self._lock:
            if not self._started:
                return self.stats.snapshot()
            self._started = False
        self.flush(timeout=timeout)
        self._stop.set()
        try:
            self._q.put_nowait(None)  # 唤醒阻塞中的 get
        except queue.Full:
            pass
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None
        return self.stats.snapshot()

    # ------------------------------------------------------------------
    # 写入
    # ------------------------------------------------------------------

    def submit(self, record: Dict[str, Any]) -> str:
        """入队一条记录，立即返回 id。

        队列满 → 同步写（backpressure），保证不丢数据也不阻塞。
        """
        self.start()
        mid = str(record.get("id") or "")
        self.stats.inc("submitted")
        if mid:
            with self._pending_lock:
                self._pending_ids[mid] = time.time()
        try:
            self._q.put_nowait(record)
        except queue.Full:
            # 退化为同步写：宁可慢，也不丢
            self.stats.inc("degraded_sync")
            self._write_one(record)
        return mid

    def submit_batch(self, records: List[Dict[str, Any]]) -> List[str]:
        return [self.submit(r) for r in records]

    def _write_one(self, record: Dict[str, Any]) -> bool:
        try:
            self.brain.store.append(record)
            self.stats.inc("written")
            mid = str(record.get("id") or "")
            if mid:
                with self._pending_lock:
                    self._pending_ids.pop(mid, None)
            if self.post_write is not None:
                try:
                    self.post_write(record)
                except Exception as exc:  # 后台重活失败不影响写入
                    self.stats.set("last_error", "%s: %s" % (type(exc).__name__, exc))
            n = int(self.stats.data.get("written") or 0)
            if self.consolidator is not None and n % self.consolidate_every == 0:
                try:
                    self.consolidator.run_all(dry_run=False)
                    self.stats.inc("consolidations")
                except Exception as exc:
                    self.stats.set("last_error", "consolidate: %s" % exc)
            return True
        except Exception as exc:
            self.stats.inc("failed")
            self.stats.set("last_error", "%s: %s" % (type(exc).__name__, exc))
            return False

    # ------------------------------------------------------------------
    # 后台循环
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        batch: List[Dict[str, Any]] = []
        last_flush = time.time()
        while not self._stop.is_set():
            try:
                item = self._q.get(timeout=0.1)
            except queue.Empty:
                item = "__timeout__"  # 哨兵：只用来触发超时落盘
            if item is None:  # close() 的唤醒信号
                break
            if item != "__timeout__":
                batch.append(item)  # type: ignore[arg-type]
            now = time.time()
            if len(batch) >= self.batch_size or (batch and now - last_flush >= self.flush_interval):
                self._flush_batch(batch)
                batch = []
                last_flush = now
        if batch:
            self._flush_batch(batch)

    def _flush_batch(self, batch: List[Dict[str, Any]]) -> None:
        """批量落盘。

        优先 ``store.append_batch``（一次 fsync），不可用则逐条写。
        """
        if not batch:
            return
        t0 = time.time()
        done = False
        try:
            if hasattr(self.brain.store, "append_batch"):
                self.brain.store.append_batch(batch)
                done = True
                self.stats.inc("written", len(batch))
        except Exception:
            done = False
        if not done:
            for rec in batch:
                self._write_one(rec)
                # _write_one 已计数，这里不再重复
            self.stats.inc("flushes")
            self.stats.set("last_flush_ms", round((time.time() - t0) * 1000, 2))
            return
        # 批量路径：pending_ids 必须在这里清，否则 wait_written() 永远等不到
        with self._pending_lock:
            for rec in batch:
                self._pending_ids.pop(str(rec.get("id") or ""), None)
        # 批量路径也要跑冷路径重活
        if self.post_write is not None:
            for rec in batch:
                try:
                    self.post_write(rec)
                except Exception as exc:
                    self.stats.set("last_error", "%s: %s" % (type(exc).__name__, exc))
        self.stats.inc("flushes")
        self.stats.set("last_flush_ms", round((time.time() - t0) * 1000, 2))

    # ------------------------------------------------------------------
    # 同步屏障
    # ------------------------------------------------------------------

    def flush(self, timeout: float = 10.0) -> Dict[str, Any]:
        """把队列里剩下的全部落盘（读一致性屏障）。

        ``Brain.recall()`` 在启用异步写入时会先调它，保证「刚写的能读到」。
        实现上不依赖 worker 线程：直接在当前线程把队列抽干，避免死等。
        """
        if not self._started and self._q.empty():
            return self.stats.snapshot()
        drained: List[Dict[str, Any]] = []
        deadline = time.time() + max(0.1, timeout)
        while time.time() < deadline:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is None:
                continue
            drained.append(item)  # type: ignore[arg-type]
        if drained:
            self._flush_batch(drained)
        return self.stats.snapshot()

    def drain(self) -> List[Dict[str, Any]]:
        """取出队列中所有条目（不落盘），用于测试与调试。"""
        out: List[Dict[str, Any]] = []
        while True:
            try:
                item = self._q.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                out.append(item)  # type: ignore[arg-type]
        return out

    # ------------------------------------------------------------------
    # 观测
    # ------------------------------------------------------------------

    def status(self) -> Dict[str, Any]:
        with self._pending_lock:
            pending = len(self._pending_ids)
        return {
            "enabled": self._started,
            "queue_size": self._q.qsize(),
            "queue_capacity": self.max_queue,
            "batch_size": self.batch_size,
            "flush_interval_s": self.flush_interval,
            "pending_ids": pending,
            "stats": self.stats.snapshot(),
        }

    def wait_written(self, memory_id: str, timeout: float = 5.0) -> bool:
        """等某条记录真正落盘（测试与「写完立刻读」场景用）。"""
        deadline = time.time() + max(0.05, timeout)
        while time.time() < deadline:
            with self._pending_lock:
                if memory_id not in self._pending_ids:
                    return True
            self.flush(timeout=0.2)
            time.sleep(0.02)
        with self._pending_lock:
            return memory_id not in self._pending_ids
