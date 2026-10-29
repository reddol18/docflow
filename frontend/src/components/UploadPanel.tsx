"use client";

import { useRouter } from "next/navigation";
import { useState } from "react";
import { api } from "@/lib/api";
import { Card, Progress } from "@/components/ui";

const BATCH = 10; // 한 요청에 10장씩 (쪽 이미지 약 0.8MB)

/** 철 이름 + 쪽 이미지(jpg)를 올리고 흐름을 시작한다. */
export function UploadPanel({ onDone }: { onDone: () => void }) {
  const router = useRouter();
  const [vol, setVol] = useState("");
  const [files, setFiles] = useState<File[]>([]);
  const [mode, setMode] = useState<"human" | "auto">("human");
  const [sent, setSent] = useState(0);
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState<string | null>(null);

  const pick = (list: FileList | null) => {
    const fs = Array.from(list ?? []).filter((f) => /\.jpe?g$/i.test(f.name)).sort((a, b) => a.name.localeCompare(b.name));
    setFiles(fs);
    // 파일 이름이 '<철>-<쪽>.jpg'면 철 이름을 채워 준다
    if (!vol && fs[0]) setVol(fs[0].name.replace(/-\d+\.jpe?g$/i, ""));
  };

  const start = async () => {
    if (!vol || !files.length) return;
    setBusy(true);
    setMsg(null);
    setSent(0);
    try {
      for (let i = 0; i < files.length; i += BATCH) {
        await api.upload(vol, files.slice(i, i + BATCH));
        setSent(Math.min(files.length, i + BATCH));
      }
      await api.run(vol, mode);
      onDone();
      router.push(`/volumes/${encodeURIComponent(vol)}`);
    } catch (e) {
      setMsg(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card title="새 철 올리기">
      <div className="grid gap-3 md:grid-cols-[1fr_1fr_auto_auto] md:items-end">
        <label className="text-sm">
          <span className="mb-1 block text-slate-500">쪽 이미지 (jpg, 여러 장)</span>
          <input
            type="file"
            multiple
            accept="image/jpeg"
            onChange={(e) => pick(e.target.files)}
            className="block w-full text-sm file:mr-3 file:rounded-md file:border-0 file:bg-slate-100 file:px-3 file:py-1.5"
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-slate-500">철 이름</span>
          <input
            value={vol}
            onChange={(e) => setVol(e.target.value)}
            placeholder="예: 5350109-1997-0002"
            className="w-full rounded-md border border-slate-300 px-3 py-1.5"
          />
        </label>
        <label className="text-sm">
          <span className="mb-1 block text-slate-500">검토</span>
          <select
            value={mode}
            onChange={(e) => setMode(e.target.value as "human" | "auto")}
            className="rounded-md border border-slate-300 px-2 py-1.5"
          >
            <option value="human">사람 검토에서 멈춤</option>
            <option value="auto">검토 없이 색인 (평가용)</option>
          </select>
        </label>
        <button
          onClick={start}
          disabled={busy || !vol || !files.length}
          className="rounded-md bg-slate-900 px-4 py-2 text-sm font-medium text-white disabled:opacity-40"
        >
          {busy ? "올리는 중…" : `${files.length}쪽 올리고 시작`}
        </button>
      </div>
      {busy && (
        <div className="mt-3 flex items-center gap-3 text-xs text-slate-500">
          <Progress value={sent} max={files.length} />
          <span className="whitespace-nowrap tabular-nums">
            {sent}/{files.length}
          </span>
        </div>
      )}
      {msg && <p className="mt-2 text-sm text-rose-600">{msg}</p>}
    </Card>
  );
}
