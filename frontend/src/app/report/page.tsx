"use client";

import { useState } from "react";
import { api, reportCsv, type DailyRow } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { Card, ErrorNote, Progress, StageBadge, Stat, usd } from "@/components/ui";

const pct = (n: number | null | undefined) => (n == null ? "–" : `${Math.round(n * 100)}%`);
const num = (n: number | null | undefined) => (n == null || n === 0 ? "–" : n.toLocaleString());

const DAILY: { key: keyof DailyRow; label: string; fmt?: (r: DailyRow) => string }[] = [
  { key: "pages_ocr", label: "OCR 면" },
  { key: "docs_extracted", label: "추출 건" },
  { key: "docs_indexed", label: "색인 건" },
  { key: "docs_flagged", label: "검토 표시 건" },
  { key: "flag_rate", label: "표시 비율", fmt: (r) => pct(r.flag_rate) },
  { key: "docs_reviewed", label: "검토 건" },
  { key: "cells_reviewed", label: "표시된 칸" },
  { key: "cells_fixed", label: "고친 칸" },
  { key: "fix_rate", label: "수정률", fmt: (r) => pct(r.fix_rate) },
  { key: "cells_edited", label: "완료 뒤 고침" },
  { key: "review_sec_median", label: "건당 검토(초)", fmt: (r) => (r.review_sec_median == null ? "–" : String(r.review_sec_median)) },
  { key: "review_min_total", label: "검토(분)" },
  { key: "usd", label: "비용", fmt: (r) => (r.usd ? usd(r.usd) : "–") },
];

export default function ReportPage() {
  const [from, setFrom] = useState("");
  const [to, setTo] = useState("");
  const { data: r, error } = usePoll(() => api.report(from || undefined, to || undefined), 15000, [from, to]);
  const t = r?.volume.total;
  const rt = r?.review_time.review;
  const days = r ? [...r.daily].reverse() : [];
  const sum = (k: keyof DailyRow) => days.reduce((s, d) => s + (Number(d[k]) || 0), 0);
  const cellsReviewed = sum("cells_reviewed");

  return (
    <div className="space-y-6">
      <ErrorNote error={error} />
      <div className="flex flex-wrap items-end gap-3">
        <h1 className="text-lg font-semibold">공정·품질 리포트</h1>
        <label className="ml-auto text-xs text-slate-500">
          시작
          <input type="date" value={from} onChange={(e) => setFrom(e.target.value)} className="ml-1 rounded border px-2 py-1 text-sm" />
        </label>
        <label className="text-xs text-slate-500">
          끝
          <input type="date" value={to} onChange={(e) => setTo(e.target.value)} className="ml-1 rounded border px-2 py-1 text-sm" />
        </label>
        <a href={reportCsv(from || undefined, to || undefined)} className="rounded-md bg-slate-900 px-3 py-1.5 text-sm font-medium text-white">
          CSV 내려받기
        </a>
      </div>

      <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
        <Stat label="철 (완료 / 전체)" value={t ? `${t.volumes_done} / ${t.volumes}` : "–"} />
        <Stat label="면 (OCR 끝 / 올림)" value={t ? `${t.pages_ocr} / ${t.pages}` : "–"} sub={t ? `남은 면 ${t.pages_left}` : undefined} />
        <Stat label="건 (색인 끝 / 추출)" value={t ? `${t.docs_indexed} / ${t.docs}` : "–"} sub={t ? `남은 건 ${t.docs_left}` : undefined} />
        <Stat
          label="검토 수정률 (기간)"
          value={cellsReviewed ? pct(sum("cells_fixed") / cellsReviewed) : "–"}
          sub={cellsReviewed ? `표시된 칸 ${cellsReviewed} 중 고친 칸 ${sum("cells_fixed")}` : "검토 제출 기록 없음"}
        />
        <Stat
          label="건당 검토 시간 (중앙값)"
          value={rt ? `${rt.median_sec}초` : "–"}
          sub={rt ? `${rt.docs}건 · 합계 ${rt.total_min}분 · 자리 비움 제외` : "검토 화면에서 잰 기록 없음"}
        />
      </div>

      <Card title={`날짜별 (${r?.day_from ?? ""} ~ ${r?.day_to ?? ""})`}>
        <div className="overflow-x-auto">
          <table className="w-full text-sm tabular-nums">
            <thead className="text-left text-xs text-slate-500">
              <tr className="border-b border-slate-100">
                <th className="py-2 pr-4 font-medium">날짜</th>
                {DAILY.map((c) => (
                  <th key={c.key} className="py-2 pr-3 text-right font-medium whitespace-nowrap">
                    {c.label}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {days.map((d) => (
                <tr key={d.day} className="border-b border-slate-50 last:border-0">
                  <td className="py-2 pr-4 font-mono text-xs">{d.day}</td>
                  {DAILY.map((c) => (
                    <td key={c.key} className="py-2 pr-3 text-right">
                      {c.fmt ? c.fmt(d) : num(d[c.key] as number)}
                    </td>
                  ))}
                </tr>
              ))}
              {!days.length && (
                <tr>
                  <td colSpan={DAILY.length + 1} className="py-8 text-center text-slate-400">
                    {r ? "기록 없음" : "불러오는 중…"}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        <p className="mt-3 text-xs text-slate-400">
          표시 비율 = 그날 색인된 문서 중 검토 표시된 문서. 수정률 = 검토 제출 때 고친 칸 / 검토 표시된 칸 (표시 안 된 칸의
          오류는 빠진다). 검사(2인 검증) 단계가 생기면 검사 오류율로 바꾼다. 검토 시간은 검토 제출·문서 저장으로 확정한 것만 센다.
          날짜는 KST.
        </p>
      </Card>

      <Card title="철별 물량">
        <div className="overflow-x-auto">
          <table className="w-full text-sm tabular-nums">
            <thead className="text-left text-xs text-slate-500">
              <tr className="border-b border-slate-100">
                <th className="py-2 pr-4 font-medium">철</th>
                <th className="py-2 pr-4 font-medium">상태</th>
                <th className="w-48 py-2 pr-4 font-medium">OCR 면</th>
                <th className="py-2 pr-4 text-right font-medium">건</th>
                <th className="py-2 pr-4 text-right font-medium">검토 표시</th>
                <th className="py-2 text-right font-medium">색인</th>
              </tr>
            </thead>
            <tbody>
              {(r?.volume.volumes ?? []).map((v) => (
                <tr key={v.vol} className="border-b border-slate-50 last:border-0">
                  <td className="py-2 pr-4 font-mono text-xs">{v.vol}</td>
                  <td className="py-2 pr-4">
                    <StageBadge stage={v.stage} />
                  </td>
                  <td className="py-2 pr-4">
                    <div className="flex items-center gap-2">
                      <Progress value={v.pages_ocr} max={v.pages} tone={v.pages_ocr === v.pages ? "emerald" : "blue"} />
                      <span className="text-xs whitespace-nowrap text-slate-500">
                        {v.pages_ocr}/{v.pages}
                      </span>
                    </div>
                  </td>
                  <td className="py-2 pr-4 text-right">{num(v.docs)}</td>
                  <td className="py-2 pr-4 text-right">
                    {v.docs ? `${v.docs_flagged} (${Math.round((v.docs_flagged / v.docs) * 100)}%)` : "–"}
                  </td>
                  <td className="py-2 text-right">{num(v.docs_indexed)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
