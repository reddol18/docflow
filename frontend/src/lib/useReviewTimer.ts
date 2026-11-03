"use client";

import { useCallback, useEffect, useRef } from "react";
import { api, type Timing } from "@/lib/api";

// 이만큼 입력(마우스·키보드·스크롤)이 없으면 자리 비움으로 보고 시간을 멈춘다
const IDLE_MS = 60_000;
const TICK_MS = 1_000;

type Acc = { ms: number; openedAt: string };

/**
 * 검토 화면에서 지금 보고 있는 문서(docId)의 실제 작업 시간을 잰다.
 * 탭이 숨겨졌거나 창이 포커스를 잃었거나 60초 동안 입력이 없으면 세지 않는다.
 * flush()로 모은 시간을 서버에 보내고 비운다. 페이지를 떠날 때도 보낸다(keepalive).
 */
export function useReviewTimer(vol: string, docId: string | undefined, mode: Timing["mode"], enabled: boolean) {
  const acc = useRef<Record<string, Acc>>({});
  const lastInput = useRef(0);
  const cur = useRef(docId);
  const modeRef = useRef(mode);
  useEffect(() => {
    cur.current = docId;
    modeRef.current = mode;
  }, [docId, mode]);

  useEffect(() => {
    const mark = () => (lastInput.current = Date.now());
    const evs = ["mousemove", "mousedown", "keydown", "wheel", "scroll", "touchstart"] as const;
    evs.forEach((e) => window.addEventListener(e, mark, { passive: true }));
    mark(); // 화면을 연 순간은 작업 중으로 본다
    let last = Date.now();
    const t = window.setInterval(() => {
      const now = Date.now();
      const dt = Math.min(now - last, TICK_MS * 2); // 탭이 잠들었다 깨어난 큰 간격은 버린다
      last = now;
      const id = cur.current;
      const active =
        enabled && id && document.visibilityState === "visible" && document.hasFocus() && now - lastInput.current < IDLE_MS;
      if (!active) return;
      const a = (acc.current[id] ??= { ms: 0, openedAt: new Date().toISOString() });
      a.ms += dt;
    }, TICK_MS);
    return () => {
      window.clearInterval(t);
      evs.forEach((e) => window.removeEventListener(e, mark));
    };
  }, [enabled]);

  const take = useCallback((only?: string[], changed?: Record<string, number>): Timing[] => {
    const ids = only ?? Object.keys(acc.current);
    const rows: Timing[] = [];
    for (const id of ids) {
      const a = acc.current[id];
      if (!a) continue;
      rows.push({
        doc_id: id,
        mode: modeRef.current,
        active_ms: Math.round(a.ms),
        opened_at: a.openedAt,
        fields_changed: Math.min(changed?.[id] ?? 0, 10),
      });
      delete acc.current[id];
    }
    return rows;
  }, []);

  /** 모은 시간을 보낸다. only를 주면 그 문서만. 실패해도 검토 흐름은 막지 않는다. */
  const flush = useCallback(
    async (only?: string[], changed?: Record<string, number>) => {
      const rows = take(only, changed);
      if (!rows.length) return;
      try {
        await api.timings(vol, rows);
      } catch {
        // 읽기 전용 외부 접속 등에서는 저장이 막힌다. 시간 기록은 부가 정보라 무시한다.
      }
    },
    [vol, take],
  );

  useEffect(() => {
    const onHide = () => {
      const rows = take();
      if (rows.length) api.timings(vol, rows, true).catch(() => {});
    };
    window.addEventListener("pagehide", onHide);
    return () => {
      window.removeEventListener("pagehide", onHide);
      onHide(); // 화면 안에서 다른 페이지로 옮길 때
    };
  }, [vol, take]);

  return { flush };
}
