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
  rule: string | null; // 값을 만드는 코드 규칙 (app/graph.py RULES). 칸 이름 대신 이것으로 판단한다
  hw: string | null; // 보여 줄 잘라낸 손글씨 칸 (cropImage·Doc.hw 키). 없으면 손글씨 칸 아님
  // 칸 검증 규칙 (https://github.com/reddol18/docflow/issues/87). reasons = 이 칸에 붙을 수 있는 기계 검수 표시 사유 글자
  validate?: { rules: string[]; reasons: string[] } | null;
};
// machine_flag_prefix: 기계 검수 표시 사유의 머리 (서버 상수, https://github.com/reddol18/docflow/issues/87)
export type Schema = { name: string; search_columns: string[]; machine_flag_prefix?: string; fields: FieldDef[] };

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

// 검색 결과: 순위 정보 + fields(스키마의 보이는 칸 값). 칸 이름은 스키마마다 다르다 — 열은 Schema.search_columns
export type SearchHit = {
  doc_id: string;
  rrf: number;
  vector_rank: number | null;
  keyword_rank: number | null;
  vol: string;
  fields: Fields;
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
  // source·stage: 설정 흐름의 검수 단계에서 고친 칸은 source "stage:<단계>", stage "<단계>"
  ts: string; field: string; old: string | null; new: string; source: string; stage: string;
  username: string | null; name: string | null;
};

// 검토 흐름 설정 (GET /workflow, app/workflows/*.yaml — https://github.com/reddol18/docflow/issues/86)
export type WorkflowStage = {
  name: string;
  label: string;
  role: Role;
  kind: "entry" | "review";
  select: "all" | "sample";
  rate: number | null;
  reject: boolean;
  reject_to: string | null;
  submit: "manual" | "on_index" | null;
  actions: boolean; // /workflow 경로로 승인·반려하는 단계 (표본 검사 단계는 /inspection)
};
export type Workflow = {
  name: string;
  distinct_actors: boolean;
  count_unconfirmed: boolean;
  initial: string;
  stages: WorkflowStage[];
};
export type DocStage = {
  doc_id: string;
  stage: string; // 저장된 위치: 단계 이름, 확정이면 "_confirmed"
  // confirmed 확정 / excluded 표본 단계의 검사 목록 밖 — 검사 없이 넘어감(입력 경로로 고칠 수 있음) / pending 차례 기다림
  status: "pending" | "excluded" | "confirmed";
  label: string | null;
  editable: boolean; // 입력 경로(PATCH /documents)로 고칠 수 있는 위치
  round: number;
  recheck: boolean;
  confirmed: boolean;
  last_reason: string | null;
  rejected_from: string | null;
  can_approve: boolean;
  can_reject: boolean;
};
export type VolumeWorkflow = { workflow: Workflow; ready: boolean; counts: Record<string, number>; docs: DocStage[] };
// 단계 승인·반려를 이 화면에서 하는 흐름인가 (기본 흐름 — 입력은 색인으로 끝나고 검사는 표본 — 이면 화면은 지금 그대로)
export const hasStageActions = (wf: Workflow | null | undefined) =>
  !!wf && (wf.stages.some((s) => s.kind === "review" && s.actions) || wf.stages[0]?.submit === "manual");

// 표본 검사 (GET/POST /volumes/{vol}/inspection, POST /inspection/{vol}/{doc_id} — https://github.com/reddol18/docflow/issues/66)
export type InspectionReason = "flagged" | "sample" | "field100";
// 검사할 수 없는 까닭 (서버 main._inspect_block). null이면 검사 가능
export type InspectBlock =
  | "inspected"
  | "in_progress"
  | "role"
  | "user"
  | "submitted"
  | "entered"
  | "other_stage"
  | "stage";
export type InspectionPlan = {
  vol: string;
  rate: number;
  fields100: string[];
  flagged: number;
  unflagged: number;
  sample: number;
  created_at: string;
};
export type InspectionDoc = {
  doc_id: string;
  reason: InspectionReason;
  inspected_at: string | null;
  inspected_by: string | null;
  fixed_fields: string[] | null; // null이면 아직 끝나지 않음 (inspected_at만 있으면 누가 차지해 검사 중)
  fields: Fields | null;
  flags: Partial<Record<string, string[]>> | null;
  check_fields: string[];
  can_inspect: boolean;
  block: InspectBlock | null;
};
export type InspectionView = { plan: InspectionPlan; docs: InspectionDoc[] };
// 이 흐름의 표본 검사 단계 (없으면 검사 경로가 404)
export const sampleStage = (wf: Workflow | null | undefined) => wf?.stages.find((s) => s.select === "sample") ?? null;

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
    // reflagged: 기계 검수(철 안 중복)로 표시가 바뀐 다른 문서 id
    call<Doc & { reflagged?: string[] }>(`/documents/${enc(vol)}/${enc(docId)}`, {
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
  workflow: () => call<Workflow>("/workflow"),
  volumeWorkflow: (vol: string) => call<VolumeWorkflow>(`/volumes/${enc(vol)}/workflow`),
  approveStage: (vol: string, docId: string, stage: string, fields: Partial<Fields>) =>
    call<{ to: string; round: number; fixed_fields: string[] }>(`/workflow/${enc(vol)}/${enc(docId)}/approve`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage, fields }),
    }),
  rejectStage: (vol: string, docId: string, stage: string, reason: string) =>
    call<{ to: string; round: number }>(`/workflow/${enc(vol)}/${enc(docId)}/reject`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage, reason }),
    }),
  inspection: (vol: string) => call<InspectionView>(`/volumes/${enc(vol)}/inspection`),
  // rate를 빼면 서버 기본 비율 (흐름 설정 rate 또는 INSPECT_RATE — 관리자 아니면 그 아래로 못 내린다)
  createInspection: (vol: string, body: { rate?: number; fields100: string[] }) =>
    call<{ plan: Omit<InspectionPlan, "vol" | "created_at">; docs: number }>(`/volumes/${enc(vol)}/inspection`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    }),
  // fields를 비우면 '이상 없음', 고친 칸만 보내면 '고쳐서 저장'
  inspect: (vol: string, docId: string, fields: Partial<Fields>) =>
    call<{ doc_id: string; fixed_fields: string[] }>(`/inspection/${enc(vol)}/${enc(docId)}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ fields }),
    }),
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
// cell = FieldDef.hw (잘라낸 손글씨 칸 — 칸 이름이 아니다)
export const cropImage = (vol: string, docId: string, cell: string) =>
  `/api/crops/${enc(vol)}/${enc(docId)}_${cell}.png`;

export const STAGE_LABEL: Record<Stage, string> = {
  new: "대기",
  queued: "대기열",
  running: "처리 중",
  review: "검토 대기",
  done: "완료",
  stopped: "중단됨",
};
