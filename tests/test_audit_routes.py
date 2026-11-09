"""GET이 아닌 경로는 모두 감사된다 (https://github.com/reddol18/docflow/issues/63).
경로 함수마다 auth.audit_as(행위)가 붙어 있어야 한다 — 없으면 이 시험이 실패한다. 새 변경 경로를 만들 때 감사를 빠뜨리지 않게."""
import ast
import inspect
import textwrap

from fastapi import APIRouter, FastAPI

from app import auth


def test_every_mutating_route_is_audited():
    from app import main
    assert main.unaudited_routes(main.app) == []
    mutating = {path: r.endpoint.audit_action for path, r in main.api_routes(main.app.routes)
                if set(r.methods) - set(auth.SAFE_METHODS)}
    assert len(mutating) >= 14  # 경로 목록을 못 읽어 빈 채로 통과하지 않게 (include_router 안의 /auth 경로 포함)
    # 로그인·로그아웃도 같은 표시 — 처리 함수 앞 거절(본문 검증 422·CSRF 403)이 이 이름으로 남는다
    assert mutating["/auth/login"] == "login" and mutating["/auth/logout"] == "logout"
    assert mutating["/volumes/{vol}/pages"] == "upload"


AUDIT_CALLS = {"audited", "audit_event"}  # 두째 인자가 행위 이름 (audit_event는 audit_later로 미뤄 부를 때 셋째)


def _audit_actions(fn):
    """경로 함수 몸에서 감사 쓰기가 쓰는 행위 이름들 (AST — 주석·문자열 속 글자는 세지 않는다)"""
    tree = ast.parse(textwrap.dedent(inspect.getsource(fn)))
    out = set()
    for n in ast.walk(tree):
        if not isinstance(n, ast.Call) or not isinstance(n.func, (ast.Name, ast.Attribute)):
            continue
        name = n.func.id if isinstance(n.func, ast.Name) else n.func.attr
        args = n.args
        if name == "audit_later" and args and isinstance(args[0], ast.Name):  # audit_later(fn, *args)
            name, args = args[0].id, args[1:]
        if name == "login_failed":
            out.add("login")
        elif name in AUDIT_CALLS and len(args) >= 2 and isinstance(args[1], ast.Constant):
            out.add(args[1].value)
    return out


def test_marked_routes_write_their_action():
    """표시(audit_as)와 실제 감사 쓰기가 함께 간다: 표시된 경로 함수는 그 행위 이름으로 audited()/audit_event를 부른다.
    표시만 붙이고 audited()를 빼먹으면 거절 행은 남아도 성공 행이 빠진다 — 그것을 막는다"""
    from app import main
    marked = [(path, r.endpoint) for path, r in main.api_routes(main.app.routes)
              if getattr(r.endpoint, "audit_action", None)]
    assert len(marked) >= 17
    missing = [(path, fn.audit_action, sorted(_audit_actions(fn))) for path, fn in marked
               if fn.audit_action not in _audit_actions(fn)]
    assert missing == []


def test_marker_without_audit_call_is_caught():
    @auth.audit_as("thing")
    def handler(user):
        return {"ok": True}

    @auth.audit_as("thing")
    def handler2(user):
        with audited(user, "thing"):  # noqa: F821 — 실행하지 않고 소스만 본다
            return {}

    assert _audit_actions(handler) == set() and _audit_actions(handler2) == {"thing"}


def test_unaudited_route_is_caught():
    """표시 없는 변경 경로는 앱에 바로 붙인 것도, include_router로 넣은 라우터 안의 것도 잡는다"""
    from app import main
    app = FastAPI()
    sub = APIRouter(prefix="/sub")

    @app.post("/x")
    def x():
        return {}

    @app.post("/y")
    @auth.audit_as("y")
    def y():
        return {}

    @app.get("/z")
    def z():
        return {}

    @sub.delete("/w")
    def w():
        return {}

    app.include_router(sub, prefix="/api")
    assert sorted(main.unaudited_routes(app)) == [("DELETE", "/api/sub/w"), ("POST", "/x")]
