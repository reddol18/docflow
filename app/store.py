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


def pages_dir(vol):
    return DATA / "pages" / vol


def ocr_path(vol, page):
    return DATA / "ocr" / vol / f"{page}.json"


def crop_path(vol, page, field):
    return DATA / "crops" / vol / f"{page}_{field}.png"


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
ALTER TABLE review_time ADD COLUMN IF NOT EXISTS user_id bigint
"""


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
        return c.execute("SELECT id, ts, node, phase, doc_id, info FROM events WHERE vol = %s AND id > %s "
                         "ORDER BY id LIMIT %s", (vol, after, limit)).fetchall()


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


def edit_history(vol, doc_id):
    with pool.connection() as c:
        # sso: SSO 사용자는 이름을 제공자가 정하므로 local 계정과 이름이 같을 수 있다 → 구별해서 보여 준다
        return c.execute("SELECT e.ts, e.field, e.old, e.new, e.source, u.username, u.name, u.issuer IS NOT NULL AS sso FROM edits e "
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
