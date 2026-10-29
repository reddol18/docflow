"use client";

import { useEffect, useRef, useState } from "react";
import { api, type GraphEvent } from "@/lib/api";

/** 철의 노드 이벤트를 id 순서로 이어 받는다 (after=마지막 id). */
export function useEvents(vol: string, ms = 2000) {
  // 철이 바뀌면 이전 철 이벤트를 버리도록 철 이름과 함께 들고 있는다
  const [state, setState] = useState<{ vol: string; list: GraphEvent[] }>({ vol, list: [] });
  const last = useRef(0);

  useEffect(() => {
    let stop = false;
    last.current = 0;
    const tick = async () => {
      try {
        // 한 번에 500개씩 오므로 밀린 만큼 이어서 읽는다
        for (let i = 0; i < 20 && !stop; i++) {
          const batch = await api.events(vol, last.current);
          if (stop || !batch.length) break;
          last.current = batch[batch.length - 1].id;
          setState((prev) => ({ vol, list: prev.vol === vol ? [...prev.list, ...batch] : batch }));
          if (batch.length < 500) break;
        }
      } catch {}
    };
    tick();
    const t = setInterval(tick, ms);
    return () => {
      stop = true;
      clearInterval(t);
    };
  }, [vol, ms]);

  return state.vol === vol ? state.list : [];
}

/** 가장 최근 '새 실행' 이후의 이벤트만 (resume이 아닌 run start 기준). */
export function currentRun(events: GraphEvent[]) {
  let start = 0;
  events.forEach((e, i) => {
    if (e.node === "run" && e.phase === "start" && !(e.info as { resume?: boolean } | null)?.resume) start = i;
  });
  return events.slice(start);
}
