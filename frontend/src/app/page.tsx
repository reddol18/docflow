"use client";

import Link from "next/link";
import { api } from "@/lib/api";
import { usePoll } from "@/lib/usePoll";
import { Card, ErrorNote, Progress, StageBadge, Stat, usd } from "@/components/ui";
import { UploadPanel } from "@/components/UploadPanel";

export default function Dashboard() {
  const { data: vols, error, refresh } = usePoll(api.volumes, 3000);
  const list = vols ?? [];
  const pages = list.reduce((s, v) => s + v.uploaded, 0);
  const ocr = list.reduce((s, v) => s + v.ocr_done, 0);
  const docs = list.reduce((s, v) => s + v.documents, 0);
  const flagged = list.reduce((s, v) => s + v.flagged_documents, 0);
  const spent = list[0]?.spent_today_usd ?? 0;
  const cost = list.reduce((s, v) => s + v.usage.reduce((a, u) => a + u.usd, 0), 0);
  const waiting = list.filter((v) => v.stage === "review");

  return (
    <div className="space-y-6">
      <ErrorNote error={error} />
      <div className="grid grid-cols-2 gap-4 md:grid-cols-5">
        <Stat label="철" value={list.length} sub={`검토 대기 ${waiting.length}`} />
        <Stat label="쪽 (OCR 완료 / 올림)" value={`${ocr} / ${pages}`} />
        <Stat label="문서" value={docs} sub="문서 시작 쪽 판별 결과" />
        <Stat
          label="검토 필요 문서"
          value={flagged}
          sub={docs ? `${Math.round((flagged / docs) * 100)}% · 나머지는 자동 통과` : undefined}
        />
        <Stat label="GPT 비용 (누적)" value={usd(cost)} sub={`오늘 ${usd(spent)} / 한도 $3`} />
      </div>

      {waiting.length > 0 && (
        <div className="rounded-xl border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900">
          사람 검토를 기다리는 철:{" "}
          {waiting.map((v) => (
            <Link key={v.vol} href={`/volumes/${encodeURIComponent(v.vol)}/documents`} className="mr-3 font-medium underline">
              {v.vol} ({v.flagged_documents}건)
            </Link>
          ))}
        </div>
      )}

      <UploadPanel onDone={refresh} />

      <Card title="철 목록">
        <div className="overflow-x-auto">
          <table className="w-full text-sm">
            <thead className="text-left text-xs text-slate-500">
              <tr className="border-b border-slate-100">
                <th className="py-2 pr-4 font-medium">철</th>
                <th className="py-2 pr-4 font-medium">상태</th>
                <th className="w-56 py-2 pr-4 font-medium">OCR</th>
                <th className="py-2 pr-4 text-right font-medium">문서</th>
                <th className="py-2 pr-4 text-right font-medium">검토 필요</th>
                <th className="py-2 pr-4 text-right font-medium">비용</th>
                <th className="py-2 font-medium" />
              </tr>
            </thead>
            <tbody>
              {list.map((v) => (
                <tr key={v.vol} className="border-b border-slate-50 last:border-0">
                  <td className="py-2.5 pr-4 font-mono text-xs">{v.vol}</td>
                  <td className="py-2.5 pr-4">
                    <StageBadge stage={v.stage} />
                  </td>
                  <td className="py-2.5 pr-4">
                    <div className="flex items-center gap-2">
                      <Progress value={v.ocr_done} max={v.uploaded} tone={v.ocr_done === v.uploaded ? "emerald" : "blue"} />
                      <span className="whitespace-nowrap text-xs tabular-nums text-slate-500">
                        {v.ocr_done}/{v.uploaded}
                      </span>
                    </div>
                  </td>
                  <td className="py-2.5 pr-4 text-right tabular-nums">{v.documents || "–"}</td>
                  <td className="py-2.5 pr-4 text-right tabular-nums">
                    {v.documents ? `${v.flagged_documents} (${Math.round((v.flagged_documents / v.documents) * 100)}%)` : "–"}
                  </td>
                  <td className="py-2.5 pr-4 text-right tabular-nums">{usd(v.usage.reduce((a, u) => a + u.usd, 0))}</td>
                  <td className="py-2.5 text-right whitespace-nowrap">
                    <Link href={`/volumes/${encodeURIComponent(v.vol)}`} className="mr-3 text-blue-600 hover:underline">
                      진행
                    </Link>
                    <Link href={`/volumes/${encodeURIComponent(v.vol)}/documents`} className="text-blue-600 hover:underline">
                      결과·수정
                    </Link>
                  </td>
                </tr>
              ))}
              {!list.length && (
                <tr>
                  <td colSpan={7} className="py-8 text-center text-slate-400">
                    {vols ? "올린 철이 없습니다" : "불러오는 중…"}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </Card>
    </div>
  );
}
