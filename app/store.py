"""저장소: 파일(쪽 이미지·OCR·잘라낸 칸), Postgres(문서 색인·비용 장부·LangGraph 체크포인트), Qdrant(벡터)."""
import datetime as dt
import hashlib
import json
import os
import shutil
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from app import fields
from app.workflow import CONFIRMED

DATA = Path(os.environ.get("DATA_DIR", "/data"))
PG_URL = os.environ.get("PG_URL", "postgresql://docflow@postgres:5432/docflow")
QDRANT_URL = os.environ.get("QDRANT_URL", "http://qdrant:6333")
COLLECTION = "documents"
KST = dt.timezone(dt.timedelta(hours=9))

pool = ConnectionPool(PG_URL, open=False, min_size=1, max_size=8,
                      kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row})
# 문서 잠금(doc_lock)만 쥐는 연결. 잠금을 쥔 요청이 pool에서 쓰기 연결을 하나 더 쓰므로 pool보다 작게 둔다
lock_pool = ConnectionPool(PG_URL, open=False, min_size=0, max_size=4,
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
  doc_id text NOT NULL, vol text NOT NULL, pages text[] NOT NULL,
  fields jsonb NOT NULL DEFAULT '{}'::jsonb, search_text text, year int,
  flags jsonb, reviewed boolean DEFAULT false, updated_at timestamptz DEFAULT now(),
  PRIMARY KEY (vol, doc_id));
ALTER TABLE documents ADD COLUMN IF NOT EXISTS fields jsonb NOT NULL DEFAULT '{}'::jsonb;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS search_text text;
ALTER TABLE documents ADD COLUMN IF NOT EXISTS year int;
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
  PRIMARY KEY (vol, doc_id));
CREATE TABLE IF NOT EXISTS review_submitters (
  vol text NOT NULL, user_id bigint NOT NULL, ts timestamptz DEFAULT now(), PRIMARY KEY (vol, user_id));
CREATE TABLE IF NOT EXISTS exports (
  id bigserial PRIMARY KEY, ts timestamptz DEFAULT clock_timestamp(), vol text NOT NULL, problems int NOT NULL, user_id bigint);
CREATE TABLE IF NOT EXISTS doc_stage (
  vol text NOT NULL, doc_id text NOT NULL, stage text NOT NULL, round int NOT NULL DEFAULT 1,
  last_reason text, rejected_from text, updated_at timestamptz DEFAULT now(),
  PRIMARY KEY (vol, doc_id));
CREATE TABLE IF NOT EXISTS stage_log (
  id bigserial PRIMARY KEY, ts timestamptz DEFAULT clock_timestamp(), vol text NOT NULL, doc_id text NOT NULL,
  stage text NOT NULL, round int NOT NULL, action text NOT NULL, to_stage text NOT NULL, user_id bigint,
  reason text, fixed_fields text[], gen int NOT NULL DEFAULT 0);
ALTER TABLE stage_log ADD COLUMN IF NOT EXISTS gen int NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS stage_log_doc ON stage_log (vol, doc_id);
CREATE TABLE IF NOT EXISTS stage_gen (vol text PRIMARY KEY, gen int NOT NULL, since timestamptz NOT NULL);
CREATE TABLE IF NOT EXISTS schema_migrations (name text PRIMARY KEY, done_at timestamptz NOT NULL DEFAULT now())
"""
# ↑ schema_migrations: 한 번만 돌리는 데이터 옮기기의 완료 표시 (전체를 훑는 옮기기를 시작할 때마다 다시 돌리지 않게).
#   중간에 실패하면 표시를 남기지 않아 다음 시작에 이어서 한다
# ↑ doc_stage·stage_log (https://github.com/reddol18/docflow/issues/86): 새 표만 더한다. 행이 없는 문서(예전 철)는
#   흐름 설정의 처음 단계에 있는 것으로 본다 → 옛 철은 옮길 것 없이 그대로 열린다
# ↑ 예전 판(https://github.com/reddol18/docflow/issues/28 이전)이 저장한 예외 원문을 표에서 지운다 (시작할 때마다, 남은 행만)


def init_db():
    if pool.closed:
        pool.open()
    if lock_pool.closed:
        lock_pool.kwargs = dict(pool.kwargs)  # 시험 서버용 설정(prepare_threshold)도 같게
        lock_pool.open()
    with pool.connection() as c:
        for stmt in SCHEMA.split(";"):  # prepare_threshold=0이라 한 번에 한 문장만 실행할 수 있다
            if stmt.strip():
                c.execute(stmt)
        migrate_documents(c)
        migrate_doc_key(c)
    prune_audit()


LEGACY_COLUMNS = ("docno", "serial", "date", "recipient", "title")  # https://github.com/reddol18/docflow/issues/9 이전 고정 열


def migrate_documents(c, schema=None):
    """예전 고정 열 → fields jsonb + 검색용 열(search_text·year). 예전 열이 남아 있을 때만, 한 트랜잭션으로.
    중간에 실패하면 되돌아가 예전 열이 그대로 남고 다음 시작 때 다시 한다. 옮긴 행 수"""
    schema = schema or fields.SCHEMA
    have = {r["column_name"] for r in c.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_schema = current_schema() "
        "AND table_name = 'documents'").fetchall()}
    old = [k for k in LEGACY_COLUMNS if k in have]
    if not old:
        return 0
    with c.transaction():
        pairs = ", ".join(f"'{k}', coalesce({k}, '')" for k in old)
        c.execute(f"UPDATE documents SET fields = jsonb_build_object({pairs}) || fields")  # noqa: S608 — 고정 열 이름
        rows = c.execute("SELECT vol, doc_id, fields FROM documents").fetchall()
        with c.cursor() as cur:
            cur.executemany("UPDATE documents SET search_text = %s, year = %s WHERE vol = %s AND doc_id = %s",
                            [(fields.search_text(schema, r["fields"]), fields.year_of(schema, r["fields"]), r["vol"],
                              r["doc_id"]) for r in rows])
        c.execute("ALTER TABLE documents " + ", ".join(f"DROP COLUMN {k}" for k in old))
    print(f"documents: 예전 고정 열 {old} → fields jsonb ({len(rows)}행)", flush=True)
    return len(rows)


def _pk_columns(c, table):
    """표의 기본 키 열 (순서대로)과 제약 이름"""
    rows = c.execute(
        "SELECT con.conname, a.attname FROM pg_constraint con "
        "JOIN pg_class t ON t.oid = con.conrelid JOIN pg_namespace n ON n.oid = t.relnamespace "
        "JOIN unnest(con.conkey) WITH ORDINALITY k(attnum, ord) ON true "
        "JOIN pg_attribute a ON a.attrelid = t.oid AND a.attnum = k.attnum "
        "WHERE con.contype = 'p' AND t.relname = %s AND n.nspname = current_schema() ORDER BY k.ord",
        (table,)).fetchall()
    return (rows[0]["conname"] if rows else None), [r["attname"] for r in rows]


def migrate_doc_key(c):
    """문서 키 doc_id → (vol, doc_id) (https://github.com/reddol18/docflow/issues/50). documents 기본 키만 doc_id 하나였다
    (다른 표 edits·events·audit·review_time·inspection·doc_stage·stage_log는 처음부터 vol 칸을 함께 가진다).
    기본 키를 한 트랜잭션에서 바꾼다 — 행은 그대로 두므로 사람이 고친 값·검사·감사 기록이 그대로 남는다.
    이미 바뀌었으면 아무것도 안 한다 (카탈로그 한 번 읽기, 시작할 때마다). 바꿨으면 True.
    예전 키로 섞인 행은 시작 마무리(main.repair_documents)가 철의 그래프 상태대로 바로잡는다"""
    name, cols = _pk_columns(c, "documents")
    if cols == ["vol", "doc_id"]:
        return False
    with c.transaction():
        if name:
            c.execute(f'ALTER TABLE documents DROP CONSTRAINT "{name}"')  # 카탈로그에서 읽은 제약 이름
        c.execute("ALTER TABLE documents ADD PRIMARY KEY (vol, doc_id)")
    print(f"documents: 기본 키 {cols} → [vol, doc_id]. 여러 철에 나온 doc_id {shared_doc_ids(c)}개 — 섞인 행은 "
          "시작 마무리에서 철의 상태대로 바로잡는다 (documents: 바로잡음 로그)", flush=True)
    return True


def shared_doc_ids(c):
    """여러 철에 나온 doc_id 수 (이벤트 기준) — 예전 키(doc_id 하나)에서 충돌했을 수 있는 문서"""
    return c.execute("SELECT count(*) AS n FROM (SELECT doc_id FROM events WHERE doc_id IS NOT NULL "
                     "GROUP BY doc_id HAVING count(DISTINCT vol) > 1) t").fetchone()["n"]


DOC_KEY_MIGRATION = "doc_key_50"  # Qdrant 점 다시 매기기 + documents 행 바로잡기 (https://github.com/reddol18/docflow/issues/50)


def migration_done(name):
    with pool.connection() as c:
        return c.execute("SELECT 1 FROM schema_migrations WHERE name = %s", (name,)).fetchone() is not None


def mark_migration(name):
    with pool.connection() as c:
        c.execute("INSERT INTO schema_migrations (name) VALUES (%s) ON CONFLICT DO NOTHING", (name,))


AUDIT_KEEP = "1 year"  # 감사 로그 보존 기간 (2026-11-06 확정, README)


def prune_audit():
    """보존 기간이 지난 감사 로그 행을 지운다 — 시작할 때마다 (배포·재시작). 지운 행 수"""
    with pool.connection() as c:
        return c.execute("DELETE FROM audit WHERE ts < now() - %s::interval", (AUDIT_KEEP,)).rowcount


def init():
    init_db()
    q = qdrant()
    if not q.collection_exists(COLLECTION):
        from qdrant_client.models import Distance, PayloadSchemaType, VectorParams
        q.create_collection(COLLECTION, vectors_config=VectorParams(size=1536, distance=Distance.COSINE))
        q.create_payload_index(COLLECTION, "year", PayloadSchemaType.INTEGER)
        q.create_payload_index(COLLECTION, "vol", PayloadSchemaType.KEYWORD)
    elif not migration_done(DOC_KEY_MIGRATION):
        migrate_vectors()  # 예전 키(doc_id)의 점을 (vol, doc_id)로. 완료 표시는 main.startup_migrate가 행 바로잡기 뒤에 남긴다


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
    """화면용 — 시드·만든 사람 id는 내보내지 않는다"""
    with pool.connection() as c:
        return c.execute("SELECT vol, rate::float AS rate, fields100, flagged, unflagged, sample, created_at "
                         "FROM inspection_plan WHERE vol = %s", (vol,)).fetchone()


def add_review_submitter(vol, user_id):
    """검토를 제출한 사람 — 교차검사의 근거. 감사 로그(실패해도 넘어감)와 달리 실패하면 검토 제출도 실패한다"""
    if user_id is None:
        return
    with pool.connection() as c:
        c.execute("INSERT INTO review_submitters (vol, user_id) VALUES (%s,%s) ON CONFLICT DO NOTHING", (vol, user_id))


def inspection_rows(vol):
    with pool.connection() as c:
        return c.execute("SELECT i.doc_id, i.reason, i.inspected_at, i.fixed_fields, u.username AS inspected_by "
                         "FROM inspection i LEFT JOIN users u ON u.id = i.inspected_by WHERE i.vol = %s ORDER BY i.doc_id",
                         (vol,)).fetchall()


def inspection_row(vol, doc_id):
    with pool.connection() as c:
        return c.execute("SELECT * FROM inspection WHERE vol = %s AND doc_id = %s", (vol, doc_id)).fetchone()


_VOL_ENTRANTS = ("SELECT user_id FROM review_submitters WHERE vol = %(vol)s "
                 "UNION SELECT user_id FROM audit WHERE vol = %(vol)s AND action = 'review' AND result = 'ok' "
                 # review_submitters·audit 이전(https://github.com/reddol18/docflow/issues/28 이전)에 검토한 철: 이벤트의 사용자 이름
                 "UNION SELECT u.id FROM events e JOIN users u ON u.username = e.info->>'user' "
                 "WHERE e.vol = %(vol)s AND e.node = 'review' AND e.phase = 'submit'")


def entered(vol, user_id):
    """이 사용자가 입력 단계에 손댄 범위: (철 전체를 검토 제출했나, 칸을 고친 문서 id 집합). 교차검사용 — 한 번에 센다.
    실행 세대(stage_gen)로 거르지 않는다 (의도, https://github.com/reddol18/docflow/issues/86): 다시 돌리기 전에 입력을
    맡았던 사람도 계속 입력한 사람으로 본다 — 작업자 ≠ 검수자를 넓게(안전 쪽으로) 막는다. 단계 승인·반려 기록만 세대로 거른다"""
    with pool.connection() as c:
        whole = c.execute(f"SELECT 1 FROM ({_VOL_ENTRANTS}) t WHERE user_id = %(uid)s LIMIT 1",  # noqa: S608 — 상수 SQL
                          {"vol": vol, "uid": user_id}).fetchone() is not None
        docs = {r["doc_id"] for r in c.execute("SELECT DISTINCT doc_id FROM edits WHERE vol = %s AND user_id = %s "
                                                "AND source IN ('review','edit')", (vol, user_id)).fetchall()}
    return whole, docs


def claim_inspection(vol, doc_id, user_id):
    """검사할 문서를 먼저 차지한다 (동시에 두 사람이 고치지 않게). 이미 누가 차지했으면 False.
    판단은 inspected_at으로 — 사용자 id가 없을 때(인증 끈 개발 모드)도 두 번 검사되지 않게"""
    with pool.connection() as c:
        cur = c.execute("UPDATE inspection SET inspected_by = %s, inspected_at = now() "
                        "WHERE vol = %s AND doc_id = %s AND inspected_at IS NULL", (user_id, vol, doc_id))
        return cur.rowcount == 1


def inspected_fields(vol, doc_id):
    """검사 단계에서 고친 칸 (이력 기준)"""
    with pool.connection() as c:
        return sorted(r["field"] for r in c.execute(
            "SELECT DISTINCT field FROM edits WHERE vol = %s AND doc_id = %s AND source = 'inspect'", (vol, doc_id)).fetchall())


def reset_inspection(vol):
    """철을 처음부터 다시 돌릴 때 예전 검사 목록을 지운다. 지운 것이 있으면 True"""
    with pool.connection() as c, c.transaction():
        c.execute("DELETE FROM inspection WHERE vol = %s", (vol,))
        return c.execute("DELETE FROM inspection_plan WHERE vol = %s", (vol,)).rowcount > 0


def finish_inspection(vol, doc_id, fixed_fields):
    with pool.connection() as c:
        c.execute("UPDATE inspection SET fixed_fields = %s WHERE vol = %s AND doc_id = %s", (fixed_fields, vol, doc_id))


def release_inspection(vol, doc_id):
    """고치다 실패하면 차지를 푼다"""
    with pool.connection() as c:
        c.execute("UPDATE inspection SET inspected_by = NULL, inspected_at = NULL, fixed_fields = NULL "
                  "WHERE vol = %s AND doc_id = %s", (vol, doc_id))


def inspection_stats(start=None, end=None):
    """철별 표본 검사 결과: 표본 수, 검사 끝난 표본 n, 그중 고친 k, 추정 오류율 k/n.
    start·end(시각)를 주면 그 기간에 검사한 것만 센다 (리포트 기간과 맞춤). 끝나지 않은(차지만 한) 검사는 세지 않는다"""
    done = "i.reason = 'sample' AND i.fixed_fields IS NOT NULL"
    period = " AND i.inspected_at >= %(start)s AND i.inspected_at < %(end)s" if start and end else ""
    with pool.connection() as c:
        rows = c.execute("SELECT p.vol, p.sample, p.rate::float AS rate, "  # noqa: S608 — 상수 조각만 끼움
                         f"count(*) FILTER (WHERE {done}{period}) AS inspected, "
                         f"count(*) FILTER (WHERE {done}{period} AND cardinality(i.fixed_fields) > 0) AS fixed "
                         "FROM inspection_plan p LEFT JOIN inspection i ON i.vol = p.vol GROUP BY p.vol, p.sample, p.rate "
                         "ORDER BY p.vol", {"start": start, "end": end}).fetchall()
    for r in rows:
        r["error_rate"] = round(r["fixed"] / r["inspected"], 4) if r["inspected"] else None
    return rows


def stage_of_source(source):
    """수정 이력 source → 단계: 입력(검토 제출·색인 뒤 수정) 'entry' / 표본 검사 'inspect'
    (https://github.com/reddol18/docflow/issues/10) / 설정 흐름의 검수 단계 'stage:<단계 이름>' → 그 단계 이름
    (https://github.com/reddol18/docflow/issues/86). 수정 이력 화면과 검수 오류목록이 이것 하나를 쓴다"""
    if source == "inspect":
        return "inspect"
    if source.startswith("stage:"):
        return source[len("stage:"):]
    return "entry"


# 사람 칸: sso — SSO 사용자는 이름을 제공자가 정하므로 local 계정과 이름이 같을 수 있다 → 구별해서 보여 준다.
# 수정 이력 화면과 검수 오류목록(표시 이름 모드)이 같은 칸을 쓴다
_USER_COLS = "u.username, u.name, u.issuer IS NOT NULL AS sso"


def edit_history(vol, doc_id):
    with pool.connection() as c:
        rows = c.execute(f"SELECT e.ts, e.field, e.old, e.new, e.source, {_USER_COLS} FROM edits e "  # noqa: S608 — 상수
                         "LEFT JOIN users u ON u.id = e.user_id WHERE e.vol = %s AND e.doc_id = %s ORDER BY e.id",
                         (vol, doc_id)).fetchall()
    for r in rows:
        r["stage"] = stage_of_source(r["source"])
    return rows


def add_export(vol, problems, user_id):
    """납품 출력 기록 (감사 로그와 따로 — 감사 로그 보존 기간 정리가 일괄 삭제의 근거를 지우지 않게)"""
    with pool.connection() as c:
        c.execute("INSERT INTO exports (vol, problems, user_id) VALUES (%s,%s,%s)", (vol, problems, user_id))


def last_export(vol, stage_moves=False):
    """마지막 출력이 철의 지금 내용과 같은가. None(내보낸 적 없음) 또는 {ts, problems, current}.
    current: 출력 뒤에 색인이 바뀌지 않았고(documents.updated_at — 실행·검토·수정·검사가 모두 여기를 바꾼다)
    쪽을 다시 올리거나 다시 돌리지 않았음(감사 로그 upload·run·resume).
    stage_moves: 검토 단계 이동도 내용 변화로 본다 — 출력 전 검증이 미확정 문서를 셀 때 (흐름 설정 count_unconfirmed)"""
    moves = ", (SELECT max(ts) FROM stage_log WHERE vol = %(v)s)" if stage_moves else ""
    with pool.connection() as c:
        e = c.execute("SELECT ts, problems FROM exports WHERE vol = %s ORDER BY id DESC LIMIT 1", (vol,)).fetchone()
        if not e:
            return None
        changed = c.execute(
            "SELECT greatest((SELECT max(updated_at) FROM documents WHERE vol = %(v)s), "  # noqa: S608 — 상수 조각만 끼움
            "(SELECT max(ts) FROM audit WHERE vol = %(v)s AND action IN ('upload','run','resume') AND result = 'ok')"
            f"{moves}) AS t", {"v": vol}).fetchone()["t"]
    return {**e, "current": changed is None or changed < e["ts"]}


# ---------- 검토 단계 (https://github.com/reddol18/docflow/issues/86) ----------
# doc_stage: 문서가 지금 어느 단계·몇 번째 차례(round)에 있나. 행이 없으면 흐름의 처음 단계 (app/workflow.py initial)
# stage_log: 단계 승인·반려 기록 (누가·언제·어느 단계·차례·어디로·사유·고친 칸) — 오류목록 내보내기의 원천
# stage_gen: 철의 실행 세대. 철을 처음부터 다시 돌리면(/run) 하나 늘고, 작업자 ≠ 검수자·단계 기록은 지금 세대만 본다
# 문서 하나를 바꾸는 일(입력 수정·검사·단계 승인/반려)은 doc_lock 안에서 확인하고 쓴다 — 확인과 쓰기 사이에 끼어들지 못하게
_GEN = "coalesce((SELECT gen FROM stage_gen WHERE vol = %(v)s), 0)"
_SINCE = "coalesce((SELECT since FROM stage_gen WHERE vol = %(v)s), '-infinity'::timestamptz)"


LOCK_WAIT = float(os.environ.get("DOC_LOCK_WAIT_SEC", "10"))  # 잠금을 이만큼 기다려도 못 얻으면 LockBusy (main이 409로)


class LockBusy(Exception):
    """다른 요청이 같은 문서·철을 처리 중이라 LOCK_WAIT 안에 잠금을 얻지 못함"""


def _lock_key(name):
    return int.from_bytes(hashlib.sha256(name.encode()).digest()[:8], "big", signed=True)


# 프로세스 안 잠금 (키를 256칸에 나눠 담는다). RLock: 한 요청이 문서 잠금 뒤 철 잠금을 같은 칸에서 다시 잡을 수 있게
_STRIPES = [threading.RLock() for _ in range(256)]
_held = threading.local()  # 이 스레드(요청)가 쥔 잠금용 연결 — 겹친 잠금은 같은 연결에 건다 (요청마다 잠금 연결 하나)


@contextmanager
def _pg_lock(c, key, deadline):
    """세션 advisory lock. 마감까지 pg_try_advisory_lock을 되풀이한다. 풀 때 실패하면 연결을 닫아 서버가 풀게 한다"""
    while not c.execute("SELECT pg_try_advisory_lock(%s) AS ok", (key,)).fetchone()["ok"]:
        if time.monotonic() >= deadline:
            raise LockBusy("잠금 대기 시간 초과")
        time.sleep(0.05)
    try:
        yield
    finally:
        try:
            c.execute("SELECT pg_advisory_unlock(%s)", (key,))
        except Exception:
            c.close()  # 닫힌 연결은 풀이 버린다 → 세션이 끝나며 잠금이 풀린다


@contextmanager
def named_lock(name):
    """이름 잠금: 프로세스 안 잠금 + Postgres 세션 advisory lock (다른 프로세스·Pod 사이). 기다림은 LOCK_WAIT까지.
    확인과 쓰기가 여러 연결에 걸치므로(그래프 상태·documents·edits·stage_log) 한 트랜잭션으로 묶지 않고, 잠금을 쥔 동안
    다른 요청이 같은 대상을 확인·쓰지 못하게 한다. 잠금용 연결(lock_pool)은 따로 둔다 — 같은 풀이면 잠금 대기가 쓰기 연결을
    다 잡아 멈출 수 있다. 트랜잭션을 열어 두지 않는다(xact lock이 아님): 트랜잭션 중인 연결이 있으면 다른 연결을 멈추는
    시험 서버(PGlite)가 있다. 잠금 순서는 문서 → 철로 고정한다 (교착 없게)"""
    key = _lock_key(name)
    deadline = time.monotonic() + LOCK_WAIT
    stripe = _STRIPES[key % len(_STRIPES)]
    if not stripe.acquire(timeout=LOCK_WAIT):
        raise LockBusy("잠금 대기 시간 초과")
    try:
        outer = getattr(_held, "conn", None)
        if outer is not None:
            with _pg_lock(outer, key, deadline):
                yield
            return
        try:
            c = lock_pool.getconn(timeout=max(0.1, deadline - time.monotonic()))
        except PoolTimeout:
            raise LockBusy("잠금 연결 대기 시간 초과") from None
        _held.conn = c
        try:
            with _pg_lock(c, key, deadline):
                yield
        finally:
            _held.conn = None
            lock_pool.putconn(c)
    finally:
        stripe.release()


def doc_lock(vol, doc_id):
    """문서 하나를 바꾸는 일(입력 수정·검사·단계 승인/반려)의 확인과 쓰기"""
    return named_lock(f"docflow-doc/{vol}/{doc_id}")


def vol_lock(vol):
    """철의 그래프 상태 읽고 쓰기(get_state → update_state) — 같은 철 다른 문서의 동시 수정이 서로 덮어쓰지 않게"""
    return named_lock(f"docflow-vol/{vol}")


def stage_rows(vol):
    with pool.connection() as c:
        return {r["doc_id"]: r for r in c.execute(
            "SELECT doc_id, stage, round, last_reason, rejected_from FROM doc_stage WHERE vol = %s",
            (vol,)).fetchall()}


def ensure_stage(vol, doc_id, stage):
    """행이 없으면 만든다 (이미 있으면 그대로). 지금 행"""
    with pool.connection() as c:
        c.execute("INSERT INTO doc_stage (vol, doc_id, stage) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING", (vol, doc_id, stage))
        return c.execute("SELECT * FROM doc_stage WHERE vol = %s AND doc_id = %s", (vol, doc_id)).fetchone()


def current_gen(vol):
    with pool.connection() as c:
        return c.execute(f"SELECT {_GEN} AS g", {"v": vol}).fetchone()["g"]  # noqa: S608 — 상수 조각만 끼움


def _same_gen(c, vol, gen):
    """트랜잭션 안에서 세대가 확인 때와 같은가 (stage_gen 행을 FOR SHARE로 잡아 /run의 세대 올리기와 엇갈리지 않게)"""
    r = c.execute("SELECT gen FROM stage_gen WHERE vol = %s FOR SHARE", (vol,)).fetchone()
    return (r["gen"] if r else 0) == gen


def move_stage(vol, doc_id, stage, rnd, action, to_stage, to_round, user_id, reason=None, fixed=None, gen=None,
               init=None):
    """단계 이동과 기록을 한 트랜잭션으로 (doc_lock 안에서 부른다). 옮겼으면 True. 옮기지 않는 경우(False):
    확인 때 읽은 세대(gen)가 바뀜(그 사이 /run이 다시 돌림), 문서의 단계·차례가 바뀜.
    행이 없던 문서는 같은 트랜잭션에서 처음 단계(init) 행을 만든다. 반려면 사유와 반려한 단계를 문서에 남기고,
    반려한 단계가 승인하거나 확정되면 지운다 (기록은 stage_log에 남음)"""
    try:
        with pool.connection() as c, c.transaction():
            if gen is not None and not _same_gen(c, vol, gen):
                return False
            if init is not None:
                c.execute("INSERT INTO doc_stage (vol, doc_id, stage) VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                          (vol, doc_id, init))
            n = c.execute(
                "UPDATE doc_stage SET stage = %(to)s, round = %(tr)s, "
                "last_reason = CASE WHEN %(rej)s THEN %(why)s WHEN rejected_from = %(s)s OR %(to)s = %(fin)s THEN NULL "
                "ELSE last_reason END, "
                "rejected_from = CASE WHEN %(rej)s THEN %(s)s WHEN rejected_from = %(s)s OR %(to)s = %(fin)s THEN NULL "
                "ELSE rejected_from END, updated_at = now() "
                "WHERE vol = %(v)s AND doc_id = %(d)s AND stage = %(s)s AND round = %(r)s",
                {"to": to_stage, "tr": to_round, "rej": action == "reject", "why": reason, "fin": CONFIRMED,
                 "v": vol, "d": doc_id, "s": stage, "r": rnd}).rowcount
            if n != 1:
                raise _Rollback  # 만든 처음 단계 행까지 되돌린다
            add_stage_log(vol, doc_id, stage, rnd, action, to_stage, user_id, reason, fixed, conn=c)
    except _Rollback:
        return False
    return True


class _Rollback(Exception):
    """move_stage 트랜잭션을 되돌리는 신호"""


def confirm_sample(vol, doc_id, stage, rnd, user_id, fixed, gen):
    """표본 검사가 끝난 문서를 확정으로 옮기고 기록한다 (한 트랜잭션). 행이 없던 문서(기본 흐름)는 만든다.
    세대가 바뀌었거나(/run) 이 사용자의 검사 차지가 없어졌으면 쓰지 않고 False"""
    with pool.connection() as c, c.transaction():
        if not _same_gen(c, vol, gen):
            return False
        if not c.execute("SELECT 1 FROM inspection WHERE vol = %s AND doc_id = %s AND inspected_at IS NOT NULL "
                         "AND inspected_by IS NOT DISTINCT FROM %s", (vol, doc_id, user_id)).fetchone():
            return False
        c.execute("INSERT INTO doc_stage (vol, doc_id, stage, round) VALUES (%(v)s, %(d)s, %(fin)s, %(r)s) "
                  "ON CONFLICT (vol, doc_id) DO UPDATE SET stage = %(fin)s, last_reason = NULL, rejected_from = NULL, "
                  "updated_at = now() WHERE doc_stage.stage = %(s)s",
                  {"v": vol, "d": doc_id, "fin": CONFIRMED, "r": rnd, "s": stage})
        add_stage_log(vol, doc_id, stage, rnd, "approve", CONFIRMED, user_id, None, fixed, conn=c)
    return True


def stage_fixed_fields(vol, doc_id, stage, sources):
    """이 단계 차례에 고친 칸 (이력 기준): 문서가 지금 세대에 이 단계로 들어온 뒤(마지막 stage_log to_stage, 없으면
    세대 시작) 이 단계 source(입력은 edit, 검수는 stage:<단계>)로 들어온 수정. 앞선 승인이 칸 반영 뒤 실패해
    다시 승인할 때 차이가 없어도 고친 칸이 기록에서 빠지지 않게"""
    with pool.connection() as c:
        return sorted(r["field"] for r in c.execute(
            "SELECT DISTINCT field FROM edits WHERE vol = %(v)s AND doc_id = %(d)s AND source = ANY(%(src)s) "  # noqa: S608
            f"AND ts >= greatest({_SINCE}, (SELECT max(ts) FROM stage_log WHERE vol = %(v)s AND doc_id = %(d)s "
            f"AND to_stage = %(s)s AND gen = {_GEN}))",
            {"v": vol, "d": doc_id, "s": stage, "src": list(sources)}).fetchall())


def backfill_inspected(stage):
    """예전 판에서 검사가 끝난 문서(단계 행 없음)를 확정 행으로, 승인 기록(stage_log)도 함께 — 시작할 때마다, 남은 것만.
    옮긴 행 수. 예전 판에서 끝나지 않은 검사 차지(inspected_at만 있고 fixed_fields 없음)는 건드리지 않는다 (README)"""
    with pool.connection() as c, c.transaction():
        return c.execute(
            "WITH moved AS (INSERT INTO doc_stage (vol, doc_id, stage) SELECT vol, doc_id, %(fin)s FROM inspection "
            "WHERE inspected_at IS NOT NULL AND fixed_fields IS NOT NULL ON CONFLICT (vol, doc_id) "
            "DO UPDATE SET stage = EXCLUDED.stage WHERE doc_stage.stage = %(s)s RETURNING vol, doc_id) "
            "INSERT INTO stage_log (vol, doc_id, stage, round, action, to_stage, user_id, fixed_fields, gen, ts) "
            "SELECT i.vol, i.doc_id, %(s)s, 1, 'approve', %(fin)s, i.inspected_by, i.fixed_fields, "
            "coalesce((SELECT gen FROM stage_gen g WHERE g.vol = i.vol), 0), i.inspected_at "
            "FROM moved m JOIN inspection i ON i.vol = m.vol AND i.doc_id = m.doc_id",
            {"fin": CONFIRMED, "s": stage}).rowcount


def add_stage_log(vol, doc_id, stage, rnd, action, to_stage, user_id, reason=None, fixed=None, conn=None):
    sql = ("INSERT INTO stage_log (vol, doc_id, stage, round, action, to_stage, user_id, reason, fixed_fields, gen) "  # noqa: S608
           f"VALUES (%(v)s,%(d)s,%(s)s,%(r)s,%(a)s,%(to)s,%(u)s,%(why)s,%(fx)s,{_GEN})")
    args = {"v": vol, "d": doc_id, "s": stage, "r": rnd, "a": action, "to": to_stage, "u": user_id, "why": reason,
            "fx": fixed}
    if conn is not None:
        conn.execute(sql, args)
        return
    with pool.connection() as c:
        c.execute(sql, args)


def stage_actors(vol, user_id):
    """이 사용자가 지금 세대에 승인·반려한 단계 {doc_id: {단계 이름}} — 작업자 ≠ 검수자 확인용 (검수 단계 수정 이력 포함)"""
    out = {}
    args = {"v": vol, "u": user_id}
    with pool.connection() as c:
        for r in c.execute("SELECT DISTINCT doc_id, stage FROM stage_log "  # noqa: S608 — 상수 조각만 끼움
                           f"WHERE vol = %(v)s AND user_id = %(u)s AND gen = {_GEN}", args).fetchall():
            out.setdefault(r["doc_id"], set()).add(r["stage"])
        for r in c.execute("SELECT DISTINCT doc_id, substr(source, 7) AS stage FROM edits "  # noqa: S608
                           f"WHERE vol = %(v)s AND user_id = %(u)s AND source LIKE 'stage:%%' AND ts >= {_SINCE}",
                           args).fetchall():
            out.setdefault(r["doc_id"], set()).add(r["stage"])
    return out


def inspected_docs_by(vol, user_id):
    """검사(표본 단계)를 맡은 문서 — 차지했거나 지금 세대에 검사로 칸을 고친 문서"""
    with pool.connection() as c:
        return {r["doc_id"] for r in c.execute(
            "SELECT doc_id FROM inspection WHERE vol = %(v)s AND inspected_by = %(u)s "  # noqa: S608
            "UNION SELECT doc_id FROM edits WHERE vol = %(v)s AND user_id = %(u)s AND source = 'inspect' "
            f"AND ts >= {_SINCE}", {"v": vol, "u": user_id}).fetchall()}


def stage_history(vol, doc_id):
    """문서의 지금 세대 단계 기록 — 화면과 오류목록 내보내기용 (사유·고친 칸 포함, 사용자 id는 이름으로).
    예전 세대 기록은 표에 남아 있다 (gen)"""
    with pool.connection() as c:
        return c.execute("SELECT s.ts, s.stage, s.round, s.action, s.to_stage, s.reason, s.fixed_fields, "  # noqa: S608
                         "u.username, u.name FROM stage_log s LEFT JOIN users u ON u.id = s.user_id "
                         f"WHERE s.vol = %(v)s AND s.doc_id = %(d)s AND s.gen = {_GEN} ORDER BY s.id",
                         {"v": vol, "d": doc_id}).fetchall()


def gen_edits(vol):
    """철의 지금 세대 수정 이력 (stage_gen.since 뒤, id 순) — 검수 오류목록용 (https://github.com/reddol18/docflow/issues/88).
    user_id는 가린 ID를 만드는 데만 쓰고 파일에 싣지 않는다. 사람 칸(_USER_COLS)은 수정 이력 화면과 같다"""
    with pool.connection() as c:
        return c.execute("SELECT e.id, e.ts, e.doc_id, e.field, e.old, e.new, e.source, e.user_id, "  # noqa: S608
                         f"{_USER_COLS} FROM edits e LEFT JOIN users u ON u.id = e.user_id "
                         f"WHERE e.vol = %(v)s AND e.ts >= {_SINCE} ORDER BY e.id", {"v": vol}).fetchall()


def gen_stage_log(vol):
    """철의 지금 세대 단계 승인·반려 기록 (id 순) — 검수 오류목록용. 예전 세대(다시 돌리기 전) 기록은 빼고 본다"""
    with pool.connection() as c:
        return c.execute("SELECT s.id, s.ts, s.doc_id, s.stage, s.round, s.action, s.to_stage, s.reason, "  # noqa: S608
                         f"s.fixed_fields, s.user_id, {_USER_COLS} FROM stage_log s "
                         f"LEFT JOIN users u ON u.id = s.user_id WHERE s.vol = %(v)s AND s.gen = {_GEN} ORDER BY s.id",
                         {"v": vol}).fetchall()


def reset_stages(vol):
    """철을 처음부터 다시 돌릴 때: 실행 세대를 하나 올리고 단계 위치를 지운다 (기록 stage_log는 세대와 함께 남긴다).
    지운 위치 행 수"""
    with pool.connection() as c, c.transaction():
        c.execute("INSERT INTO stage_gen (vol, gen, since) VALUES (%s, 1, clock_timestamp()) ON CONFLICT (vol) "
                  "DO UPDATE SET gen = stage_gen.gen + 1, since = clock_timestamp()", (vol,))
        return c.execute("DELETE FROM doc_stage WHERE vol = %s", (vol,)).rowcount


# 납품 뒤 지우는 사업 데이터 (https://github.com/reddol18/docflow/issues/12): 값·원문이 든 표.
# 남기는 표: usage(비용)·audit(감사)·review_time(작업 시간)·exports(출력 기록) — 개수·시각만 있고 값은 없다
PURGE_TABLES = ("documents", "edits", "events", "inspection", "inspection_plan", "review_submitters",
                "doc_stage", "stage_log")  # 단계 기록에는 반려 사유(사람이 쓴 글)가 있다


def purge_volume(vol, delete_thread):
    """철 하나의 사업 데이터를 지운다: Qdrant 벡터 → 파일 → LangGraph 체크포인트 → Postgres 행(한 트랜잭션).
    바깥 저장소를 먼저 지운다 — 중간에 실패해도 출력 기록(exports)과 문서 행이 남아 다시 부르면 이어서 지운다.
    체크포인트는 저장기의 delete_thread로 (내부 표 목록을 이 코드가 따라가지 않게). 지운 개수를 돌려준다"""
    out = {"vectors": delete_vectors(vol)}
    files = 0
    for kind in ("pages", "ocr", "crops"):
        d = under(kind, vol)
        if d.exists():
            files += sum(1 for p in d.rglob("*") if p.is_file())
            shutil.rmtree(d)
    out["files"] = files
    delete_thread(vol)
    with pool.connection() as c, c.transaction():
        for t in PURGE_TABLES:
            out[t] = c.execute(f"DELETE FROM {t} WHERE vol = %s", (vol,)).rowcount  # noqa: S608 — 고정된 표 이름
    return out


def save_documents(docs, schema=None):
    """칸 값은 fields jsonb 그대로, 검색용 열(search_text·year)은 스키마 search 설정으로 만든다"""
    schema = schema or fields.SCHEMA
    with pool.connection() as c:
        for d in docs:
            _upsert_doc(c, d, schema)


def _upsert_doc(c, d, schema):
    f = d["fields"]
    c.execute(
        """INSERT INTO documents (doc_id, vol, pages, fields, search_text, year, flags, reviewed, updated_at)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s, now())
           ON CONFLICT (vol, doc_id) DO UPDATE SET pages=EXCLUDED.pages, fields=EXCLUDED.fields,
             search_text=EXCLUDED.search_text, year=EXCLUDED.year, flags=EXCLUDED.flags,
             reviewed=EXCLUDED.reviewed, updated_at=now()""",
        (d["doc_id"], d["vol"], d["pages"], json.dumps(f, ensure_ascii=False),
         fields.search_text(schema, f), fields.year_of(schema, f),
         json.dumps(d["flags"], ensure_ascii=False), d.get("reviewed", False)))
    if d.get("edited"):
        c.execute("UPDATE documents SET edited = true WHERE vol = %s AND doc_id = %s", (d["vol"], d["doc_id"]))


def index_time(vol):
    """철을 처음 색인한 시각 (index 노드 끝 이벤트). 없으면 None"""
    with pool.connection() as c:
        return c.execute("SELECT min(ts) AS t FROM events WHERE vol = %s AND node = 'index' AND phase = 'end'",
                         (vol,)).fetchone()["t"]


def restore_documents(vol, docs, created_at, schema=None):
    """documents 행을 그래프 상태 그대로 다시 쓴다 (옮기기의 바로잡기 — https://github.com/reddol18/docflow/issues/50).
    save_documents와 달리 edited도 상태대로 (켜고 끈다), 새로 만드는 행의 created_at은 철의 색인 시각
    (리포트 '색인 문서 수'가 바로잡은 날에 부풀지 않게; 모르면 NULL — 리포트가 세지 않는다). 있던 행의 created_at은 그대로.
    있던 행의 updated_at도 그대로 둔다 — 바로잡기는 출력 내용(그래프 상태)을 바꾸지 않으므로, 내보낸 철이 '내보낸 뒤
    바뀜'(last_export current False → 일괄 삭제 409)이 되지 않게"""
    schema = schema or fields.SCHEMA
    with pool.connection() as c, c.transaction(), c.cursor() as cur:
        cur.executemany(
            """INSERT INTO documents (doc_id, vol, pages, fields, search_text, year, flags, reviewed, edited, created_at,
                                      updated_at)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
               ON CONFLICT (vol, doc_id) DO UPDATE SET pages=EXCLUDED.pages, fields=EXCLUDED.fields,
                 search_text=EXCLUDED.search_text, year=EXCLUDED.year, flags=EXCLUDED.flags,
                 reviewed=EXCLUDED.reviewed, edited=EXCLUDED.edited""",
            [(d["doc_id"], vol, d["pages"], json.dumps(d["fields"], ensure_ascii=False),
              fields.search_text(schema, d["fields"]), fields.year_of(schema, d["fields"]),
              json.dumps(d.get("flags"), ensure_ascii=False), d.get("reviewed", False), bool(d.get("edited")),
              created_at) for d in docs])


def save_edit(vol, d, flag_items, schema=None):
    """문서 수정 저장 — 고친 문서 d는 통째로, 철 안 중복이 바뀐 다른 문서는 검토 표시만 [(doc_id, flags)], 한 트랜잭션으로
    (https://github.com/reddol18/docflow/issues/87). 다른 문서의 칸 값·검토 여부는 건드리지 않는다 (그 문서를 동시에 고친 값을
    낡은 값으로 덮지 않게). 부른 쪽(main._edit_document)이 철 잠금을 쥔다"""
    schema = schema or fields.SCHEMA
    with pool.connection() as c, c.transaction():
        _upsert_doc(c, d, schema)
        for doc_id, flags in flag_items:
            c.execute("UPDATE documents SET flags = %s, updated_at = now() WHERE vol = %s AND doc_id = %s",
                      (json.dumps(flags, ensure_ascii=False), vol, doc_id))


def keyword_search(words, year_from=None, year_to=None, limit=20):
    """검색 칸(스키마 search.keyword — 김해는 제목·수신·문서번호)에 든 낱말 수로 점수를 매긴다
    (형태소 분석 없는 단순 부분 일치)."""
    if not words:
        return []
    hay = "coalesce(search_text,'')"
    score = " + ".join(f"(CASE WHEN {hay} ILIKE %s THEN 1 ELSE 0 END)" for _ in words)
    sql = f"SELECT vol, doc_id, ({score}) AS score FROM documents WHERE ({score}) > 0"  # noqa: S608 — 자리표시자만 끼움, 값은 매개변수
    args = [f"%{w}%" for w in words] * 2
    if year_from:
        sql += " AND year >= %s"
        args.append(int(year_from))
    if year_to:
        sql += " AND year <= %s"
        args.append(int(year_to))
    sql += " ORDER BY score DESC, doc_id, vol LIMIT %s"
    args.append(limit)
    with pool.connection() as c:
        return c.execute(sql, args).fetchall()


def doc_key(r):
    """문서 키 (철, doc_id) — 같은 쪽 이름이 다른 철에도 있을 수 있다 (https://github.com/reddol18/docflow/issues/50)"""
    return r["vol"], r["doc_id"]


def get_documents(keys):
    """keys = [(vol, doc_id)] → {(vol, doc_id): 행}"""
    keys = list(keys)
    if not keys:
        return {}
    with pool.connection() as c:
        rows = c.execute("SELECT d.* FROM documents d JOIN unnest(%s::text[], %s::text[]) k(vol, doc_id) "
                         "ON d.vol = k.vol AND d.doc_id = k.doc_id",
                         ([v for v, _ in keys], [d for _, d in keys])).fetchall()
    return {doc_key(r): r for r in rows}


_q = None


def qdrant():
    global _q
    if _q is None:
        from qdrant_client import QdrantClient
        _q = QdrantClient(url=QDRANT_URL, timeout=30)
    return _q


def point_id(vol, doc_id):
    """Qdrant 점 id — (철, doc_id)에서 만든다 (https://github.com/reddol18/docflow/issues/50).
    철·doc_id에는 '/'가 들어가지 않으므로(main.VOL_RX·쪽 파일 이름 규칙) 이음 글자로 겹치지 않는다"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"docflow/doc/{vol}/{doc_id}"))


def legacy_point_id(doc_id):
    """#50 이전 점 id (doc_id만으로) — 옮기기와 시험에서만 쓴다"""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, "docflow/" + doc_id))


class VectorMigrationError(RuntimeError):
    """Qdrant 점을 안전하게 옮기지 못함 — 시작을 멈추고 무엇을 할지 알린다"""


def migrate_vectors(batch=256):
    """Qdrant 점 id를 doc_id → (vol, doc_id)로 다시 매긴다 (https://github.com/reddol18/docflow/issues/50).
    벡터를 다시 만들지 않는다 (임베딩 비용·LLM 호출 없음): payload의 vol·doc_id로 새 id를 셈해, id가 다른 점만
    벡터·payload를 그대로 새 id로 넣고(wait) 그 다음에 옛 id를 지운다. 중간에 멈춰도 다시 돌리면 이어서 끝난다
    (새 id 넣기는 같은 값 덮어쓰기, 옛 id 지우기는 없는 id면 그냥 지나감). 시작할 때마다 — 옮길 것이 없으면 id만 훑는다.
    payload에 vol·doc_id가 없는 점이 있으면 아무것도 옮기지 않고 VectorMigrationError (조용히 버리지 않는다). 옮긴 점 수"""
    from qdrant_client.models import PointIdsList, PointStruct
    q = qdrant()
    moves, bad, offset = [], [], None
    while True:
        pts, offset = q.scroll(COLLECTION, limit=batch, offset=offset, with_payload=["vol", "doc_id"],
                               with_vectors=False)
        for p in pts:
            pl = p.payload or {}
            if not pl.get("vol") or not pl.get("doc_id"):
                bad.append(str(p.id))
            elif str(p.id) != point_id(pl["vol"], pl["doc_id"]):
                moves.append(p.id)
        if offset is None:
            break
    if bad:
        raise VectorMigrationError(
            f"Qdrant '{COLLECTION}'에 vol·doc_id payload가 없는 점 {len(bad)}개 (예: {bad[:3]}) — 문서 키(vol, doc_id)로 "
            "옮길 수 없어 시작을 멈춘다. 그 점들을 지우거나(그 철을 다시 돌리면 다시 만든다) 컬렉션을 백업한 뒤 지우고 "
            "철을 다시 돌린다 (https://github.com/reddol18/docflow/issues/50)")
    if not moves:
        return 0
    before = q.count(COLLECTION, exact=True).count
    for i in range(0, len(moves), batch):
        old = q.retrieve(COLLECTION, ids=moves[i:i + batch], with_payload=True, with_vectors=True)
        q.upsert(COLLECTION, wait=True, points=[
            PointStruct(id=point_id(p.payload["vol"], p.payload["doc_id"]), vector=p.vector, payload=p.payload)
            for p in old])
        q.delete(COLLECTION, points_selector=PointIdsList(points=[p.id for p in old]), wait=True)
    after = q.count(COLLECTION, exact=True).count
    print(f"qdrant: 점 {len(moves)}개 id를 (vol, doc_id)로 옮김 (점 수 {before} → {after})", flush=True)
    return len(moves)


def upsert_vectors(docs, vectors, schema=None):
    """payload: doc_id·vol·year(연도 필터) + 보이는 칸 값 (숨김 칸은 넣지 않는다)"""
    from qdrant_client.models import PointStruct
    schema = schema or fields.SCHEMA
    pts = []
    for d, v in zip(docs, vectors):
        f = d["fields"]
        pts.append(PointStruct(id=point_id(d["vol"], d["doc_id"]), vector=v, payload={
            "doc_id": d["doc_id"], "vol": d["vol"], "year": fields.year_of(schema, f),
            "fields": {k: f.get(k, "") for k in fields.shown(schema)}}))
    qdrant().upsert(COLLECTION, points=pts)


def _vol_filter(vol):
    from qdrant_client.models import FieldCondition, Filter, MatchValue
    return Filter(must=[FieldCondition(key="vol", match=MatchValue(value=vol))])


def count_vectors(vol):
    return qdrant().count(COLLECTION, count_filter=_vol_filter(vol), exact=True).count


def delete_vectors(vol):
    """철의 벡터를 모두 지운다. 지운 개수(지우기 전 센 값)"""
    from qdrant_client.models import FilterSelector
    n = count_vectors(vol)
    if n:
        qdrant().delete(COLLECTION, points_selector=FilterSelector(filter=_vol_filter(vol)), wait=True)
    return n


def delete_doc_vectors(vol, doc_ids):
    """철 vol의 문서들의 벡터를 지운다 (LLM_PROVIDER=off로 다시 색인·수정한 문서 — 예전 값의 벡터를 남기지 않는다)"""
    from qdrant_client.models import PointIdsList
    if doc_ids:
        qdrant().delete(COLLECTION, points_selector=PointIdsList(points=[point_id(vol, d) for d in doc_ids]), wait=True)


def vector_search(vec, year_from=None, year_to=None, limit=20):
    from qdrant_client.models import FieldCondition, Filter, Range
    flt = None
    if year_from or year_to:
        flt = Filter(must=[FieldCondition(key="year", range=Range(gte=year_from, lte=year_to))])
    res = qdrant().query_points(COLLECTION, query=vec, query_filter=flt, limit=limit)
    return [{"vol": p.payload["vol"], "doc_id": p.payload["doc_id"], "score": p.score} for p in res.points]


def missing_vectors(keys):
    """(vol, doc_id) 중 Qdrant 점이 없는 것 — 시작 때 예전 키 충돌로 잃은 벡터를 세는 데 쓴다"""
    keys = list(keys)
    have = set()
    for i in range(0, len(keys), 256):
        have |= {str(p.id) for p in qdrant().retrieve(COLLECTION, ids=[point_id(v, d) for v, d in keys[i:i + 256]],
                                                      with_payload=False, with_vectors=False)}
    return [k for k in keys if point_id(*k) not in have]
