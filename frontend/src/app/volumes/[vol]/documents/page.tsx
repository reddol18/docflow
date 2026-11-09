"use client";

import Link from "next/link";
import { use, useEffect, useMemo, useState } from "react";
import {
  api,
  can,
  cropImage,
  hasStageActions,
  headImage,
  type Doc,
  type DocStage,
  type Fields,
  type Schema,
  type Workflow,
} from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { useMe } from "@/lib/useMe";
import { useReviewTimer } from "@/lib/useReviewTimer";
import { Card, ErrorNote, FlagChips, StageBadge } from "@/components/ui";

type Fixes = Record<string, Partial<Fields>>;

// README 캡처용: 원본 쪽 이미지에 실명·서명·직인이 있어 공개 캡처에서는 흐리게 한다
const MASK = process.env.NEXT_PUBLIC_MASK_IMAGES === "1";

const conf = (n: number | undefined) => (n == null ? "" : n.toFixed(2));

export default function DocumentsPage({ params }: { params: Promise<{ vol: string }> }) {
  const vol = decodeURIComponent(use(params).vol);
  const { data: s } = usePoll(() => api.volume(vol), 3000, [vol]);
  const [docs, setDocs] = useState<Doc[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [onlyFlagged, setOnlyFlagged] = useState(true);
  const [sel, setSel] = useState<string | null>(null);
  const [fixes, setFixes] = useState<Fixes>({});
  const [saving, setSaving] = useState(false);
  const [note, setNote] = useState<string | null>(null);
  const [schema, setSchema] = useState<Schema | null>(null);
  const [wf, setWf] = useState<Workflow | null>(null);
  const [stages, setStages] = useState<Record<string, DocStage>>({});
  const [stageTick, setStageTick] = useState(0);
  const [reasons, setReasons] = useState<Record<string, string>>({}); // 반려 사유는 문서마다 따로
  const reviewing = s?.stage === "review";

  // 칸 목록·표시명은 서버의 색인 스키마에서 (https://github.com/reddol18/docflow/issues/9)
  // 검토 단계는 서버의 흐름 설정에서 (https://github.com/reddol18/docflow/issues/86)
  useEffect(() => {
    let alive = true;
    api
      .schema()
      .then((x) => alive && setSchema(x))
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)));
    api
      .workflow()
      .then((x) => alive && setWf(x))
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, []);
  // 단계 승인·반려는 그것을 이 화면에서 하는 흐름일 때만 (기본 흐름은 버튼 없이 단계·확정·검사 제외 표시만)
  const staged = hasStageActions(wf);
  useEffect(() => {
    if (s?.stage !== "done") return;
    let alive = true;
    api
      .volumeWorkflow(vol)
      .then((x) => alive && setStages(Object.fromEntries(x.docs.map((d) => [d.doc_id, d]))))
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [vol, s?.stage, stageTick]);
  const stageLabel = (name: string | null) => wf?.stages.find((x) => x.name === name)?.label ?? name;
  const cells = (schema?.fields ?? []).filter((f) => f.level === "document");
  const titleKey = cells.find((f) => f.isad === "title")?.name ?? cells[0]?.name;
  // 기계 검수 표시 사유 (서버가 칸마다 알려 줌) — 칩 색 구분용
  const machine = useMemo(() => (schema?.fields ?? []).flatMap((f) => f.validate?.reasons ?? []), [schema]);
  const editor = can(useMe(), "reviewer"); // 보기 역할은 고치기·검토 버튼을 숨긴다 (서버도 403으로 막음)

  // 철 상태가 바뀔 때(검토 대기 → 처리 중 → 완료) 문서를 다시 읽는다
  useEffect(() => {
    let alive = true;
    api
      .documents(vol)
      .then((d) => {
        if (!alive) return;
        setDocs(d);
        setError(null);
      })
      .catch((e) => alive && setError(e instanceof Error ? e.message : String(e)));
    return () => {
      alive = false;
    };
  }, [vol, s?.stage, stageTick]);

  const shown = useMemo(
    () => (docs ?? []).filter((d) => !onlyFlagged || Object.keys(d.flags).length > 0),
    [docs, onlyFlagged],
  );
  const cur = shown.find((d) => d.doc_id === sel) ?? shown[0];
  const idx = cur ? shown.indexOf(cur) : -1;
  const changed = Object.values(fixes).reduce((n, f) => n + Object.keys(f).length, 0);
  const changedBy = Object.fromEntries(Object.entries(fixes).map(([id, f]) => [id, Object.keys(f).length]));
  // 공정 리포트용 검토 시간: 지금 보고 있는 문서의 실제 작업 시간(자리 비움 제외)
  const timer = useReviewTimer(vol, cur?.doc_id, reviewing ? "review" : "edit", editor && (reviewing || s?.stage === "done"));

  const value = (d: Doc, k: string) => fixes[d.doc_id]?.[k] ?? d.fields[k] ?? "";
  const setValue = (d: Doc, k: string, v: string) =>
    setFixes((prev) => {
      const next = { ...prev, [d.doc_id]: { ...prev[d.doc_id] } };
      if (v === (d.fields[k] ?? "")) delete next[d.doc_id][k];
      else next[d.doc_id][k] = v;
      if (!Object.keys(next[d.doc_id]).length) delete next[d.doc_id];
      return next;
    });

  const submitReview = async () => {
    setSaving(true);
    setNote(null);
    try {
      await api.submitReview(vol, fixes);
      await timer.flush(undefined, changedBy);
      setFixes({});
      setNote(`검토 반영 → 흐름 재개 (고친 칸 ${changed}개). 색인이 끝나면 결과가 갱신됩니다.`);
    } catch (e) {
      setNote(`실패: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  const cs = cur ? stages[cur.doc_id] : undefined; // 지금 문서의 검토 단계
  // 입력 경로(저장)로 고칠 수 있나 — 서버가 내려준 editable (PATCH와 같은 규칙, workflow.editable_positions)
  const entryEditable = !staged || !!cs?.editable;
  // 고칠 수 있나: 기본 흐름은 지금처럼, 단계 흐름은 입력 경로로 고칠 수 있는 위치 또는 내가 승인할 차례(승인하며 고침)
  const canEdit = reviewing || (s?.stage === "done" && (entryEditable || !!cs?.can_approve));
  const reason = cur ? (reasons[cur.doc_id] ?? "") : "";
  const setReason = (docId: string, v: string) => setReasons((prev) => ({ ...prev, [docId]: v }));

  const dropFixes = (docId: string) =>
    setFixes((prev) => {
      const next = { ...prev };
      delete next[docId];
      return next;
    });

  const approveStage = async (d: Doc, st: DocStage) => {
    setSaving(true);
    setNote(null);
    try {
      const r = await api.approveStage(vol, d.doc_id, st.stage, fixes[d.doc_id] ?? {});
      await timer.flush([d.doc_id], changedBy);
      dropFixes(d.doc_id);
      setNote(`${d.doc_id} ${stageLabel(st.stage)} 승인 → ${r.to === "_confirmed" ? "확정" : stageLabel(r.to)}`);
      setStageTick((n) => n + 1);
    } catch (e) {
      setNote(`실패: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  const rejectStage = async (d: Doc, st: DocStage) => {
    setSaving(true);
    setNote(null);
    try {
      const r = await api.rejectStage(vol, d.doc_id, st.stage, reasons[d.doc_id] ?? "");
      setReason(d.doc_id, "");
      setNote(`${d.doc_id} 반려 → ${stageLabel(r.to)} (차례 ${r.round})`);
      setStageTick((n) => n + 1);
    } catch (e) {
      setNote(`실패: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  const saveDoc = async (d: Doc) => {
    if (!fixes[d.doc_id]) return;
    setSaving(true);
    setNote(null);
    try {
      const { reflagged = [], ...updated } = await api.editDocument(vol, d.doc_id, fixes[d.doc_id]);
      await timer.flush([d.doc_id], changedBy);
      // 기계 검수의 철 안 중복으로 다른 문서 표시가 바뀌었을 때만 목록을 다시 읽는다 (https://github.com/reddol18/docflow/issues/87)
      const list = reflagged.length
        ? await api.documents(vol)
        : (docs ?? []).map((x) => (x.doc_id === d.doc_id ? updated : x));
      setDocs(list);
      // 지금 문서 자리를 지킨다. '검토 필요만' 보기에서 빠지면(표시가 다 풀림) 그 뒤의 다음 문서로
      const flagged = (x: Doc) => Object.keys(x.flags).length > 0;
      const stays = !onlyFlagged || list.some((x) => x.doc_id === d.doc_id && flagged(x));
      if (stays) setSel(d.doc_id);
      else {
        const pos = shown.findIndex((x) => x.doc_id === d.doc_id);
        const ids = new Set(list.filter(flagged).map((x) => x.doc_id));
        const next = [...shown.slice(pos + 1), ...shown.slice(0, Math.max(pos, 0))].find((x) => ids.has(x.doc_id));
        setSel(next?.doc_id ?? null);
      }
      setFixes((prev) => {
        const next = { ...prev };
        delete next[d.doc_id];
        return next;
      });
      setNote(
        `${d.doc_id} 저장 — Postgres·Qdrant·그래프 상태 갱신${reflagged.length ? ` (다른 문서 ${reflagged.length}건 기계 검수 표시 바뀜)` : ""}`,
      );
    } catch (e) {
      setNote(`실패: ${e instanceof Error ? e.message : e}`);
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      <div className="flex flex-wrap items-center gap-3">
        <Link href={`/volumes/${encodeURIComponent(vol)}`} className="text-sm text-slate-500 hover:text-slate-800">
          ← 진행 단계
        </Link>
        <h1 className="font-mono text-lg font-semibold">{vol}</h1>
        {s && <StageBadge stage={s.stage} />}
        <label className="ml-4 flex items-center gap-1.5 text-sm text-slate-600">
          <input type="checkbox" checked={onlyFlagged} onChange={(e) => setOnlyFlagged(e.target.checked)} />
          검토 필요만 ({(docs ?? []).filter((d) => Object.keys(d.flags).length).length}/{docs?.length ?? 0})
        </label>
        {reviewing && editor && (
          <button
            onClick={submitReview}
            disabled={saving}
            className="ml-auto rounded-md bg-amber-500 px-4 py-2 text-sm font-semibold text-white disabled:opacity-50"
          >
            검토 완료 → 이어서 색인 {changed ? `(고친 칸 ${changed})` : ""}
          </button>
        )}
      </div>
      {reviewing && (
        <div className="rounded-lg border border-amber-200 bg-amber-50 px-4 py-2 text-sm text-amber-900">
          흐름이 <b>사람 검토(interrupt)</b>에서 멈춰 있습니다. 빨간 표시가 붙은 칸만 확인하고 고친 뒤 위 버튼을 누르면
          LangGraph가 <code>Command(resume=고친 값)</code>으로 이어서 색인합니다.
        </div>
      )}
      {note && <div className="rounded-lg bg-slate-100 px-4 py-2 text-sm">{note}</div>}

      <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
        <Card>
          <ul className="max-h-[70vh] space-y-1 overflow-y-auto text-sm">
            {shown.map((d) => {
              const n = Object.values(d.flags).flat().length;
              return (
                <li key={d.doc_id}>
                  <button
                    onClick={() => setSel(d.doc_id)}
                    className={`w-full rounded-md px-2 py-1.5 text-left ${cur?.doc_id === d.doc_id ? "bg-slate-900 text-white" : "hover:bg-slate-100"}`}
                  >
                    <div className="flex items-center justify-between font-mono text-xs">
                      <span>{d.doc_id.split("-").slice(-2).join("-")}</span>
                      <span className="flex gap-1">
                        {stages[d.doc_id] && (
                          <span
                            className={`rounded px-1 ${stages[d.doc_id].confirmed ? "bg-emerald-600 text-white" : stages[d.doc_id].status === "excluded" ? "bg-sky-100 text-sky-800" : stages[d.doc_id].recheck ? "bg-amber-500 text-white" : "bg-slate-200 text-slate-700"}`}
                          >
                            {stages[d.doc_id].label ?? stages[d.doc_id].stage}
                            {stages[d.doc_id].recheck && !stages[d.doc_id].confirmed ? " 재" : ""}
                          </span>
                        )}
                        {fixes[d.doc_id] &&<span className="rounded bg-blue-500 px-1 text-white">수정</span>}
                        {d.edited && <span className="rounded bg-emerald-500 px-1 text-white">저장됨</span>}
                        {n > 0 && <span className="rounded bg-rose-500 px-1 text-white">{n}</span>}
                      </span>
                    </div>
                    <div className={`truncate text-xs ${cur?.doc_id === d.doc_id ? "text-slate-300" : "text-slate-500"}`}>
                      {(titleKey && d.fields[titleKey]) || "(제목 없음)"}
                    </div>
                  </button>
                </li>
              );
            })}
            {!shown.length && <li className="py-6 text-center text-slate-400">{docs ? "없음" : "불러오는 중…"}</li>}
          </ul>
        </Card>

        {cur && (
          <Card
            title={`${cur.doc_id} · ${cur.pages.length}쪽`}
            right={
              <div className="flex gap-2 text-sm">
                <button disabled={idx <= 0} onClick={() => setSel(shown[idx - 1].doc_id)} className="rounded border px-2 py-1 disabled:opacity-30">
                  ← 이전
                </button>
                <button
                  disabled={idx >= shown.length - 1}
                  onClick={() => setSel(shown[idx + 1].doc_id)}
                  className="rounded border px-2 py-1 disabled:opacity-30"
                >
                  다음 →
                </button>
              </div>
            }
          >
            <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
              <div>
                <div className="mb-1 text-xs text-slate-500">원본 머리부 (쪽 위 45%)</div>
                {/* eslint-disable-next-line @next/next/no-img-element */}
                <img
                  src={headImage(vol, cur.doc_id)}
                  alt="머리부"
                  className={`w-full rounded border border-slate-200 ${MASK ? "blur-[3px]" : ""}`}
                />
                {MASK && <div className="mt-1 text-[11px] text-slate-400">공개 캡처용으로 원본을 흐리게 표시 (실명·직인 보호)</div>}
              </div>
              <div className="space-y-4">
                {cells.map((f) => {
                  const k = f.name;
                  const cell = f.hw; // 잘라낸 손글씨 칸 — 칸 이름이 아니라 스키마 rule로 서버가 정한다
                  const hw = cell ? cur.hw[cell] : undefined;
                  const flags = cur.flags[f.flag_key];
                  const dirty = fixes[cur.doc_id]?.[k] !== undefined;
                  return (
                    <div key={k}>
                      <div className="mb-1 flex items-center gap-2 text-sm">
                        <span className="w-16 font-medium">{f.label}</span>
                        <FlagChips flags={flags} machine={machine} />
                      </div>
                      <input
                        value={value(cur, k)}
                        onChange={(e) => setValue(cur, k, e.target.value)}
                        disabled={!editor || !f.editable || !canEdit}
                        className={`w-full rounded-md border px-3 py-1.5 text-sm ${dirty ? "border-blue-500 bg-blue-50" : flags?.length ? "border-rose-300" : "border-slate-300"}`}
                      />
                      <div className="mt-1 space-y-1 text-xs text-slate-500">
                        {cur.b[k] && (
                          <div>
                            GPT 텍스트 추출: <span className="font-mono">{cur.b[k]?.value || "–"}</span> (확신도 {conf(cur.b[k]?.confidence)})
                            {f.rule === "recipient" && cur.info.recipient_fix && <span className="ml-1 text-amber-700">· 규칙 보정: {cur.info.recipient_fix}</span>}
                          </div>
                        )}
                        {cell && hw && (
                          <div>
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img
                              src={cropImage(vol, cur.doc_id, cell)}
                              alt={`${f.label} 손글씨 칸`}
                              className="my-1 max-h-14 rounded border border-slate-200 bg-white"
                              onError={(e) => (e.currentTarget.style.display = "none")}
                            />
                            손글씨 판독: <span className="font-mono">{hw.text || "–"}</span> (확신도 {conf(hw.conf)})
                            {cell === "docno" && cur.info.head_ratio > 0.4 && (
                              <span className="ml-1 text-amber-700">· 머리부 {Math.round(cur.info.head_ratio * 100)}%까지 넓혀 찾음</span>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
                {!reviewing && s?.stage === "done" && editor && entryEditable && (
                  <button
                    onClick={() => saveDoc(cur)}
                    disabled={saving || !fixes[cur.doc_id]}
                    className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-30"
                  >
                    이 문서 저장 (재색인)
                  </button>
                )}
                {cs && (
                  <div className="space-y-2 rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm">
                    <div>
                      검토 단계: <b>{cs.label ?? cs.stage}</b>
                      {cs.round > 1 && !cs.confirmed && <span className="ml-2 text-amber-700">재검수 (차례 {cs.round})</span>}
                    </div>
                    {/* 반려 사유는 반려된 뒤 반려한 단계가 다시 승인하기 전까지만 (서버가 그때 지운다) */}
                    {staged && cs.last_reason && !cs.confirmed && (
                      <div className="rounded border border-amber-200 bg-amber-50 px-2 py-1 text-amber-900">
                        반려 사유 ({stageLabel(cs.rejected_from)}): {cs.last_reason}
                      </div>
                    )}
                    {cs.can_approve && (
                      <button
                        onClick={() => approveStage(cur, cs)}
                        disabled={saving}
                        className="rounded-md bg-emerald-600 px-4 py-2 font-medium text-white disabled:opacity-30"
                      >
                        {cs.label ?? cs.stage} 승인{fixes[cur.doc_id] ? " (고친 칸 함께)" : ""} → 다음 단계
                      </button>
                    )}
                    {cs.can_reject && (
                      <div className="flex flex-wrap items-center gap-2">
                        <input
                          value={reason}
                          onChange={(e) => setReason(cur.doc_id, e.target.value)}
                          maxLength={500}
                          placeholder="반려 사유 (필수)"
                          className="min-w-0 flex-1 rounded-md border border-slate-300 px-3 py-1.5"
                        />
                        <button
                          onClick={() => rejectStage(cur, cs)}
                          disabled={saving || !reason.trim()}
                          className="rounded-md bg-rose-600 px-4 py-2 font-medium text-white disabled:opacity-30"
                        >
                          반려 → {stageLabel(wf?.stages.find((x) => x.name === cs.stage)?.reject_to ?? null)}
                        </button>
                      </div>
                    )}
                  </div>
                )}
              </div>
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}
