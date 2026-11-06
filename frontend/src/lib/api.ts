// docflow API 타입과 호출. 모든 요청은 /api/* (next.config.ts rewrites → docflow).

export type Stage = "new" | "queued" | "running" | "review" | "done" | "stopped";

export type Usage = { kind: string; calls: number; tin: number; tout: number; usd: number };

export type VolumeSummary = {
  vol: string;
  stage: Stage;
  running: string | null;
  next: string[];
  waiting_review: boolean;
  pages: number;
  ocr_done: number;
  uploaded: number;
  documents: number;
  orphans: string[];
  flagged_documents: number;
  indexed: number | null;
  usage: Usage[];
  spent_today_usd: number;
};

// 색인 칸은 서버의 스키마(GET /schema, app/field_schemas/*.yaml)가 정한다 (https://github.com/reddol18/docflow/issues/9)
export type Fields = Record<string, string>;
export type FieldDef = {
  name: string;
  label: string;
  level: "volume" | "document";
  method: string;
  editable: boolean;
  flag_key: string; // 이 칸의 검토 표시가 붙는 키 (김해 문서번호 → serial)
  isad: string | null;
};
export type Schema = { name: string; fields: FieldDef[] };

export type Doc = {
  doc_id: string;
  vol: string;
  pages: string[];
  fields: Fields;
  b: Partial<Record<string, { value: string; confidence: number }>>; // LLM 추출 (스키마의 prompt 칸)
  hw: Partial<Record<string, { text: string; conf: number }>>; // 손글씨 칸 판독 (김해 규칙)
  info: {
    prefix: string;
    blank: boolean;
    conf_serial: number;
    conf_date: number;
    b_serial: string;
    b_date: string;
    recipient_fix: string;
    head_ratio: number;
    box_fail: Record<string, string>;
  };
  flags: Partial<Record<string, string[]>>;
  reviewed?: boolean;
  edited?: boolean;
};

export type GraphEvent = {
  id: number;
  ts: string;
  node: string;
  phase: "start" | "end" | "error" | "waiting";
  doc_id: string | null;
  info: Record<string, unknown> | null;
};

export type SearchHit = {
  doc_id: string;
  rrf: number;
  vector_rank: number | null;
  keyword_rank: number | null;
  vol: string;
  docno: string;
  date: string;
  recipient: string;
  title: string;
};

export type Timing = {
  doc_id: string;
  mode: "review" | "edit";
  active_ms: number;
  opened_at: string;
  fields_changed: number;
};

export type DailyRow = {
  day: string;
  pages_ocr: number;
  docs_extracted: number;
  docs_indexed: number;
  docs_flagged: number;
  flag_rate: number | null;
  docs_reviewed: number;
  cells_reviewed: number;
  cells_fixed: number;
  fix_rate: number | null;
  cells_edited: number;
  review_docs_timed: number;
  review_sec_median: number | null;
  review_min_total: number;
  usd: number;
};

export type Report = {
  day_from: string;
  day_to: string;
  volume: {
    total: {
      volumes: number;
      volumes_done: number;
      pages: number;
      pages_ocr: number;
      pages_left: number;
      docs: number;
      docs_flagged: number;
      docs_indexed: number;
      docs_left: number;
    };
    volumes: { vol: string; stage: Stage; pages: number; pages_ocr: number; docs: number; docs_flagged: number; docs_indexed: number }[];
  };
  daily: DailyRow[];
  review_time: Partial<Record<Timing["mode"], { docs: number; median_sec: number; mean_sec: number; total_min: number }>>;
  columns: Record<string, string>;
};

export type Role = "viewer" | "reviewer" | "inspector" | "admin";
export type Me = { username: string; name: string; role: Role };
export type AuthConfig = { modes: ("local" | "oidc")[]; off: boolean; oidc_label: string | null };
export type EditRow = {
  ts: string; field: string; old: string | null; new: string; source: "review" | "edit" | "inspect"; stage: "entry" | "inspect";
  username: string | null; name: string | null;
};

const ROLE_ORDER: Role[] = ["viewer", "reviewer", "inspector", "admin"];
export const can = (me: Me | null | undefined, need: Role) => !!me && ROLE_ORDER.indexOf(me.role) >= ROLE_ORDER.indexOf(need);
export const ROLE_LABEL: Record<Role, string> = { viewer: "보기", reviewer: "검토", inspector: "검사", admin: "관리" };

export const loginUrl = () => {
  const here = window.location.pathname + window.location.search;
  return `/login?next=${encodeURIComponent(here)}`;
};

export class ApiError extends Error {
  constructor(
    message: string,
    public status: number,
  ) {
    super(message);
  }
}

async function call<T>(path: string, init?: RequestInit & { noRedirect?: boolean }): Promise<T> {
  const { noRedirect, ...rest } = init ?? {};
  // X-Docflow-CSRF: 다른 사이트의 폼·스크립트는 이 헤더를 붙일 수 없다 → 서버가 쿠키 요청을 믿는 근거 (app/auth.py)
  const headers = new Headers(rest.headers);
  headers.set("X-Docflow-CSRF", "1");
  const r = await fetch(`/api${path}`, { cache: "no-store", ...rest, headers });
  if (r.status === 401 && !noRedirect && window.location.pathname !== "/login") {
    window.location.assign(loginUrl()); // 세션 만료·로그인 안 함 → 로그인 뒤 이 화면으로 돌아온다
    return new Promise<T>(() => {});
  }
  if (!r.ok) {
    let msg = `${r.status}`;
    try {
      const j = await r.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {}
    throw new ApiError(msg, r.status);
  }
  return r.json() as Promise<T>;
}

const enc = encodeURIComponent;

export const api = {
  volumes: () => call<VolumeSummary[]>("/volumes"),
  volume: (vol: string) => call<VolumeSummary>(`/volumes/${enc(vol)}`),
  schema: () => call<Schema>("/schema"),
  documents: (vol: string) => call<Doc[]>(`/volumes/${enc(vol)}/documents`),
  events: (vol: string, after: number) => call<GraphEvent[]>(`/volumes/${enc(vol)}/events?after=${after}`),
  upload: (vol: string, files: File[]) => {
    const fd = new FormData();
    files.forEach((f) => fd.append("files", f, f.name));
    return call<{ saved: number; total: number }>(`/volumes/${enc(vol)}/pages`, { method: "POST", body: fd });
  },
  run: (vol: string, review: "human" | "auto") =>
    call<{ started: boolean }>(`/volumes/${enc(vol)}/run?review=${review}`, { method: "POST" }),
  resume: (vol: string) => call<{ resumed_at: string[] }>(`/volumes/${enc(vol)}/resume`, { method: "POST" }),
  submitReview: (vol: string, fixes: Record<string, Partial<Fields>>) =>
    call(`/review/${enc(vol)}/json`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fixes),
    }),
  editDocument: (vol: string, docId: string, fields: Partial<Fields>) =>
    call<Doc>(`/documents/${enc(vol)}/${enc(docId)}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(fields),
    }),
  search: (q: string, yearFrom?: string, yearTo?: string) => {
    const p = new URLSearchParams({ q });
    if (yearFrom) p.set("year_from", yearFrom);
    if (yearTo) p.set("year_to", yearTo);
    return call<SearchHit[]>(`/search?${p}`);
  },
  ask: (q: string) => call<{ answer: string; sources: SearchHit[] }>(`/ask?q=${enc(q)}`),
  timings: (vol: string, rows: Timing[]) =>
    call<{ saved: number; dropped: number }>(`/volumes/${enc(vol)}/timings`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(rows),
    }),
  report: (dayFrom?: string, dayTo?: string) => call<Report>(`/report?${reportQuery(dayFrom, dayTo)}`),
  edits: (vol: string, docId: string) => call<EditRow[]>(`/documents/${enc(vol)}/${enc(docId)}/edits`),
  authConfig: () => call<AuthConfig>("/auth/config", { noRedirect: true }),
  me: () => call<{ user: Me }>("/auth/me", { noRedirect: true }),
  login: (username: string, password: string) =>
    call<{ user: Me }>("/auth/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ username, password }),
      noRedirect: true,
    }),
  logout: () => call<{ ok: boolean }>("/auth/logout", { method: "POST", noRedirect: true }),
};

export const oidcLoginUrl = (next: string) => `/api/auth/oidc/login?next=${encodeURIComponent(next)}`;

const reportQuery = (dayFrom?: string, dayTo?: string) => {
  const p = new URLSearchParams();
  if (dayFrom) p.set("day_from", dayFrom);
  if (dayTo) p.set("day_to", dayTo);
  return p.toString();
};
export const reportCsv = (dayFrom?: string, dayTo?: string) => `/api/report.csv?${reportQuery(dayFrom, dayTo)}`;

export const headImage = (vol: string, page: string) => `/api/head/${enc(vol)}/${enc(page)}`;
export const cropImage = (vol: string, docId: string, field: string) =>
  `/api/crops/${enc(vol)}/${enc(docId)}_${field}.png`;

export const STAGE_LABEL: Record<Stage, string> = {
  new: "대기",
  queued: "대기열",
  running: "처리 중",
  review: "검토 대기",
  done: "완료",
  stopped: "중단됨",
};

// 손글씨 칸을 잘라 둔 칸 (김해 양식 규칙 — app/graph.py HW_RULES). 칸 목록·표시명은 api.schema()
export const HW_FIELDS = ["docno", "date"];
