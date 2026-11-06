"""저장소: 파일(쪽 이미지·OCR·잘라낸 칸), Postgres(문서 색인·비용 장부·LangGraph 체크포인트), Qdrant(벡터)."""
import datetime as dt
import json
import os
import uuid
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

DATA = Path(os.environ.get("DATA_DIR", "/data"))
PG_URL = os.environ.get("PG_URL", "postgresql://docflow@postgres:5432/docflow")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLLECTION = "documents"
KST = dt.timezone(dt.timedelta(hours=9))

pool = ConnectionPool(PG_URL, open=False, min_size=1, max_size=8,
                      kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})


class PathEscape(ValueError):
    """철·쪽 이름이 저장 폴더 밖을 가리킴 (main이 400으로 바꾼다)"""


def under(kind, *parts):
    """DATA/kind 아래 경로. 각 부분은 경로 한 칸이어야 하고('.'·'..'·구분자 없음), 합친 경로가 그 폴더 안이어야 한다
    — 철 이름·쪽 이름이 '..'·'/'로 새지 않게 (https://github.com/reddol18/docflow/issues/49, ASVS V12.3.1).
    입력 형식 검사(main.check)에 더한 두 번째 방어. 경로 정규화는 글자로만 한다(os.path.abspath) —
    resolve()는 심볼릭 링크를 따라가 정상 폴더(링크로 붙인 데이터 폴더)를 밖으로 잘못 볼 수 있다."""
    for part in parts:
        if part in ("", ".", "..") or any(s in part for s in ("/", "\\", "\0")):
            raise PathEscape(f"bad path part in {kind}: {parts!r}")
    root = Path(os.path.abspath(DATA / kind))
    p = Path(os.path.abspath(root.joinpath(*parts)))
    if p == root or not p.is_relative_to(root):
        raise PathEscape(f"path escapes {kind}: {parts!r}")
    return DATA / kind / Path(*parts)


def pages_dir(vol):
    return under("pages", vol)


def page_path(vol, page):
    return under("pages", vol, f"{page}.jpg")


def ocr_dir(vol):
    return under("ocr", vol)


def ocr_path(vol, page):
    return under("ocr", vol, f"{page}.json")


def crop_path(vol, page, field):
    return under("crops", vol, f"{page}_{field}.png")


def crop_file(vol, name):
    return under("crops", vol, name)


def load_ocr(vol, page):
    return json.loads(ocr_path(vol, page).read_text(encoding="utf-8"))


SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
  doc_id text PRIMARY KEY, vol text NOT NULL, pages text[] NOT NULL,
  docno text, serial text, date text, recipient text, title text,
  flags jsonb, reviewed boolean DEFAULT false, updated_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS usage (
  id bigserial PRIMARY KEY, ts timestamptz DEFAULT now(), vol text, kind text, model text,
  tin int, tout int, usd numeric(12,8));
CREATE TABLE IF NOT EXISTS events (
  id bigserial PRIMARY KEY, ts timestamptz DEFAULT now(), vol text NOT NULL, node text NOT NULL,
  phase text NOT NULL, doc_id text, info jsonb);
CREATE INDEX IF NOT EXISTS events_vol_id ON events (vol, id);
ALTER TABLE documents ADD COLUMN IF NOT EXISTS edited boolean DEFAULT false;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();
CREATE TABLE IF NOT EXISTS review_time (
  id bigserial PRIMARY KEY, ts timestamptz DEFAULT now(), vol text NOT NULL, doc_id text NOT NULL,
  mode text NOT NULL, active_ms int NOT NULL, opened_at timestamptz, fields_changed int);
CREATE INDEX IF NOT EXISTS review_time_ts ON review_time (ts);
CREATE INDEX IF NOT EXISTS events_ts ON events (ts);
CREATE TABLE IF NOT EXISTS users (
  id bigserial PRIMARY KEY, username text NOT NULL, name text, role text NOT NULL,
  issuer text, subject text, password_hash text, failed int NOT NULL DEFAULT 0, locked_until timestamptz,
  disabled boolean NOT NULL DEFAULT false, created_at timestamptz DEFAULT now(), last_login timestamptz);
CREATE UNIQUE INDEX IF NOT EXISTS users_local ON users (username) WHERE issuer IS NULL;
CREATE UNIQUE INDEX IF NOT EXISTS users_oidc ON users (issuer, subject) WHERE issuer IS NOT NULL;
CREATE TABLE IF NOT EXISTS sessions (
  token_hash text PRIMARY KEY, user_id bigint NOT NULL REFERENCES users (id) ON DELETE CASCADE,
  via text NOT NULL, created_at timestamptz DEFAULT now(), last_seen timestamptz DEFAULT now(), expires_at timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS oidc_pending (
  state text PRIMARY KEY, nonce text NOT NULL, verifier text NOT NULL, next text, created_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS edits (
  id bigserial PRIMARY KEY, ts timestamptz DEFAULT now(), vol text NOT NULL, doc_id text NOT NULL, field text NOT NULL,
  old text, new text, user_id bigint REFERENCES users (id), source text NOT NULL);
CREATE INDEX IF NOT EXISTS edits_doc ON edits (vol, doc_id);
ALTER TABLE review_time ADD COLUMN IF NOT EXISTS user_id bigint;
ALTER TABLE users ADD COLUMN IF NOT EXISTS attempt_at timestamptz;
CREATE TABLE IF NOT EXISTS audit (
  id bigserial PRIMARY KEY, ts timestamptz DEFAULT now(), user_id bigint, username text,
  action text NOT NULL, vol text, doc_id text, result text NOT NULL, info jsonb);
CREATE INDEX IF NOT EXISTS audit_ts ON audit (ts);
UPDATE events SET info = (info - 'error') || jsonb_build_object('code', 'internal') WHERE info ? 'error';
CREATE TABLE IF NOT EXISTS inspection_plan (
  vol text PRIMARY KEY, rate numeric NOT NULL, fields100 text[] NOT NULL, seed bigint NOT NULL,
  flagged int NOT NULL, unflagged int NOT NULL, sample int NOT NULL,
  created_by bigint, created_at timestamptz DEFAULT now());
CREATE TABLE IF NOT EXISTS inspection (
  vol text NOT NULL, doc_id text NOT NULL, reason text NOT NULL,
  inspected_by bigint, inspected_at timestamptz, fixed_fields text[],
  PRIMARY KEY (vol, doc_id))
"""
# ↑ 예전 판(https://github.com/reddol18/docflow/issues/28 이전)이 저장한 예외 원문을 표에서 지운다 (시작할 때마다, 남은 행만)


def init_db():
    if pool.closed:
        pool.open()
    with pool.connection() as c:
        for stmt in SCHEMA.split(";"):  # prepare_threshold=0이라 한 번에 한 문장만 실행할 수 있다
            if stmt.strip():
                c.execute(stmt)


def init():
    init_db()
    q = qdrant()
    if not q.collection_exists(COLLECTION):
        from qdrant_client.models import Distance, PayloadSchemaType, VectorParams
        q.create_collection(COLLECTION, vectors_config=VectorParams(size=1536, distance=Distance.COSINE))
        q.create_payload_index(COLLECTION, "year", PayloadSchemaType.INTEGER)
        q.create_payload_index(COLLECTION, "vol", PayloadSchemaType.KEYWORD)


def add_usage(vol, kind, model, tin, tout, usd):
    with pool.connection() as c:
        c.execute("INSERT INTO usage (vol, kind, model, tin, tout, usd) VALUES (%s,%s,%s,%s,%s,%s)",
                  (vol, kind, model, tin, tout, usd))


def spent_today():
    start = dt.datetime.now(KST).replace(hour=0, minute=0, second=0, microsecond=0)
    with pool.connection() as c:
        return float(c.execute("SELECT coalesce(sum(usd),0) AS s FROM usage WHERE ts >= %s", (start,)).fetchone()["s"])


def usage_summary(vol=None):
    sql = "SELECT kind, count(*) AS calls, sum(tin) AS tin, sum(tout) AS tout, sum(usd)::float AS usd FROM usage"
    args = ()
    if vol:
        sql += " WHERE vol = %s"
        args = (vol,)
    with pool.connection() as c:
        return c.execute(sql + " GROUP BY kind ORDER BY kind", args).fetchall()


def event(vol, node, phase, doc_id=None, **info):
    """대시보드 진행 화면용 노드 이벤트 (start/end/error)."""
    with pool.connection() as c:
        c.execute("INSERT INTO events (vol, node, phase, doc_id, info) VALUES (%s,%s,%s,%s,%s)",
                  (vol, node, phase, doc_id, json.dumps(info, ensure_ascii=False) if info else None))


def events(vol, after=0, limit=500):
    with pool.connection() as c:
        rows = c.execute("SELECT id, ts, node, phase, doc_id, info FROM events WHERE vol = %s AND id > %s "
                         "ORDER BY id LIMIT %s", (vol, after, limit)).fetchall()
    for r in rows:  # 예전 판(https://github.com/reddol18/docflow/issues/28 이전)은 예외 문구를 info.error에 저장했다 → 내보내지 않는다
        if r["info"] and "error" in r["info"]:
            r["info"] = {k: v for k, v in r["info"].items() if k != "error"} | {"code": r["info"].get("code", "internal")}
    return rows


def audit(user, action, vol=None, doc_id=None, result="ok", **info):
    """감사 로그: 누가·언제·무엇을(행위·철·문서)·결과. info에는 개수·칸 이름 같은 요약만 — 값·원문은 넣지 않는다
    (값 이력은 edits 표). user는 auth.User 또는 None."""
    with pool.connection() as c:
        c.execute("INSERT INTO audit (user_id, username, action, vol, doc_id, result, info) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                  (getattr(user, "id", None), getattr(user, "username", None), action, vol, doc_id, result,
                   json.dumps(info, ensure_ascii=False) if info else None))


def audit_log(limit=500):
    with pool.connection() as c:
        return c.execute("SELECT * FROM audit ORDER BY id DESC LIMIT %s", (limit,)).fetchall()


def add_review_times(vol, rows, user_id=None):
    """검토 화면이 잰 문서별 작업 시간. rows = [{doc_id, mode, active_ms, opened_at, fields_changed}]"""
    with pool.connection() as c:
        for r in rows:
            c.execute("INSERT INTO review_time (vol, doc_id, mode, active_ms, opened_at, fields_changed, user_id) "
                      "VALUES (%s,%s,%s,%s,%s,%s,%s)",
                      (vol, r["doc_id"], r["mode"], r["active_ms"], r.get("opened_at"), r.get("fields_changed"), user_id))


def add_edits(vol, rows, user_id, source):
    """수정 이력: rows = [(doc_id, 칸, 이전 값, 새 값)], source = review(검토 제출) | edit(색인 뒤 수정). 한 연결·한 트랜잭션."""
    if not rows:
        return
    with pool.connection() as c, c.transaction(), c.cursor() as cur:
        cur.executemany("INSERT INTO edits (vol, doc_id, field, old, new, user_id, source) VALUES (%s,%s,%s,%s,%s,%s,%s)",
                        [(vol, did, f, old, new, user_id, source) for did, f, old, new in rows])


def create_inspection(vol, summary, rows, user_id):
    """검사 목록을 한 번에 만든다 (한 트랜잭션). 이미 있으면 False"""
    with pool.connection() as c, c.transaction(), c.cursor() as cur:
        cur.execute("INSERT INTO inspection_plan (vol, rate, fields100, seed, flagged, unflagged, sample, created_by) "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (vol) DO NOTHING",
                    (vol, summary["rate"], summary["fields100"], summary["seed"], summary["flagged"],
                     summary["unflagged"], summary["sample"], user_id))
        if cur.rowcount == 0:
            return False
        cur.executemany("INSERT INTO inspection (vol, doc_id, reason) VALUES (%s,%s,%s)",
                        [(vol, d, r) for d, r in sorted(rows.items())])
    return True


def inspection_plan(vol):
    with pool.connection() as c:
        return c.execute("SELECT * FROM inspection_plan WHERE vol = %s", (vol,)).fetchone()


def inspection_rows(vol):
    with pool.connection() as c:
        return c.execute("SELECT i.doc_id, i.reason, i.inspected_at, i.fixed_fields, u.username AS inspected_by "
                         "FROM inspection i LEFT JOIN users u ON u.id = i.inspected_by WHERE i.vol = %s ORDER BY i.doc_id",
                         (vol,)).fetchall()


def inspection_row(vol, doc_id):
    with pool.connection() as c:
        return c.execute("SELECT * FROM inspection WHERE vol = %s AND doc_id = %s", (vol, doc_id)).fetchone()


def entrants(vol, doc_id):
    """이 문서의 입력 단계에 손댄 사용자 id: 칸을 고친 사람(edits review·edit) + 이 철의 검토를 제출한 사람(audit review ok)"""
    with pool.connection() as c:
        rows = c.execute("SELECT user_id FROM edits WHERE vol = %s AND doc_id = %s AND source IN ('review','edit') "
                         "UNION SELECT user_id FROM audit WHERE vol = %s AND action = 'review' AND result = 'ok'",
                         (vol, doc_id, vol)).fetchall()
    return {r["user_id"] for r in rows if r["user_id"] is not None}


def mark_inspected(vol, doc_id, user_id, fixed_fields):
    """검사 끝 표시. 이미 누가 검사했으면 False (동시에 두 번 제출 막기)"""
    with pool.connection() as c:
        cur = c.execute("UPDATE inspection SET inspected_by = %s, inspected_at = now(), fixed_fields = %s "
                        "WHERE vol = %s AND doc_id = %s AND inspected_by IS NULL", (user_id, fixed_fields, vol, doc_id))
        return cur.rowcount == 1


def inspection_stats():
    """철별 표본 검사 결과: 표본 수, 검사 끝난 표본 n, 그중 고친 k, 추정 오류율 k/n"""
    with pool.connection() as c:
        rows = c.execute("SELECT p.vol, p.sample, p.rate::float AS rate, "
                         "count(i.inspected_at) FILTER (WHERE i.reason = 'sample') AS inspected, "
                         "count(*) FILTER (WHERE i.reason = 'sample' AND cardinality(i.fixed_fields) > 0) AS fixed "
                         "FROM inspection_plan p LEFT JOIN inspection i ON i.vol = p.vol GROUP BY p.vol, p.sample, p.rate "
                         "ORDER BY p.vol").fetchall()
    for r in rows:
        r["error_rate"] = round(r["fixed"] / r["inspected"], 4) if r["inspected"] else None
    return rows


def edit_history(vol, doc_id):
    with pool.connection() as c:
        # sso: SSO 사용자는 이름을 제공자가 정하므로 local 계정과 이름이 같을 수 있다 → 구별해서 보여 준다
        # stage: 입력(검토 제출·색인 뒤 수정) / 검사 (https://github.com/reddol18/docflow/issues/10)
        return c.execute("SELECT e.ts, e.field, e.old, e.new, e.source, "
                         "CASE WHEN e.source = 'inspect' THEN 'inspect' ELSE 'entry' END AS stage, "
                         "u.username, u.name, u.issuer IS NOT NULL AS sso FROM edits e "
                         "LEFT JOIN users u ON u.id = e.user_id WHERE e.vol = %s AND e.doc_id = %s ORDER BY e.id",
                         (vol, doc_id)).fetchall()


def save_documents(docs):
    with pool.connection() as c:
        for d in docs:
            f = d["fields"]
            c.execute(
                """INSERT INTO documents (doc_id, vol, pages, docno, serial, date, recipient, title, flags, reviewed, updated_at)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                   ON CONFLICT (doc_id) DO UPDATE SET pages=EXCLUDED.pages, docno=EXCLUDED.docno, serial=EXCLUDED.serial,
                     date=EXCLUDED.date, recipient=EXCLUDED.recipient, title=EXCLUDED.title, flags=EXCLUDED.flags,
                     reviewed=EXCLUDED.reviewed, updated_at=now()""",
                (d["doc_id"], d["vol"], d["pages"], f["docno"], f["serial"], f["date"], f["recipient"], f["title"],
                 json.dumps(d["flags"], ensure_ascii=False), d.get("reviewed", False)))
            if d.get("edited"):
                c.execute("UPDATE documents SET edited = true WHERE doc_id = %s", (d["doc_id"],))


def keyword_search(words, year_from=None, year_to=None, limit=20):
    """제목·수신·문서번호에 든 낱말 수로 점수를 매긴다 (형태소 분석 없는 단순 부분 일치)."""
    if not words:
        return []
    hay = "coalesce(title,'') || ' ' || coalesce(recipient,'') || ' ' || coalesce(docno,'')"
    score = " + ".join(f"(CASE WHEN {hay} ILIKE %s THEN 1 ELSE 0 END)" for _ in words)
    sql = f"SELECT doc_id, ({score}) AS score FROM documents WHERE ({score}) > 0"  # noqa: S608 — 자리표시자만 끼움, 값은 매개변수
    args = [f"%{w}%" for w in words] * 2
    if year_from:
        sql += " AND left(date,4) >= %s"
        args.append(str(year_from))
    if year_to:
        sql += " AND left(date,4) <= %s"
        args.append(str(year_to))
    sql += " ORDER BY score DESC, doc_id LIMIT %s"
    args.append(limit)
    with pool.connection() as c:
        return c.execute(sql, args).fetchall()


def get_documents(ids):
    with pool.connection() as c:
        rows = c.execute("SELECT * FROM documents WHERE doc_id = ANY(%s)", (list(ids),)).fetchall()
    return {r["doc_id"]: r for r in rows}


_q = None


def qdrant():
    global _q
    if _q is None:
        from qdrant_client import QdrantClient
        _q = QdrantClient(url=QDRANT_URL, timeout=30)
    return _q


def point_id(doc_id):
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "docflow/" + doc_id))


def upsert_vectors(docs, vectors):
    from qdrant_client.models import PointStruct
    pts = []
    for d, v in zip(docs, vectors):
        f = d["fields"]
        year = int(f["date"][:4]) if f["date"][:4].isdigit() else None
        pts.append(PointStruct(id=point_id(d["doc_id"]), vector=v, payload={
            "doc_id": d["doc_id"], "vol": d["vol"], "year": year, "date": f["date"],
            "docno": f["docno"], "title": f["title"], "recipient": f["recipient"]}))
    qdrant().upsert(COLLECTION, points=pts)


def vector_search(vec, year_from=None, year_to=None, limit=20):
    from qdrant_client.models import FieldCondition, Filter, Range
    flt = None
    if year_from or year_to:
        flt = Filter(must=[FieldCondition(key="year", range=Range(gte=year_from, lte=year_to))])
    res = qdrant().query_points(COLLECTION, query=vec, query_filter=flt, limit=limit)
    return [{"doc_id": p.payload["doc_id"], "score": p.score} for p in res.points]
