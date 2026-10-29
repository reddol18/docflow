import type { ReactNode } from "react";
import { STAGE_LABEL, type Stage } from "@/lib/api";

const STAGE_STYLE: Record<Stage, string> = {
  new: "bg-slate-100 text-slate-600",
  queued: "bg-slate-100 text-slate-600",
  running: "bg-blue-100 text-blue-700",
  review: "bg-amber-100 text-amber-800",
  done: "bg-emerald-100 text-emerald-700",
  stopped: "bg-rose-100 text-rose-700",
};

export function StageBadge({ stage }: { stage: Stage }) {
  return (
    <span className={`inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ${STAGE_STYLE[stage]}`}>
      {stage === "running" && <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-blue-600" />}
      {STAGE_LABEL[stage]}
    </span>
  );
}

export function Card({ title, children, right }: { title?: string; children: ReactNode; right?: ReactNode }) {
  return (
    <section className="rounded-xl border border-slate-200 bg-white p-5 shadow-sm">
      {(title || right) && (
        <div className="mb-4 flex items-center justify-between">
          {title && <h2 className="font-semibold">{title}</h2>}
          {right}
        </div>
      )}
      {children}
    </section>
  );
}

export function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
      <div className="text-xs text-slate-500">{label}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
      {sub && <div className="mt-0.5 text-xs text-slate-400">{sub}</div>}
    </div>
  );
}

export function Progress({ value, max, tone = "blue" }: { value: number; max: number; tone?: "blue" | "emerald" | "amber" }) {
  const pct = max ? Math.min(100, (value / max) * 100) : 0;
  const color = { blue: "bg-blue-500", emerald: "bg-emerald-500", amber: "bg-amber-500" }[tone];
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-slate-100">
      <div className={`h-full ${color} transition-all`} style={{ width: `${pct}%` }} />
    </div>
  );
}

export function FlagChips({ flags }: { flags?: string[] }) {
  if (!flags?.length) return null;
  return (
    <span className="flex flex-wrap gap-1">
      {flags.map((f) => (
        <span key={f} className="rounded bg-rose-50 px-1.5 py-0.5 text-[11px] font-medium text-rose-700 ring-1 ring-rose-200">
          {f}
        </span>
      ))}
    </span>
  );
}

export function ErrorNote({ error }: { error: string | null }) {
  if (!error) return null;
  return <div className="rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700">API 오류: {error}</div>;
}

export const usd = (n: number) => `$${n.toFixed(n < 0.1 ? 4 : 2)}`;
