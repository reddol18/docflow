"use client";

// 결과·수정 화면과 검사 화면이 같이 쓰는 것: 스키마·흐름 읽기, 고친 칸 추적, 다음 문서 고르기
import { useEffect, useState } from "react";
import { api, type Fields, type Schema, type Workflow } from "@/lib/api";

export const errMsg = (e: unknown) => (e instanceof Error ? e.message : String(e));

/** 색인 스키마(GET /schema)와 검토 흐름(GET /workflow)을 한 번 읽는다. 오류는 출처별로 모아 한 줄로 */
export function useSchemaWorkflow() {
  const [schema, setSchema] = useState<Schema | null>(null);
  const [wf, setWf] = useState<Workflow | null>(null);
  const [errs, setErrs] = useState<{ schema?: string; workflow?: string }>({});
  useEffect(() => {
    let alive = true;
    api
      .schema()
      .then((x) => alive && setSchema(x))
      .catch((e) => alive && setErrs((p) => ({ ...p, schema: `스키마: ${errMsg(e)}` })));
    api
      .workflow()
      .then((x) => alive && setWf(x))
      .catch((e) => alive && setErrs((p) => ({ ...p, workflow: `흐름: ${errMsg(e)}` })));
    return () => {
      alive = false;
    };
  }, []);
  const error = [errs.schema, errs.workflow].filter(Boolean).join(" · ") || null;
  return { schema, wf, error };
}

type Editable = { doc_id: string; fields: Fields | null };

/** 문서마다 고친 칸 {doc_id: {칸: 새 값}} — 원래 값으로 되돌리면 빠진다 */
export function useFieldFixes() {
  const [fixes, setFixes] = useState<Record<string, Partial<Fields>>>({});
  const value = (d: Editable, k: string) => fixes[d.doc_id]?.[k] ?? d.fields?.[k] ?? "";
  const setValue = (d: Editable, k: string, v: string) =>
    setFixes((prev) => {
      const next = { ...prev, [d.doc_id]: { ...prev[d.doc_id] } };
      if (v === (d.fields?.[k] ?? "")) delete next[d.doc_id][k];
      else next[d.doc_id][k] = v;
      if (!Object.keys(next[d.doc_id]).length) delete next[d.doc_id];
      return next;
    });
  const drop = (docId: string) =>
    setFixes((prev) => {
      const next = { ...prev };
      delete next[docId];
      return next;
    });
  const clear = () => setFixes({});
  return { fixes, value, setValue, drop, clear };
}

/** list에서 id 다음부터 한 바퀴 돌며 pred를 만족하는 첫 문서 (id 자신은 빼고) */
export function nextAfter<T extends { doc_id: string }>(list: T[], id: string, pred: (x: T) => boolean) {
  const pos = list.findIndex((x) => x.doc_id === id);
  return [...list.slice(pos + 1), ...list.slice(0, Math.max(pos, 0))].find(pred);
}
