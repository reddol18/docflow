"use client";

import Link from "next/link";
import { use, useMemo, useState } from "react";
import { api, type GraphEvent } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { currentRun, useEvents } from "@/lib/useEvents";
import { Card, ErrorNote, Progress, StageBadge, usd } from "@/components/ui";

type StepState = "idle" | "active" | "done" | "waiting" | "skipped" | "error";

const DOC_NODES = ["extract", "locate", "widen", "read", "merge"] as const;
const DOC_LABEL: Record<string, string> = {
  extract: "4칸 추출 (GPT)",
  locate: "손글씨 칸 찾기",
  widen: "범위 넓힘",
  read: "손글씨 판독 (비전)",
  merge: "합치기",
};

const STEP_STYLE: Record<StepState, string> = {
  idle: "border-slate-200 bg-white text-slate-400",
  active: "border-blue-400 bg-blue-50 text-blue-800 ring-2 ring-blue-200",
  done: "border-emerald-300 bg-emerald-50 text-emerald-800",
  waiting: "border-amber-400 bg-amber-50 text-amber-900 ring-2 ring-amber-200",
  skipped: "border-dashed border-slate-300 bg-white text-slate-400",
  error: "border-rose-400 bg-rose-50 text-rose-800",
};

function nodeState(evs: GraphEvent[], node: string): StepState {
  const mine = evs.filter((e) => e.node === node && !e.doc_id);
  if (!mine.length) return "idle";
  const last = mine[mine.length - 1];
  return last.phase === "start" ? "active" : last.phase === "error" ? "error" : "done";
}

function Step({ title, state, children }: { title: string; state: StepState; children?: React.ReactNode }) {
  return (
    <div className={`min-w-[150px] flex-1 rounded-lg border px-3 py-2.5 ${STEP_STYLE[state]}`}>
      <div className="flex items-center gap-1.5 text-sm font-semibold">
        {state === "active" && <span className="h-2 w-2 animate-pulse rounded-full bg-blue-600" />}
        {state === "done" && <span>✓</span>}
        {state === "waiting" && <span>⏸</span>}
        {title}
      </div>
      <div className="mt-1 text-xs opacity-80">{children}</div>
    </div>
  );
}

const Arrow = () => <div className="self-center text-slate-300">→</div>;

export default function VolumePage({ params }: { params: Promise<{ vol: string }> }) {
  const vol = decodeURIComponent(use(params).vol);
  const { data: s, error, refresh } = usePoll(() => api.volume(vol), 2000, [vol]);
  const all = useEvents(vol);
  const evs = useMemo(() => currentRun(all), [all]);
  const [busy, setBusy] = useState(false);

  // 문서별 하위 그래프 위치
  const docs = useMemo(() => {
    const m = new Map<string, { node: string; phase: string; read?: Record<string, unknown>; widened: number }>();
    for (const e of evs) {
      if (!e.doc_id || !DOC_NODES.includes(e.node as (typeof DOC_NODES)[number])) continue;
      const cur = m.get(e.doc_id) ?? { node: e.node, phase: e.phase, widened: 0 };
      cur.node = e.node;
      cur.phase = e.phase;
      if (e.node === "widen" && e.phase === "end") cur.widened += 1;
      if (e.node === "read" && e.phase === "end") cur.read = e.info ?? undefined;
      m.set(e.doc_id, cur);
    }
    return m;
  }, [evs]);

  const splitInfo = evs.findLast((e) => e.node === "split" && e.phase === "end")?.info as
    | { documents: number; orphans: number }
    | undefined;
  const totalDocs = splitInfo?.documents ?? 0;
  const merged = [...docs.values()].filter((d) => d.node === "merge" && d.phase === "end").length;
  const docCount = (n: string) => [...docs.values()].filter((d) => d.node === n && d.phase === "start").length;
  const ocrPages = evs.filter((e) => e.node === "ocr_page");
  const ocrAvg = ocrPages.length
    ? ocrPages.reduce((a, e) => a + Number((e.info as { seconds: number }).seconds), 0) / ocrPages.length
    : 0;
  const waiting = s?.stage === "review";
  const reviewEnd = evs.some((e) => e.node === "run" && e.phase === "start" && (e.info as { resume?: boolean })?.resume);
  const indexState = nodeState(evs, "index");
  const reviewState: StepState = waiting
    ? "waiting"
    : reviewEnd
      ? "done"
      : indexState !== "idle"
        ? "skipped"
        : "idle";
  const docState: StepState = !totalDocs ? "idle" : merged >= totalDocs ? "done" : docs.size ? "active" : "idle";

  const resume = async () => {
    setBusy(true);
    try {
      await api.resume(vol);
      refresh();
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-6">
      <ErrorNote error={error} />
      <div className="flex flex-wrap items-center gap-3">
        <Link href="/" className="text-sm text-slate-500 hover:text-slate-800">← 대시보드</Link>
        <h1 className="font-mono text-lg font-semibold">{vol}</h1>
        {s && <StageBadge stage={s.stage} />}
        <div className="ml-auto flex gap-2">
          {s?.stage === "stopped" && (
            <button onClick={resume} disabled={busy} className="rounded-md bg-rose-600 px-3 py-1.5 text-sm text-white">
              끊긴 곳부터 다시
            </button>
          )}
          <Link
            href={`/volumes/${encodeURIComponent(vol)}/documents`}
            className={`rounded-md px-3 py-1.5 text-sm font-medium ${waiting ? "bg-amber-500 text-white" : "bg-slate-900 text-white"}`}
          >
            {waiting ? `검토하기 (${s?.flagged_documents}건)` : "결과·수정"}
          </Link>
        </div>
      </div>

      <Card title="LangGraph 진행 단계" right={<span className="text-xs text-slate-400">thread_id = {vol} · 체크포인트: Postgres</span>}>
        <div className="flex flex-wrap items-stretch gap-2">
          <Step title="OCR" state={nodeState(evs, "ocr")}>
            <Progress value={s?.ocr_done ?? 0} max={s?.uploaded ?? 0} />
            <div className="mt-1 tabular-nums">
              {s?.ocr_done ?? 0}/{s?.uploaded ?? 0}쪽{ocrAvg ? ` · 쪽당 ${ocrAvg.toFixed(1)}초` : ""}
            </div>
          </Step>
          <Arrow />
          <Step title="문서 나누기" state={nodeState(evs, "split")}>
            {splitInfo ? `문서 ${splitInfo.documents}건${splitInfo.orphans ? ` · 앞쪽 남는 쪽 ${splitInfo.orphans}` : ""}` : "규칙: 머리부 + 문서번호/시행일자"}
          </Step>
          <Arrow />
          <Step title="문서별 하위 그래프 ×N" state={docState}>
            <Progress value={merged} max={totalDocs} tone={merged === totalDocs && totalDocs ? "emerald" : "blue"} />
            <div className="mt-1 tabular-nums">
              {merged}/{totalDocs || "?"} 완료 · 병렬 4
            </div>
          </Step>
          <Arrow />
          <Step title="검토 표시" state={nodeState(evs, "validate")}>
            {s?.documents ? `표시 ${s.flagged_documents}/${s.documents}건` : "형식·범위·흐름·확신도"}
          </Step>
          <Arrow />
          <Step title="사람 검토" state={reviewState}>
            {waiting ? "interrupt() — 입력 대기" : reviewState === "skipped" ? "auto 모드: 건너뜀" : reviewState === "done" ? "검토 반영" : "표시된 문서만"}
          </Step>
          <Arrow />
          <Step title="색인" state={indexState}>
            {s?.indexed != null ? `Postgres + Qdrant ${s.indexed}건` : "임베딩 → Qdrant"}
          </Step>
        </div>

        {totalDocs > 0 && (
          <div className="mt-4 flex flex-wrap gap-2 text-xs text-slate-500">
            <span className="font-medium text-slate-600">하위 그래프 지금 위치:</span>
            {DOC_NODES.map((n) => (
              <span key={n} className="rounded bg-slate-100 px-2 py-0.5">
                {DOC_LABEL[n]} {docCount(n)}
              </span>
            ))}
          </div>
        )}
      </Card>

      <div className="grid gap-6 lg:grid-cols-[3fr_2fr]">
        <Card title={`문서 (${docs.size})`}>
          <div className="max-h-[480px] overflow-y-auto">
            <table className="w-full text-sm">
              <thead className="sticky top-0 bg-white text-left text-xs text-slate-500">
                <tr>
                  <th className="py-1.5 pr-3 font-medium">시작 쪽</th>
                  <th className="py-1.5 pr-3 font-medium">하위 그래프 단계</th>
                  <th className="py-1.5 font-medium">손글씨 판독</th>
                </tr>
              </thead>
              <tbody>
                {[...docs.entries()].map(([id, d]) => (
                  <tr key={id} className="border-t border-slate-50">
                    <td className="py-1.5 pr-3 font-mono text-xs">{id.split("-").pop()}</td>
                    <td className="py-1.5 pr-3">
                      <span className={d.node === "merge" && d.phase === "end" ? "text-emerald-700" : "text-blue-700"}>
                        {d.node === "merge" && d.phase === "end" ? "완료" : `${DOC_LABEL[d.node]}${d.phase === "start" ? "…" : ""}`}
                      </span>
                      {d.widened > 0 && <span className="ml-2 text-xs text-amber-700">범위 넓힘 ×{d.widened}</span>}
                    </td>
                    <td className="py-1.5 font-mono text-xs text-slate-600">
                      {d.read ? `${String(d.read.docno ?? "")} · ${String(d.read.date ?? "")}` : ""}
                    </td>
                  </tr>
                ))}
                {!docs.size && (
                  <tr>
                    <td colSpan={3} className="py-6 text-center text-slate-400">
                      {s?.stage === "running" ? "OCR 중 — 끝나면 문서별 흐름이 시작됩니다" : "아직 없음"}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </div>
        </Card>

        <div className="space-y-6">
          <Card title="비용">
            <table className="w-full text-sm">
              <tbody>
                {(s?.usage ?? []).map((u) => (
                  <tr key={u.kind} className="border-t border-slate-50 first:border-0">
                    <td className="py-1">{u.kind}</td>
                    <td className="py-1 text-right tabular-nums text-slate-500">{u.calls}회</td>
                    <td className="py-1 text-right tabular-nums">{usd(u.usd)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </Card>
          <Card title="이벤트 로그">
            <ol className="max-h-[320px] space-y-0.5 overflow-y-auto font-mono text-[11px] text-slate-600">
              {[...all].reverse().slice(0, 150).map((e) => (
                <li key={e.id} className={e.phase === "error" ? "text-rose-600" : e.phase === "waiting" ? "text-amber-700" : ""}>
                  {new Date(e.ts).toLocaleTimeString("ko-KR", { hour12: false })} {e.node}.{e.phase}
                  {e.doc_id ? ` ${e.doc_id.split("-").pop()}` : ""}
                  {e.info ? ` ${JSON.stringify(e.info)}` : ""}
                </li>
              ))}
            </ol>
          </Card>
        </div>
      </div>
    </div>
  );
}
