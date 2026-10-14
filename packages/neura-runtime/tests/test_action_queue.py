from __future__ import annotations

import numpy as np
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from neura_contracts import ReasonCode
from neura_runtime.action_queue import ActionQueue, MergeKind, MergeSettings

DIM = 2


def chunk(start_value: float, length: int) -> np.ndarray:
    values = np.arange(start_value, start_value + length, dtype=np.float64)
    return np.stack([values, -values], axis=1)


def settings_for(
    kind: MergeKind, apc: int = 10, min_useful: int = 1, **kw: object
) -> MergeSettings:
    return MergeSettings(kind=kind, actions_per_chunk=apc, min_useful_actions=min_useful, **kw)  # type: ignore[arg-type]


def drain(queue: ActionQueue, n: int) -> list[tuple[int, float]]:
    out = []
    for _ in range(n):
        item = queue.pop()
        if item is None:
            break
        out.append((item[0], float(item[1][0])))
    return out


def test_replace_queues_executable_part_from_start() -> None:
    q = ActionQueue(DIM)
    result = q.merge(0, chunk(100, 20), settings_for(MergeKind.REPLACE, apc=10))
    assert result.accepted
    assert result.added == 10
    assert drain(q, 3) == [(0, 100.0), (1, 101.0), (2, 102.0)]
    assert q.cursor == 3
    assert len(q) == 7


def test_late_chunk_is_trimmed_never_executed_late() -> None:
    q = ActionQueue(DIM)
    q.merge(0, chunk(0, 10), settings_for(MergeKind.REPLACE))
    drain(q, 4)  # cursor 4
    result = q.merge(1, chunk(500, 10), settings_for(MergeKind.REPLACE))
    assert result.accepted
    assert result.trimmed == 3
    assert result.delay == 3
    # Index 4 now holds the chunk's action for index 4 (value 503), not its first action.
    assert drain(q, 1) == [(4, 503.0)]


def test_too_few_useful_actions_is_rejected_and_queue_unchanged() -> None:
    q = ActionQueue(DIM)
    q.merge(0, chunk(0, 10), settings_for(MergeKind.REPLACE))
    drain(q, 8)
    before = q.peek(10).copy()
    result = q.merge(0, chunk(900, 10), settings_for(MergeKind.REPLACE, min_useful=3))
    assert not result.accepted
    assert result.reason is ReasonCode.CHUNK_LATE
    np.testing.assert_array_equal(q.peek(10), before)


def test_append_keeps_queue_and_continues_after_it() -> None:
    q = ActionQueue(DIM)
    q.merge(0, chunk(0, 10), settings_for(MergeKind.APPEND))
    drain(q, 4)
    anchor = q.end_index  # 10
    q.merge(anchor, chunk(100, 10), settings_for(MergeKind.APPEND))
    values = [v for _, v in drain(q, 20)]
    assert values == [4, 5, 6, 7, 8, 9, *range(100, 110)]


def test_gap_is_rejected() -> None:
    q = ActionQueue(DIM)
    q.merge(0, chunk(0, 5), settings_for(MergeKind.APPEND))
    result = q.merge(7, chunk(100, 5), settings_for(MergeKind.APPEND))
    assert result.reason is ReasonCode.CHUNK_NOT_CONTIGUOUS


def test_blend_mixes_overlap_then_uses_new_chunk() -> None:
    q = ActionQueue(DIM)
    q.merge(0, chunk(0, 10), settings_for(MergeKind.BLEND, blend_weights=(0.5, 0.5)))
    drain(q, 2)  # cursor 2, queued values 2..9
    q.merge(2, chunk(100, 10), settings_for(MergeKind.BLEND, blend_weights=(0.25, 0.75)))
    values = [v for _, v in drain(q, 4)]
    # index 2: 0.25*100 + 0.75*2; index 3: 0.75*101 + 0.25*3; then new only.
    assert values == pytest.approx([26.5, 76.5, 102.0, 103.0])


def test_rtc_rejects_delay_beyond_conditioned_bound() -> None:
    q = ActionQueue(DIM)
    q.merge(0, chunk(0, 10), settings_for(MergeKind.RTC, max_conditioned_delay=2))
    drain(q, 3)
    result = q.merge(0, chunk(100, 10), settings_for(MergeKind.RTC, max_conditioned_delay=2))
    assert result.reason is ReasonCode.CHUNK_LATE
    ok = q.merge(1, chunk(100, 10), settings_for(MergeKind.RTC, max_conditioned_delay=2))
    assert ok.accepted


def test_clear_keeps_cursor_and_reset_restarts_indexing() -> None:
    q = ActionQueue(DIM)
    q.merge(0, chunk(0, 10), settings_for(MergeKind.REPLACE))
    drain(q, 3)
    q.clear()
    assert q.cursor == 3
    assert q.pop() is None
    q.reset_indexing()
    assert q.cursor == 0


def test_wrong_dimension_is_a_programming_error() -> None:
    with pytest.raises(ValueError, match="expected shape"):
        ActionQueue(DIM).merge(0, np.zeros((5, 3)), settings_for(MergeKind.REPLACE))


operations = st.lists(
    st.one_of(
        st.tuples(st.just("pop"), st.integers(1, 5)),
        st.tuples(
            st.just("merge"),
            st.integers(-12, 4),  # start relative to cursor
            st.integers(1, 15),  # chunk length
            st.sampled_from(list(MergeKind)),
        ),
    ),
    max_size=40,
)


@settings(max_examples=300, deadline=None)
@given(operations)
def test_dispatch_history_is_contiguous_and_never_rewritten(ops: list[tuple]) -> None:  # type: ignore[type-arg]
    """Whatever arrives, dispatched indexes are 0, 1, 2, ... and each action is dispatched once,
    with the value present in the queue at the moment it was popped."""
    q = ActionQueue(DIM)
    dispatched: list[int] = []
    next_value = 0.0
    for op in ops:
        if op[0] == "pop":
            for _ in range(op[1]):
                item = q.pop()
                if item is None:
                    break
                dispatched.append(item[0])
        else:
            _, rel, length, kind = op
            start = (
                max(0, q.cursor + rel)
                if kind is not MergeKind.APPEND
                else q.end_index + min(rel, 0)
            )
            before_cursor = q.cursor
            before_len = len(q)
            result = q.merge(
                start,
                chunk(next_value, length),
                MergeSettings(
                    kind=kind,
                    actions_per_chunk=10,
                    min_useful_actions=2,
                    blend_weights=(0.5, 0.5),
                    max_conditioned_delay=3,
                ),
            )
            next_value += 1000
            assert q.cursor == before_cursor  # merging never moves the cursor
            if not result.accepted:
                assert len(q) == before_len
            else:
                assert result.added >= 2
                assert result.added <= 10 - result.trimmed
    assert dispatched == list(range(len(dispatched)))
