from __future__ import annotations

import asyncio
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

    from .._native import (
        NativeCompiledExecutionGraph,
        NativeExecutionBatch,
        NativeExecutionSession,
    )


class BatchExecutionHost(Protocol):
    def skipped_nodes(self) -> list[int]: ...

    def resolve_conditions(
        self,
        requests: list[tuple[int, int]],
    ) -> list[tuple[int, bool]]: ...

    def apply_events(self, events: list[tuple[int, int, bool | None]]) -> None: ...

    def run_leaf(self, node: int) -> None: ...

    def stop_requested(self) -> bool: ...

    def request_stop(self) -> None: ...

    def require_running(self) -> None: ...


@dataclass(frozen=True, slots=True)
class _DispatchBatch:
    actions: tuple[int, ...]
    finished: bool


def _resolve_batch_conditions(
    session: NativeExecutionSession,
    host: BatchExecutionHost,
    batch: NativeExecutionBatch,
    *,
    max_actions: int,
) -> _DispatchBatch:
    actions: list[int] = []
    while True:
        batch_actions, conditions, events, finished = batch.parts()
        host.apply_events(events)
        actions.extend(batch_actions)
        if not conditions:
            return _DispatchBatch(
                actions=tuple(actions),
                finished=finished,
            )
        resolutions = host.resolve_conditions(conditions)
        batch = session.advance(
            [],
            resolutions,
            host.skipped_nodes(),
            max_actions - len(actions),
        )


def run_serial_session(
    graph: NativeCompiledExecutionGraph,
    host: BatchExecutionHost,
) -> None:
    session = graph.session(host.skipped_nodes())
    batch = _resolve_batch_conditions(session, host, session.start(1), max_actions=1)
    while not batch.finished:
        host.require_running()
        if not batch.actions:
            raise RuntimeError("running graph session returned no serial action")
        node = batch.actions[0]
        host.run_leaf(node)
        batch = _resolve_batch_conditions(
            session,
            host,
            session.advance([node], [], host.skipped_nodes(), 1),
            max_actions=1,
        )


def run_threaded_session(
    graph: NativeCompiledExecutionGraph,
    host: BatchExecutionHost,
    *,
    workers: int,
) -> None:
    if workers < 1:
        raise ValueError("graph workers must be >= 1")
    session = graph.session(host.skipped_nodes())
    running: dict[Future[None], int] = {}
    failure: BaseException | None = None
    batch = _resolve_batch_conditions(
        session,
        host,
        session.start(workers),
        max_actions=workers,
    )
    with ThreadPoolExecutor(
        max_workers=workers, thread_name_prefix="GraphWorker"
    ) as pool:
        while True:
            if failure is None and not host.stop_requested():
                for node in batch.actions:
                    running[pool.submit(host.run_leaf, node)] = node

            if batch.finished and not running:
                break
            if not running:
                host.require_running()
                raise RuntimeError("running graph session returned no threaded action")

            done, _ = wait(running, return_when=FIRST_COMPLETED)
            completed: list[int] = []
            for future in done:
                node = running.pop(future)
                try:
                    future.result()
                except BaseException as error:
                    if failure is None:
                        failure = error
                        host.request_stop()
                else:
                    completed.append(node)

            if failure is not None:
                if running:
                    continue
                raise failure

            available = workers - len(running)
            batch = _resolve_batch_conditions(
                session,
                host,
                session.advance(
                    completed,
                    [],
                    host.skipped_nodes(),
                    available,
                ),
                max_actions=available,
            )
    host.require_running()


async def run_async_session(
    graph: NativeCompiledExecutionGraph,
    host: BatchExecutionHost,
    run_leaf: Callable[[int], Awaitable[None]],
    *,
    concurrency: int,
) -> None:
    if concurrency < 1:
        raise ValueError("graph concurrency must be >= 1")
    session = graph.session(host.skipped_nodes())
    running: dict[asyncio.Future[None], int] = {}
    failure: BaseException | None = None
    batch = _resolve_batch_conditions(
        session,
        host,
        session.start(concurrency),
        max_actions=concurrency,
    )
    while True:
        if failure is None and not host.stop_requested():
            for node in batch.actions:
                running[asyncio.ensure_future(run_leaf(node))] = node

        if batch.finished and not running:
            break
        if not running:
            host.require_running()
            raise RuntimeError("running graph session returned no async action")

        done, _ = await asyncio.wait(
            running,
            return_when=asyncio.FIRST_COMPLETED,
        )
        completed: list[int] = []
        for task in done:
            node = running.pop(task)
            try:
                task.result()
            except BaseException as error:
                if failure is None:
                    failure = error
                    host.request_stop()
            else:
                completed.append(node)

        if failure is not None:
            if running:
                continue
            raise failure

        available = concurrency - len(running)
        batch = _resolve_batch_conditions(
            session,
            host,
            session.advance(
                completed,
                [],
                host.skipped_nodes(),
                available,
            ),
            max_actions=available,
        )
    host.require_running()
