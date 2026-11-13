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


class FenceMixin:
    """LangGraph 체크포인터에 섞는다 (app/worker.py FencedSaver): 체크포인트·쓰기를 남기기 직전에 그 철(thread_id)의 취소 표시를
    본다 — 임대를 잃은 뒤 진행 중이던 노드가 끝나 늦게 쓰는 것을 막는다. 남는 틈: 확인과 쓰기 사이(짧음), 그리고 노드가
    체크포인트 밖에서 한 일(문서 행·벡터 저장 — 같은 값을 다시 쓰는 일이라 되찾은 워커가 덮는다).
    LangGraph는 체크포인트를 다음 단계와 겹쳐 쓰므로 막히면 끝난 단계 하나를 되찾은 워커가 다시 할 수 있다 (손해는 다시 하기뿐)"""

    def put(self, config, *args, **kwargs):
        check((config.get("configurable") or {}).get("thread_id"))
        return super().put(config, *args, **kwargs)

    def put_writes(self, config, *args, **kwargs):
        check((config.get("configurable") or {}).get("thread_id"))
        return super().put_writes(config, *args, **kwargs)
