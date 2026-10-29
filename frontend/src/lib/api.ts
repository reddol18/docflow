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

export type Fields = { docno: string; serial: string; date: string; recipient: string; title: string };

export type Doc = {
  doc_id: string;
  vol: string;
  pages: string[];
  fields: Fields;
  b: Record<"docno" | "date" | "recipient" | "title", { value: string; confidence: number }>;
  hw: Record<"docno" | "date", { text: string; conf: number }>;
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
  flags: Partial<Record<"serial" | "date" | "recipient" | "title", string[]>>;
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

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(`/api${path}`, { cache: "no-store", ...init });
  if (!r.ok) {
    let msg = `${r.status}`;
    try {
      const j = await r.json();
      msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail ?? j);
    } catch {}
    throw new Error(msg);
  }
  return r.json() as Promise<T>;
}

const enc = encodeURIComponent;

export const api = {
  volumes: () => call<VolumeSummary[]>("/volumes"),
  volume: (vol: string) => call<VolumeSummary>(`/volumes/${enc(vol)}`),
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
};

export const headImage = (vol: string, page: string) => `/api/head/${enc(vol)}/${enc(page)}`;
export const cropImage = (vol: string, docId: string, field: "docno" | "date") =>
  `/api/crops/${enc(vol)}/${enc(docId)}_${field}.png`;

export const STAGE_LABEL: Record<Stage, string> = {
  new: "대기",
  queued: "대기열",
  running: "처리 중",
  review: "검토 대기",
  done: "완료",
  stopped: "중단됨",
};

export const FIELD_LABEL = { docno: "문서번호", date: "시행일자", recipient: "수신", title: "제목" } as const;
export type EditField = keyof typeof FIELD_LABEL;
export const EDIT_FIELDS = Object.keys(FIELD_LABEL) as EditField[];

// 문서번호 칸의 검토 표시는 'serial'(일련번호) 키로 온다
export const flagKey = (f: EditField) => (f === "docno" ? "serial" : f);
