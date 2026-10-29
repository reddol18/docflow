/*
 * 게이트웨이 시나리오 실행기. breakscale_wrap의 lab 모듈을 가져다 쓰기만 한다(수정 없음).
 * 실행: cd <breakscale_wrap>; bun <이 파일> <시나리오.json> [구간초=60]
 *   BSW 환경변수로 breakscale_wrap 경로를 바꿀 수 있다(기본 E:/claude-dev/base/breakscale_lab). SEED로 난수 seed(기본 1).
 * 출력: 구간별로 클라이언트마다 완료·실패, 노드마다 대기 수. 끝에 실패 이유 합계.
 *   엔진 실측: 완료·실패·대기·실패 이유. lab 추정: CPU 공유 배율(x, specs의 host가 같은 노드끼리).
 */
import { readFileSync } from 'node:fs';

const BSW = process.env.BSW ?? 'E:/claude-dev/base/breakscale_lab';
const { runLab } = await import(`${BSW}/lab/core/run.ts`);
const { CpuContention } = await import(`${BSW}/lab/core/cpu.ts`);
const { fromScenarioFile } = await import(`${BSW}/lab/core/scenario-file.ts`);

const [file, win = '60'] = process.argv.slice(2);
const scn = fromScenarioFile(JSON.parse(readFileSync(file, 'utf8')));
const every = Number(win) * 1000;
const nodes = (scn.design as { nodes: { id: string; kind: string }[] }).nodes;
const clients = nodes.filter((n) => n.kind === 'client').map((n) => n.id);
const others = nodes.filter((n) => n.kind !== 'client').map((n) => n.id);
const cpu = new CpuContention(scn.specs, scn.hosts);
const prev: Record<string, { ok: number; fail: number }> = {};
let next = every;

const r = await runLab(scn.design, {
  seconds: scn.seconds, seed: Number(process.env.SEED ?? 1), budgetMs: 600_000, tickMs: 50, controller: cpu,
  onFrame(e: any, t: number) {
    scn.step(e, t);
    if (t < next) return;
    next += every;
    const s = e.snapshot();
    const cs = clients.map((id) => {
      const n = s.nodes[id];
      const p = prev[id] ?? { ok: 0, fail: 0 };
      prev[id] = { ok: n.totalCompleted, fail: n.totalFailed };
      return `${id} 완료${String(n.totalCompleted - p.ok).padStart(3)} 실패${String(n.totalFailed - p.fail).padStart(3)} p95 ${(n.p95 / 1000).toFixed(1).padStart(5)}s`;
    });
    const ns = others.map((id) => {
      const f = cpu.state.get(id)?.factor ?? 1;
      return `${id} 대기${String(s.nodes[id].queued).padStart(4)}${f > 1.01 ? ` x${f.toFixed(2)}` : ''}`;
    });
    console.log(`t=${String(Math.round(t / 1000)).padStart(3)}s | ${cs.join(' | ')} || ${ns.join(' | ')}`);
  },
});

console.log('\n클라이언트별 합계 (엔진 실측)');
for (const id of clients) {
  const n = r.snapshot.nodes[id];
  console.log(`  ${id}: 완료 ${n.totalCompleted} · 실패 ${n.totalFailed}`);
}
console.log('노드별 합계 (엔진 실측)');
for (const id of others) {
  const n = r.snapshot.nodes[id];
  console.log(`  ${id}: 완료 ${n.totalCompleted} · 실패 ${n.totalFailed}`);
}
const reasons =Object.entries(r.snapshot.failuresByReason).filter(([, v]) => (v as number) > 0);
console.log(`실패 이유: ${reasons.map(([k, v]) => `${k} ${v}`).join(', ') || '없음'}`);
