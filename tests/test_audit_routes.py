"""GET이 아닌 경로는 모두 감사된다 (https://github.com/reddol18/docflow/issues/63).
경로 함수마다 auth.audit_as(행위)가 붙어 있어야 한다 — 없으면 이 시험이 실패한다. 새 변경 경로를 만들 때 감사를 빠뜨리지 않게."""
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
