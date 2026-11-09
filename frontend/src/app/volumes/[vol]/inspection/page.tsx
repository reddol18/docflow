"use client";

// 검사 탭: 검사 목록 만들기 · 진행 · 문서마다 검사 (https://github.com/reddol18/docflow/issues/66)
// 서버 규칙은 그대로 따른다 — 버튼을 숨기는 것은 화면 편의일 뿐, 역할·교차검사는 서버가 다시 막는다(403).
import Link from "next/link";
import { use, useCallback, useEffect, useMemo, useState } from "react";
import {
  api,
  ApiError,
  can,
  headImage,
  ROLE_LABEL,
  sampleStage,
  type EditRow,
  type Fields,
  type InspectBlock,
  type InspectionDoc,
  type InspectionReason,
  type InspectionView,
  type Schema,
  type Workflow,
} from "@/lib/api";
import { useMe } from "@/lib/useMe";
import { usePoll } from "@/lib/usePoll";
import { Card, ErrorNote, FlagChips, Progress, StageBadge, Stat, VolumeTabs } from "@/components/ui";

// 원본 쪽 이미지에 실명·서명·직인이 있어 공개 캡처에서는 흐리게 한다 (결과·수정 화면과 같음)
const MASK = process.env.NEXT_PUBLIC_MASK_IMAGES === "1";

const REASON_LABEL: Record<InspectionReason, string> = { flagged: "검토 표시", sample: "표본", field100: "100% 검수 칸" };
const REASONS: InspectionReason[] = ["flagged", "sample", "field100"];

const msg = (e: unknown) => (e instanceof Error ? e.message : String(e));

function blockText(b: InspectBlock, d: InspectionDoc, role: string) {
  switch (b) {
    case "inspected":
      return `이미 검사함${d.inspected_by ? ` (${d.inspected_by})` : ""}`;
    case "role":
      return `읽기 전용 — 검사는 ${role} 역할부터`;
    case "submitted":
      return "이 철의 검토를 제출한 사람은 검사할 수 없음 (교차검사)";
    case "entered":
      return "이 문서를 입력 단계에서 고친 사람은 검사할 수 없음 (교차검사)";
    case "other_stage":
      return "같은 문서의 다른 검수 단계를 맡은 사람은 검사할 수 없음";
    case "stage":
      return "아직 검사 단계에 오지 않은 문서";
  }
}

export default function InspectionPage({ params }: { params: Promise<{ vol: string }> }) {
  const vol = decodeURIComponent(use(params).vol);
  const { data: s } = usePoll(() => api.volume(vol), 5000, [vol]);
  const me = useMe();
  const [wf, setWf] = useState<Workflow | null>(null);
  const [schema, setSchema] = useState<Schema | null>(null);
  const [view, setView] = useState<InspectionView | null>(null);
  const [missing, setMissing] = useState(false); // 검사 목록 없음 (404)
  const [error, setError] = useState<string | null>(null);
  const [note, setNote] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [sel, setSel] = useState<string | null>(null);
  const [onlyLeft, setOnlyLeft] = useState(true);
  const [fixes, setFixes] = useState<Record<string, Partial<Fields>>>({});
  const [edits, setEdits] = useState<EditRow[]>([]);
  const [tick, setTick] = useState(0);
  // 만들기 양식: 비율(%) — 비워 두면 서버 기본 비율, 100% 검수 칸
  const [ratePct, setRatePct] = useState<string | null>(null);
  const [fields100, setFields100] = useState<string[]>([]);

  useEffect(() => {
    let alive = true;
    api
      .workflow()
      .then((x) => alive && setWf(x))
      .catch((e) => alive && setError(msg(e)));
    api
      .schema()
      .then((x) => alive && setSchema(x))
      .catch((e) => alive && setError(msg(e)));
    return () => {
      alive = false;
    };
  }, []);

  const stage = sampleStage(wf);
  const reload = useCallback(() => setTick((n) => n + 1), []);
  useEffect(() => {
    if (!stage) return;
    let alive = true;
    api
      .inspection(vol)
      .then((x) => {
        if (!alive) return;
        setView(x);
        setMissing(false);
        setError(null);
      })
      .catch((e) => {
        if (!alive) return;
        if (e instanceof ApiError && e.status === 404) {
          setView(null);
          setMissing(true);
        } else setError(msg(e));
      });
    return () => {
      alive = false;
    };
  }, [vol, stage, tick, s?.stage]);

  const inspector = !!stage && can(me, stage.role);
  const cells = useMemo(() => (schema?.fields ?? []).filter((f) => f.level === "document"), [schema]);
  const label = (k: string) => cells.find((f) => f.name === k)?.label ?? k;
  const stageLabel = (name: string) => wf?.stages.find((x) => x.name === name)?.label ?? name;
  const docs = useMemo(() => view?.docs ?? [], [view]);
  const done = docs.filter((d) => d.inspected_at).length;
  const shown = useMemo(() => docs.filter((d) => !onlyLeft || !d.inspected_at), [docs, onlyLeft]);
  const cur = shown.find((d) => d.doc_id === sel) ?? shown[0];

  // 지금 문서의 수정 이력 (검사에서 고친 칸은 단계 '검사'로 남는다)
  const curId = cur?.doc_id;
  useEffect(() => {
    if (!curId) return;
    let alive = true;
    api
      .edits(vol, curId)
      .then((x) => alive && setEdits(x))
      .catch(() => alive && setEdits([]));
    return () => {
      alive = false;
    };
  }, [vol, curId, tick]);

  const create = async () => {
    const pct = (ratePct ?? "").trim();
    const rate = pct === "" ? undefined : Number(pct) / 100;
    if (rate !== undefined && !(rate >= 0 && rate <= 1)) {
      setNote("비율은 0~100(%) 사이 숫자");
      return;
    }
    setBusy(true);
    setNote(null);
    try {
      const r = await api.createInspection(vol, { rate, fields100 });
      setNote(`검사 목록을 만듦 — 문서 ${r.docs}건 (표시 ${r.plan.flagged} · 표본 ${r.plan.sample})`);
      reload();
    } catch (e) {
      setNote(`실패: ${msg(e)}`);
    } finally {
      setBusy(false);
    }
  };

  const value = (d: InspectionDoc, k: string) => fixes[d.doc_id]?.[k] ?? d.fields?.[k] ?? "";
  const setValue = (d: InspectionDoc, k: string, v: string) =>
    setFixes((prev) => {
      const next = { ...prev, [d.doc_id]: { ...prev[d.doc_id] } };
      if (v === (d.fields?.[k] ?? "")) delete next[d.doc_id][k];
      else next[d.doc_id][k] = v;
      if (!Object.keys(next[d.doc_id]).length) delete next[d.doc_id];
      return next;
    });

  const inspect = async (d: InspectionDoc, fix: boolean) => {
    setBusy(true);
    setNote(null);
    try {
      const r = await api.inspect(vol, d.doc_id, fix ? (fixes[d.doc_id] ?? {}) : {});
      setFixes((prev) => {
        const next = { ...prev };
        delete next[d.doc_id];
        return next;
      });
      setNote(
        r.fixed_fields.length
          ? `${d.doc_id} 고쳐서 저장 — ${r.fixed_fields.map(label).join(", ")}`
          : `${d.doc_id} 이상 없음`,
      );
      // 다음 남은 문서로
      const pos = shown.findIndex((x) => x.doc_id === d.doc_id);
      const next = [...shown.slice(pos + 1), ...shown.slice(0, Math.max(pos, 0))].find((x) => !x.inspected_at);
      setSel(onlyLeft ? (next?.doc_id ?? null) : d.doc_id);
      reload();
    } catch (e) {
      setNote(`실패: ${msg(e)}`);
      reload();
    } finally {
      setBusy(false);
    }
  };

  const dirty = cur ? Object.keys(fixes[cur.doc_id] ?? {}).length : 0;
  const checks = new Set(cur?.check_fields ?? []);
  const roleLabel = stage ? ROLE_LABEL[stage.role] : "";

  return (
    <div className="space-y-4">
      <ErrorNote error={error} />
      <div className="flex flex-wrap items-center gap-3">
        <Link href="/" className="text-sm text-slate-500 hover:text-slate-800">
          ← 대시보드
        </Link>
        <h1 className="font-mono text-lg font-semibold">{vol}</h1>
        {s && <StageBadge stage={s.stage} />}
        {view && (
          <button onClick={reload} className="ml-auto rounded border px-2 py-1 text-sm text-slate-600">
            새로 고침
          </button>
        )}
      </div>
      <VolumeTabs vol={vol} active="inspection" />
      {note && <div className="rounded-lg bg-slate-100 px-4 py-2 text-sm">{note}</div>}

      {wf && !stage && (
        <Card>
          <p className="text-sm text-slate-600">
            지금 검토 흐름(<b>{wf.name}</b>)에는 표본 검사 단계가 없습니다. 문서 단계 승인·반려는 결과·수정 탭에서 합니다.
          </p>
        </Card>
      )}

      {stage && missing && (
        <Card title="검사 목록 만들기">
          {s && s.stage !== "done" ? (
            <p className="text-sm text-slate-600">색인이 끝난 철만 검사 목록을 만들 수 있습니다.</p>
          ) : !inspector ? (
            <p className="text-sm text-slate-600">
              아직 검사 목록이 없습니다. 검사 목록은 {roleLabel} 역할이 만듭니다 (읽기 전용).
            </p>
          ) : (
            <div className="space-y-4 text-sm">
              <p className="text-slate-600">
                검토 표시가 붙은 문서는 전부, 표시 없는 문서는 비율만큼 무작위 표본을 검사합니다. 100% 검수 칸을 고르면
                나머지 문서도 모두 그 칸을 확인합니다.
              </p>
              <label className="flex items-center gap-2">
                <span className="w-28 font-medium">표본 비율 (%)</span>
                <input
                  type="number"
                  min={0}
                  max={100}
                  step="any"
                  value={ratePct ?? (stage.rate != null ? String(stage.rate * 100) : "")}
                  onChange={(e) => setRatePct(e.target.value)}
                  placeholder="서버 기본값"
                  className="w-32 rounded-md border border-slate-300 px-2 py-1"
                />
                <span className="text-xs text-slate-500">비워 두면 서버 기본 비율. 기본보다 낮추는 것은 관리 역할만</span>
              </label>
              <div className="flex flex-wrap items-center gap-3">
                <span className="w-28 font-medium">100% 검수 칸</span>
                {cells
                  .filter((f) => f.editable)
                  .map((f) => (
                    <label key={f.name} className="flex items-center gap-1">
                      <input
                        type="checkbox"
                        checked={fields100.includes(f.name)}
                        onChange={(e) =>
                          setFields100((prev) =>
                            e.target.checked ? [...prev, f.name] : prev.filter((x) => x !== f.name),
                          )
                        }
                      />
                      {f.label}
                    </label>
                  ))}
              </div>
              <button
                onClick={create}
                disabled={busy}
                className="rounded-md bg-slate-900 px-4 py-2 font-medium text-white disabled:opacity-30"
              >
                검사 목록 만들기
              </button>
            </div>
          )}
        </Card>
      )}

      {view && (
        <>
          <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4">
            <Stat label="검사함" value={`${done} / ${docs.length}`} sub={<Progress value={done} max={docs.length} tone="emerald" />} />
            <Stat label="남음" value={docs.length - done} />
            <Stat
              label="고친 문서"
              value={docs.filter((d) => d.fixed_fields?.length).length}
              sub={`표본 비율 ${(view.plan.rate * 100).toFixed(1)}% · 표시 없는 문서 ${view.plan.unflagged}건 중 ${view.plan.sample}건`}
            />
            <Stat
              label="100% 검수 칸"
              value={view.plan.fields100.length ? view.plan.fields100.map(label).join(", ") : "없음"}
            />
          </div>
          <Card title="이유별">
            <table className="w-full text-sm">
              <thead className="text-left text-xs text-slate-500">
                <tr>
                  <th className="py-1 font-medium">이유</th>
                  <th className="py-1 text-right font-medium">문서</th>
                  <th className="py-1 text-right font-medium">검사함</th>
                  <th className="py-1 text-right font-medium">남음</th>
                  <th className="py-1 text-right font-medium">고침</th>
                </tr>
              </thead>
              <tbody>
                {REASONS.map((r) => {
                  const rows = docs.filter((d) => d.reason === r);
                  const n = rows.filter((d) => d.inspected_at).length;
                  return (
                    <tr key={r} className="border-t border-slate-50 tabular-nums">
                      <td className="py-1">{REASON_LABEL[r]}</td>
                      <td className="py-1 text-right">{rows.length}</td>
                      <td className="py-1 text-right">{n}</td>
                      <td className="py-1 text-right">{rows.length - n}</td>
                      <td className="py-1 text-right">{rows.filter((d) => d.fixed_fields?.length).length}</td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </Card>

          <div className="grid gap-4 lg:grid-cols-[280px_1fr]">
            <Card>
              <label className="mb-2 flex items-center gap-1.5 text-sm text-slate-600">
                <input type="checkbox" checked={onlyLeft} onChange={(e) => setOnlyLeft(e.target.checked)} />
                남은 것만 ({docs.length - done}/{docs.length})
              </label>
              <ul className="max-h-[70vh] space-y-1 overflow-y-auto text-sm">
                {shown.map((d) => (
                  <li key={d.doc_id}>
                    <button
                      onClick={() => setSel(d.doc_id)}
                      className={`w-full rounded-md px-2 py-1.5 text-left ${cur?.doc_id === d.doc_id ? "bg-slate-900 text-white" : "hover:bg-slate-100"}`}
                    >
                      <div className="flex items-center justify-between font-mono text-xs">
                        <span>{d.doc_id.split("-").slice(-2).join("-")}</span>
                        <span className="flex gap-1">
                          <span className="rounded bg-slate-200 px-1 text-slate-700">{REASON_LABEL[d.reason]}</span>
                          {fixes[d.doc_id] && <span className="rounded bg-blue-500 px-1 text-white">수정</span>}
                          {d.inspected_at ? (
                            <span className="rounded bg-emerald-600 px-1 text-white">{d.fixed_fields?.length ? "고침" : "통과"}</span>
                          ) : (
                            !d.can_inspect && <span className="rounded bg-slate-400 px-1 text-white">막힘</span>
                          )}
                        </span>
                      </div>
                    </button>
                  </li>
                ))}
                {!shown.length && <li className="py-6 text-center text-slate-400">{docs.length ? "모두 검사함" : "없음"}</li>}
              </ul>
            </Card>

            {cur && (
              <Card title={`${cur.doc_id} · ${REASON_LABEL[cur.reason]}`}>
                <div className="grid gap-5 xl:grid-cols-[minmax(0,1fr)_minmax(0,1fr)]">
                  <div>
                    <div className="mb-1 text-xs text-slate-500">원본 머리부 (쪽 위 45%)</div>
                    {/* eslint-disable-next-line @next/next/no-img-element */}
                    <img
                      src={headImage(vol, cur.doc_id)}
                      alt="머리부"
                      className={`w-full rounded border border-slate-200 ${MASK ? "blur-[3px]" : ""}`}
                    />
                  </div>
                  <div className="space-y-4">
                    <div className="text-xs text-slate-500">확인할 칸 ({cur.check_fields.length})</div>
                    {cells
                      .filter((f) => checks.has(f.name))
                      .map((f) => {
                        const flags = cur.flags?.[f.flag_key];
                        const changed = fixes[cur.doc_id]?.[f.name] !== undefined;
                        return (
                          <div key={f.name}>
                            <div className="mb-1 flex items-center gap-2 text-sm">
                              <span className="w-16 font-medium">{f.label}</span>
                              <FlagChips flags={flags} machinePrefix={schema?.machine_flag_prefix} />
                            </div>
                            <input
                              value={value(cur, f.name)}
                              onChange={(e) => setValue(cur, f.name, e.target.value)}
                              disabled={!cur.can_inspect || !f.editable}
                              className={`w-full rounded-md border px-3 py-1.5 text-sm ${changed ? "border-blue-500 bg-blue-50" : "border-amber-400 bg-amber-50"}`}
                            />
                          </div>
                        );
                      })}
                    {cells.some((f) => !checks.has(f.name)) && (
                      <details className="text-xs text-slate-500">
                        <summary className="cursor-pointer">다른 칸 (확인 대상 아님)</summary>
                        <dl className="mt-1 grid grid-cols-[4rem_1fr] gap-x-2 gap-y-0.5">
                          {cells
                            .filter((f) => !checks.has(f.name))
                            .map((f) => (
                              <div key={f.name} className="contents">
                                <dt>{f.label}</dt>
                                <dd className="truncate">{cur.fields?.[f.name] || "–"}</dd>
                              </div>
                            ))}
                        </dl>
                      </details>
                    )}

                    {cur.can_inspect ? (
                      <div className="flex flex-wrap gap-2">
                        <button
                          onClick={() => inspect(cur, false)}
                          disabled={busy || dirty > 0}
                          title={dirty ? "고친 칸이 있으면 '고쳐서 저장'" : undefined}
                          className="rounded-md bg-emerald-600 px-4 py-2 text-sm font-medium text-white disabled:opacity-30"
                        >
                          이상 없음
                        </button>
                        <button
                          onClick={() => inspect(cur, true)}
                          disabled={busy || !dirty}
                          className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-30"
                        >
                          고쳐서 저장{dirty ? ` (${dirty}칸)` : ""}
                        </button>
                      </div>
                    ) : (
                      cur.block && (
                        <div className="rounded-lg border border-slate-200 bg-slate-50 px-3 py-2 text-sm text-slate-700">
                          {blockText(cur.block, cur, roleLabel)}
                          {cur.inspected_at && cur.fixed_fields && (
                            <span className="ml-1 text-slate-500">
                              · {cur.fixed_fields.length ? `고친 칸: ${cur.fixed_fields.map(label).join(", ")}` : "이상 없음"}
                            </span>
                          )}
                        </div>
                      )
                    )}

                    {edits.length > 0 && (
                      <div>
                        <div className="mb-1 text-xs text-slate-500">수정 이력</div>
                        <table className="w-full text-xs">
                          <tbody>
                            {edits.map((e, i) => (
                              <tr key={i} className="border-t border-slate-50">
                                <td className="py-0.5 pr-2 text-slate-400">
                                  {new Date(e.ts).toLocaleString("ko-KR", { hour12: false })}
                                </td>
                                <td className="py-0.5 pr-2">{stageLabel(e.stage)}</td>
                                <td className="py-0.5 pr-2">{label(e.field)}</td>
                                <td className="py-0.5 pr-2 font-mono">
                                  {e.old || "–"} → {e.new || "–"}
                                </td>
                                <td className="py-0.5 text-slate-500">{e.name || e.username || ""}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                </div>
              </Card>
            )}
          </div>
        </>
      )}
    </div>
  );
}
