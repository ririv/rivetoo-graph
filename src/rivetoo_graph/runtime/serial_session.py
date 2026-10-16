from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ..events import GraphEventDeliveryError
from .control import GraphExecutionStopped
from .session_tree import DynamicSubgraph, RootSession, SessionDefinition

if TYPE_CHECKING:
    from collections.abc import Callable

    from .._native import NativeMixedExecutionBatch, NativeMixedExecutionSession


@dataclass(frozen=True, slots=True)
class _Child:
    node: int
    complete: Callable[[], None]


@dataclass(slots=True)
class _Frame:
    definition: SessionDefinition
    native: NativeMixedExecutionSession
    batch: NativeMixedExecutionBatch
    owner: RootSession | _Child
    held_actions: list[int] = field(default_factory=list)


def _frame(definition: SessionDefinition, owner: RootSession | _Child) -> _Frame:
    native = definition.graph.mixed_session(
        definition.host.skipped_nodes(),
        definition.native_actions,
        definition.native_contexts,
        1,
    )
    return _Frame(definition, native, native.start(1), owner)


def run_serial_definition(root: SessionDefinition) -> None:
    # 串行只需显式栈：O(D) 个 session 帧，D 为动态嵌套深度。
    # 静态路径按原有单批次流程执行，不为每个 action 构造并发调度工作对象。
    stack = [_frame(root, RootSession())]
    try:
        while stack:
            frame = stack[-1]
            host = frame.definition.host
            host.require_running()
            actions, conditions, events, finished, native_running, failures = (
                frame.batch.parts()
            )
            host.apply_events(events)
            for node, message in failures:
                host.record_failure(node, RuntimeError(message))
            if conditions:
                frame.held_actions.extend(actions)
                frame.batch = frame.native.advance(
                    [],
                    host.resolve_conditions(conditions),
                    host.skipped_nodes(),
                    1 - len(frame.held_actions) - native_running,
                )
                continue
            if frame.held_actions:
                actions = frame.held_actions + actions
                frame.held_actions = []
            if finished:
                stack.pop()
                match frame.owner:
                    case RootSession():
                        host.raise_failures()
                    case _Child(node=node, complete=complete):
                        parent = stack[-1]
                        try:
                            host.raise_failures()
                        except Exception as error:
                            parent.definition.host.record_failure(node, error)
                            success = False
                        else:
                            complete()
                            success = True
                        parent.batch = parent.native.advance(
                            [(node, success)],
                            [],
                            parent.definition.host.skipped_nodes(),
                            1,
                        )
                continue
            if not actions:
                if not native_running:
                    raise RuntimeError(
                        "running graph session returned no serial action"
                    )
                frame.native.wait_native()
                frame.batch = frame.native.advance([], [], host.skipped_nodes(), 0)
                continue
            node = actions[0]
            try:
                result = host.run_leaf(node)
                if isinstance(result, DynamicSubgraph):
                    stack.append(
                        _frame(result.definition, _Child(node, result.complete))
                    )
                    continue
                if inspect.isawaitable(result):
                    raise TypeError(
                        "synchronous driver received an asynchronous action host"
                    )
            except (GraphExecutionStopped, GraphEventDeliveryError):
                raise
            except Exception as error:
                host.record_failure(node, error)
                success = False
            else:
                success = True
            frame.batch = frame.native.advance(
                [(node, success)], [], host.skipped_nodes(), 1
            )
    except BaseException:
        root.host.request_stop()
        raise
    root.host.require_running()
