"""철 그래프 취소 표시 (https://github.com/reddol18/docflow/issues/5). 워커가 그래프 작업의 임대를 잃으면(다른 워커가 되찾음)
표시를 세우고, 그래프 노드 시작(app/graph.py traced)·LLM 예약 직전(app/llm.py _metered)에서 확인해 끊는다 — 두 워커가
같은 철 그래프를 함께 돌리거나 비용을 두 번 쓰지 않게. 다른 모듈에 기대지 않는다 (불러오기 순환 없음)."""

import threading

_flags: dict[str, threading.Event] = {}
_lock = threading.Lock()


class Cancelled(BaseException):  # noqa: N818 — Exception이 아니다: 노드의 except Exception(오류 이벤트·재시도)에 잡히지 않게
    """이 프로세스가 더는 이 철의 그래프를 쥐고 있지 않다"""


def arm(vol) -> threading.Event:
    """그래프를 시작할 때 — 새 표시 (앞선 표시는 버린다)"""
    with _lock:
        ev = _flags[vol] = threading.Event()
    return ev


def disarm(vol, ev) -> None:
    with _lock:
        if _flags.get(vol) is ev:
            del _flags[vol]


def check(vol) -> None:
    ev = _flags.get(vol)
    if ev is not None and ev.is_set():
        raise Cancelled(vol)
