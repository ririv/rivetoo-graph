from __future__ import annotations

import asyncio
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

from .control import GraphExecutionStopped

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .._native import (
        NativeActionBindings,
        NativeCompiledExecutionGraph,
        NativeMixedExecutionBatch,
        NativeMixedExecutionSession,
    )


class BatchExecutionHost(Protocol):
    def skipped_nodes(self) -> list[int]: ...

    def resolve_conditions(
        self,
        requests: list[tuple[int, int]],
    ) -> list[tuple[int, bool | None]]: ...

    def record_failure(self, node: int, error: Exception) -> None: ...

    def raise_failures(self) -> None: ...

    def apply_events(self, events: list[tuple[int, int, bool | None]]) -> None: ...

    def run_leaf(self, node: int) -> None: ...

    def stop_requested(self) -> bool: ...

    def request_stop(self) -> None: ...

    def require_running(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _DispatchBatch:
    actions: tuple[int, ...]
    finished: bool
    native_running: int


def _resolve_batch_conditions(
    session: NativeMixedExecutionSession,
    host: BatchExecutionHost,
    batch: NativeMixedExecutionBatch,
    *,
    capacity: int,
    occupied_python: int = 0,
) -> _DispatchBatch:
    actions: list[int] = []
    while True:
        batch_actions, conditions, events, finished, native_running, failures = (
            batch.parts()
        )
        host.apply_events(events)
        for node, message in failures:
            host.record_failure(node, RuntimeError(message))
        actions.extend(batch_actions)
        if not conditions:
            return _DispatchBatch(
                actions=tuple(actions),
                finished=finished,
                native_running=native_running,
            )
        resolutions = host.resolve_conditions(conditions)
        host.require_running()
        available = capacity - occupied_python - len(actions) - native_running
        batch = session.advance(
            [],
            resolutions,
            host.skipped_nodes(),
            available,
        )


def run_serial_session(
    graph: NativeCompiledExecutionGraph,
    host: BatchExecutionHost,
    native_actions: NativeActionBindings,
    native_contexts: list[tuple[int, str | None]],
) -> None:
    session = graph.mixed_session(
        host.skipped_nodes(), native_actions, native_contexts, 1
    )
    batch = _resolve_batch_conditions(session, host, session.start(1), capacity=1)
    while not batch.finished:
        host.require_running()
        if not batch.actions and batch.native_running:
            session.wait_native()
            batch = _resolve_batch_conditions(
                session,
                host,
                session.advance([], [], host.skipped_nodes(), 0),
                capacity=1,
            )
            continue
        if not batch.actions:
            raise RuntimeError("running graph session returned no serial action")
        node = batch.actions[0]
        try:
            host.run_leaf(node)
        except GraphExecutionStopped:
            raise
        except Exception as error:
            host.record_failure(node, error)
            success = False
        else:
            success = True
        host.require_running()
        batch = _resolve_batch_conditions(
            session,
            host,
            session.advance([(node, success)], [], host.skipped_nodes(), 1),
            capacity=1,
        )
    host.raise_failures()


def run_threaded_session(
    graph: NativeCompiledExecutionGraph,
    host: BatchExecutionHost,
    *,
    workers: int,
    native_actions: NativeActionBindings,
    native_contexts: list[tuple[int, str | None]],
) -> None:
    if workers < 1:
        raise ValueError("graph workers must be >= 1")
    session = graph.mixed_session(
        host.skipped_nodes(), native_actions, native_contexts, workers
    )
    running: dict[Future[None], int] = {}
    native_waiter: Future[None] | None = None
    failure: BaseException | None = None
    batch = _resolve_batch_conditions(
        session,
        host,
        session.start(workers),
        capacity=workers,
    )
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="GraphWorker"
    ) as pool:
        try:
            while True:
                if failure is None and not host.stop_requested():
                    for node in batch.actions:
                        running[pool.submit(host.run_leaf, node)] = node

                if batch.native_running and native_waiter is None:
                    native_waiter = pool.submit(session.wait_native)

                if batch.finished and not running and native_waiter is None:
                    break
                waits = set(running)
                if native_waiter is not None:
                    waits.add(native_waiter)
                if not waits:
                    host.require_running()
                    raise RuntimeError(
                        "running graph session returned no threaded action"
                    )

                done, _ = wait(waits, return_when=FIRST_COMPLETED)
                completed: list[tuple[int, bool]] = []
                for future in done:
                    waiter = native_waiter
                    if waiter is not None and future is waiter:
                        waiter.result()
                        native_waiter = None
                        continue
                    node = running.pop(future)
                    try:
                        future.result()
                    except GraphExecutionStopped as error:
                        if failure is None:
                            failure = error
                            host.request_stop()
                    except Exception as error:
                        host.record_failure(node, error)
                        completed.append((node, False))
                    except BaseException as error:
                        if failure is None:
                            failure = error
                            host.request_stop()
                    else:
                        completed.append((node, True))

                if failure is not None:
                    # 不再轮询陈旧 batch；退出线程池会等待已经提交的 Python 工作。
                    raise failure

                available = workers - len(running) - batch.native_running
                host.require_running()
                batch = _resolve_batch_conditions(
                    session,
                    host,
                    session.advance(
                        completed,
                        [],
                        host.skipped_nodes(),
                        available,
                    ),
                    capacity=workers,
                    occupied_python=len(running),
                )
        except BaseException:
            host.request_stop()
            raise
    host.require_running()
    host.raise_failures()


async def run_async_session(
    graph: NativeCompiledExecutionGraph,
    host: BatchExecutionHost,
    run_leaf: Callable[[int], Awaitable[None]],
    *,
    concurrency: int,
    native_actions: NativeActionBindings,
    native_contexts: list[tuple[int, str | None]],
) -> None:
    if concurrency < 1:
        raise ValueError("graph concurrency must be >= 1")
    session = graph.mixed_session(
        host.skipped_nodes(), native_actions, native_contexts, concurrency
    )
    running: dict[asyncio.Future[None], int] = {}
    native_waiter: asyncio.Task[None] | None = None
    failure: BaseException | None = None
    try:
        batch = _resolve_batch_conditions(
            session,
            host,
            session.start(concurrency),
            capacity=concurrency,
        )
        while True:
            if failure is None and not host.stop_requested():
                for node in batch.actions:
                    running[asyncio.ensure_future(run_leaf(node))] = node

            if batch.native_running and native_waiter is None:
                native_waiter = asyncio.create_task(
                    asyncio.to_thread(session.wait_native)
                )

            if batch.finished and not running and native_waiter is None:
                break
            waits = set(running)
            if native_waiter is not None:
                waits.add(native_waiter)
            if not waits:
                host.require_running()
                raise RuntimeError("running graph session returned no async action")

            done, _ = await asyncio.wait(
                waits,
                return_when=asyncio.FIRST_COMPLETED,
            )
            completed: list[tuple[int, bool]] = []
            for task in done:
                waiter = native_waiter
                if waiter is not None and task is waiter:
                    waiter.result()
                    native_waiter = None
                    continue
                node = running.pop(task)
                try:
                    task.result()
                except GraphExecutionStopped as error:
                    if failure is None:
                        failure = error
                        host.request_stop()
                except Exception as error:
                    host.record_failure(node, error)
                    completed.append((node, False))
                except BaseException as error:
                    if failure is None:
                        failure = error
                        host.request_stop()
                else:
                    completed.append((node, True))

            if failure is not None:
                raise failure

            available = concurrency - len(running) - batch.native_running
            host.require_running()
            batch = _resolve_batch_conditions(
                session,
                host,
                session.advance(
                    completed,
                    [],
                    host.skipped_nodes(),
                    available,
                ),
                capacity=concurrency,
                occupied_python=len(running),
            )
        host.require_running()
    except BaseException:
        host.request_stop()
        raise
    finally:
        # 条件求值或主动取消也可能离开循环；已启动任务始终先收拢，
        # 不让 asyncio.run 的关闭流程代为取消仍在执行的 action。
        await asyncio.gather(
            *running,
            *(() if native_waiter is None else (native_waiter,)),
            return_exceptions=True,
        )
    host.raise_failures()
