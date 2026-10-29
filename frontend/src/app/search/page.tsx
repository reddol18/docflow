"use client";

import Link from "next/link";
import { useSearchParams } from "next/navigation";
import { Suspense, useEffect, useState } from "react";
import { api, type SearchHit } from "@/lib/api";
import { Card } from "@/components/ui";

const EXAMPLES = ["공장설립 변경 신고", "한국전력공사 송전철탑", "1997년 도로 점용 허가", "폐기물 처리 사업장"];

export default function SearchPage() {
  return (
    <Suspense>
      <Search />
    </Suspense>
  );
}

function Search() {
  // ?q=… 로 열면 바로 검색 (링크 공유·캡처용)
  const initial = useSearchParams().get("q") ?? "";
  const [q, setQ] = useState(initial);
  const [yf, setYf] = useState("");
  const [yt, setYt] = useState("");
  const [hits, setHits] = useState<SearchHit[] | null>(null);
  const [answer, setAnswer] = useState<{ text: string; sources: SearchHit[] } | null>(null);
  const [busy, setBusy] = useState<"search" | "ask" | null>(null);
  const [err, setErr] = useState<string | null>(null);

  const run = async (kind: "search" | "ask", query = q) => {
    if (!query.trim()) return;
    setQ(query);
    setBusy(kind);
    setErr(null);
    try {
      if (kind === "search") {
        setHits(await api.search(query, yf, yt));
        setAnswer(null);
      } else {
        const r = await api.ask(query);
        setAnswer({ text: r.answer, sources: r.sources });
        setHits(r.sources);
      }
    } catch (e) {
      setErr(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(null);
    }
  };

  useEffect(() => {
    if (!initial) return;
    api.search(initial, "", "").then(setHits, (e) => setErr(String(e)));
  }, [initial]);

  return (
    <div className="space-y-6">
      <Card title="문서 검색 — Qdrant 벡터 + Postgres 낱말 (RRF로 합침)">
        <div className="flex flex-wrap gap-2">
          <input
            value={q}
            onChange={(e) => setQ(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && run("search")}
            placeholder="예: 한국전력공사에 보낸 원상복구 통보"
            className="min-w-[320px] flex-1 rounded-md border border-slate-300 px-3 py-2"
          />
          <input value={yf} onChange={(e) => setYf(e.target.value)} placeholder="연도부터" className="w-24 rounded-md border border-slate-300 px-2" />
          <input value={yt} onChange={(e) => setYt(e.target.value)} placeholder="연도까지" className="w-24 rounded-md border border-slate-300 px-2" />
          <button onClick={() => run("search")} disabled={!!busy} className="rounded-md bg-slate-900 px-4 py-2 text-sm text-white">
            {busy === "search" ? "찾는 중…" : "검색"}
          </button>
          <button onClick={() => run("ask")} disabled={!!busy} className="rounded-md bg-blue-600 px-4 py-2 text-sm text-white">
            {busy === "ask" ? "답하는 중…" : "질문하기 (GPT)"}
          </button>
        </div>
        <div className="mt-2 flex flex-wrap gap-2 text-xs">
          {EXAMPLES.map((x) => (
            <button key={x} onClick={() => run("search", x)} className="rounded-full bg-slate-100 px-2.5 py-1 text-slate-600 hover:bg-slate-200">
              {x}
            </button>
          ))}
        </div>
        {err && <p className="mt-2 text-sm text-rose-600">{err}</p>}
      </Card>

      {answer && (
        <Card title="답변 (검색 상위 5건만 근거로)">
          <p className="whitespace-pre-wrap text-sm leading-relaxed">{answer.text}</p>
        </Card>
      )}

      {hits && (
        <Card title={`결과 ${hits.length}건`}>
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-slate-500">
              <tr>
                <th className="py-1.5 pr-3 font-medium">순위 (벡터/낱말)</th>
                <th className="py-1.5 pr-3 font-medium">시행일자</th>
                <th className="py-1.5 pr-3 font-medium">문서번호</th>
                <th className="py-1.5 pr-3 font-medium">제목</th>
                <th className="py-1.5 font-medium">수신</th>
              </tr>
            </thead>
            <tbody>
              {hits.map((h, i) => (
                <tr key={h.doc_id} className="border-t border-slate-50 align-top">
                  <td className="py-2 pr-3 text-xs tabular-nums text-slate-500">
                    {i + 1} ({h.vector_rank ?? "–"}/{h.keyword_rank ?? "–"})
                  </td>
                  <td className="py-2 pr-3 whitespace-nowrap tabular-nums">{h.date}</td>
                  <td className="py-2 pr-3 whitespace-nowrap font-mono text-xs">{h.docno}</td>
                  <td className="py-2 pr-3">
                    <Link href={`/volumes/${encodeURIComponent(h.vol)}/documents`} className="text-blue-700 hover:underline">
                      {h.title}
                    </Link>
                    <div className="font-mono text-[11px] text-slate-400">{h.doc_id}</div>
                  </td>
                  <td className="py-2 text-slate-600">{h.recipient}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}
    </div>
  );
}
