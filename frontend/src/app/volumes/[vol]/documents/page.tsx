"use client";

import Link from "next/link";
import { use, useEffect, useMemo, useState } from "react";
import {
  api,
  cropImage,
  EDIT_FIELDS,
  FIELD_LABEL,
  flagKey,
  headImage,
  type Doc,
  type EditField,
  type Fields,
} from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
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
  const reviewing = s?.stage === "review";

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
  }, [vol, s?.stage]);

  const shown = useMemo(
    () => (docs ?? []).filter((d) => !onlyFlagged || Object.keys(d.flags).length > 0),
    [docs, onlyFlagged],
  );
  const cur = shown.find((d) => d.doc_id === sel) ?? shown[0];
  const idx = cur ? shown.indexOf(cur) : -1;
  const changed = Object.values(fixes).reduce((n, f) => n + Object.keys(f).length, 0);

  const value = (d: Doc, k: EditField) => fixes[d.doc_id]?.[k] ?? d.fields[k] ?? "";
  const setValue = (d: Doc, k: EditField, v: string) =>
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
      setFixes({});
      setNote(`검토 반영 → 흐름 재개 (고친 칸 ${changed}개). 색인이 끝나면 결과가 갱신됩니다.`);
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
      const updated = await api.editDocument(vol, d.doc_id, fixes[d.doc_id]);
      setDocs((prev) => prev?.map((x) => (x.doc_id === d.doc_id ? updated : x)) ?? null);
      setFixes((prev) => {
        const next = { ...prev };
        delete next[d.doc_id];
        return next;
      });
      setNote(`${d.doc_id} 저장 — Postgres·Qdrant·그래프 상태 갱신`);
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
        {reviewing && (
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
                        {fixes[d.doc_id] && <span className="rounded bg-blue-500 px-1 text-white">수정</span>}
                        {d.edited && <span className="rounded bg-emerald-500 px-1 text-white">저장됨</span>}
                        {n > 0 && <span className="rounded bg-rose-500 px-1 text-white">{n}</span>}
                      </span>
                    </div>
                    <div className={`truncate text-xs ${cur?.doc_id === d.doc_id ? "text-slate-300" : "text-slate-500"}`}>
                      {d.fields.title || "(제목 없음)"}
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
                {EDIT_FIELDS.map((k) => {
                  const flags = cur.flags[flagKey(k)];
                  const dirty = fixes[cur.doc_id]?.[k] !== undefined;
                  return (
                    <div key={k}>
                      <div className="mb-1 flex items-center gap-2 text-sm">
                        <span className="w-16 font-medium">{FIELD_LABEL[k]}</span>
                        <FlagChips flags={flags} />
                      </div>
                      <input
                        value={value(cur, k)}
                        onChange={(e) => setValue(cur, k, e.target.value)}
                        disabled={!reviewing && s?.stage !== "done"}
                        className={`w-full rounded-md border px-3 py-1.5 text-sm ${dirty ? "border-blue-500 bg-blue-50" : flags?.length ? "border-rose-300" : "border-slate-300"}`}
                      />
                      <div className="mt-1 space-y-1 text-xs text-slate-500">
                        <div>
                          GPT 텍스트 추출: <span className="font-mono">{cur.b[k]?.value || "–"}</span> (확신도 {conf(cur.b[k]?.confidence)})
                          {k === "recipient" && cur.info.recipient_fix && <span className="ml-1 text-amber-700">· 규칙 보정: {cur.info.recipient_fix}</span>}
                        </div>
                        {(k === "docno" || k === "date") && (
                          <div>
                            {/* eslint-disable-next-line @next/next/no-img-element */}
                            <img
                              src={cropImage(vol, cur.doc_id, k)}
                              alt={`${FIELD_LABEL[k]} 손글씨 칸`}
                              className="my-1 max-h-14 rounded border border-slate-200 bg-white"
                              onError={(e) => (e.currentTarget.style.display = "none")}
                            />
                            손글씨 판독: <span className="font-mono">{cur.hw[k]?.text || "–"}</span> (확신도 {conf(cur.hw[k]?.conf)})
                            {k === "docno" && cur.info.head_ratio > 0.4 && (
                              <span className="ml-1 text-amber-700">· 머리부 {Math.round(cur.info.head_ratio * 100)}%까지 넓혀 찾음</span>
                            )}
                          </div>
                        )}
                      </div>
                    </div>
                  );
                })}
                {!reviewing && s?.stage === "done" && (
                  <button
                    onClick={() => saveDoc(cur)}
                    disabled={saving || !fixes[cur.doc_id]}
                    className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-30"
                  >
                    이 문서 저장 (재색인)
                  </button>
                )}
              </div>
            </div>
          </Card>
        )}
      </div>
    </div>
  );
}
