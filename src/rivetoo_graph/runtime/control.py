from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field

type StopHandler = Callable[[], None]


class GraphExecutionStopped(RuntimeError):
    """Graph 执行作用域已经收到停止请求。"""


@dataclass(slots=True)
class GraphExecutionControl:
    """协调一组 Graph 执行及其宿主资源的停止状态。

    `GraphPlan` 默认创建独享实例；宿主若把多个顺序 Plan 视为同一次请求，
    可以显式传入同一实例。执行结果仍分别保存在各 Plan 的
    `GraphExecutionState` 中，不进入本对象。
    """

    _stop_event: threading.Event = field(
        default_factory=threading.Event,
        init=False,
        repr=False,
    )
    _handler_lock: threading.Lock = field(
        default_factory=threading.Lock,
        init=False,
        repr=False,
    )
    _stop_handlers: list[StopHandler] = field(
        default_factory=list,
        init=False,
        repr=False,
    )

    def stop_requested(self) -> bool:
        return self._stop_event.is_set()

    def require_running(self) -> None:
        if self.stop_requested():
            raise GraphExecutionStopped("graph execution stop requested")

    def register_stop_handler(self, handler: StopHandler) -> None:
        with self._handler_lock:
            if handler in self._stop_handlers:
                return
            self._stop_handlers.append(handler)

    def request_stop(self) -> None:
        with self._handler_lock:
            if self._stop_event.is_set():
                return
            self._stop_event.set()
            handlers = tuple(self._stop_handlers)
        for handler in handlers:
            handler()
