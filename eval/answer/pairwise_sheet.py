"""쌍별 심판 사람 확인표 — 쌍별 결과(pairwise.py)에서 약 20쌍을 골라 심판 판정을 가린 채점 파일을 만든다
(https://github.com/reddol18/docflow/issues/22 조건 3, 2026-11-24 사용자 결정 3).

고르기 (seed로 늘 같게):
  두 답 글이 같은 쌍(identical — 심판을 부르지 않은 쌍)과 심판 오류 쌍은 뺀다 (사람에게 보여 줄 차이가 없거나 짝 지을 판정이 없다).
  남은 쌍을 심판 결과(a 승·b 승·무승부)별로 나눠 섞은 뒤 돌아가며 하나씩 뽑는다 — 한 결과로 몰리면 kappa를 믿기 어려워서
  (#21 쏠림 규칙). 표에는 심판 결과가 들어가지 않는다 (고르는 데만 쓴다).
가린 채점: 쌍마다 답 A·답 B 자리를 섞는다 (a가 A인 쌍이 절반). 모델 이름·심판 판정·이유는 표·채점 페이지에 넣지 않는다.
  자리 열쇠(답 A가 a인지 b인지)는 따로 _key/ 폴더의 JSON에만 — 채점이 끝날 때까지 열지 않는다.
사람 값: A / B / 비김 (pairwise_agreement.py가 열쇠로 a·b·tie로 바꿔 심판 결과와 맞춘다).
결과 (저장소 밖, 이미 있으면 덮지 않는다): <답변 폴더>/human/<쌍별 label>_pw_human.csv (빈 판정 칸),
  같은 이름 _pw_score.html (혼자 도는 채점 페이지 — 버튼으로 매기고 'CSV 저장'으로 같은 칸의 CSV를 받는다. 엑셀 필요 없음),
  <답변 폴더>/human/_key/<쌍별 label>_pw_key.json.

실행 (표준 라이브러리만):
  python -I -X utf8 eval/answer/pairwise_sheet.py --pairwise <work>\\answers\\pairwise\\<label>.json [--n 20] [--seed 22]
"""
import argparse
import csv
import hashlib
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I: 같은 폴더

from human_sheet import APP_ROOT, csv_safe  # noqa: E402

COLUMNS = ["id", "순번", "답 있음", "질문", "참고 답", "답 A", "답 B", "가져온 문서", "판정 (A/B/비김)", "메모"]
VERDICT_COL = "판정 (A/B/비김)"
RESULTS = ("a", "b", "tie")
CRITERIA = [  # pairwise_prompts_ko.yaml(pw-1)의 판단 기준과 같은 뜻
    "사실 정확성: 참고 답·문서와 어긋나는 값(문서번호·날짜·회사·처리결과 등)이 있는 답은 크게 나쁘다.",
    "근거: 문서에 없는 내용을 지어낸 답은 나쁘다. 참고 답이 '답할 수 없음'이면 '자료에 없다'고 말한 답이 낫다. "
    "문서에 답이 있는데 '없다'고 피한 답은 맞는 값을 준 답보다 나쁘다.",
    "완전성: 질문이 요구한 값·문서를 빠짐없이 바로 답했는가.",
    "근거 표시: [철/문서] 표시가 그 내용을 담은 문서를 가리키는가.",
    "길이·말투, 그리고 어느 답이 왼쪽(A)인지는 보지 않는다. 차이가 없거나(둘 다 맞음, 둘 다 같은 정도로 틀림) 고를 수 없으면 비김.",
]


def candidates(pw_rows):
    """쌍별 결과 rows → 사람에게 보일 수 있는 쌍 [{id, result}] (같은 답·오류 뺌)"""
    return [{"id": r["id"], "result": r["result"]} for r in pw_rows
            if not r.get("identical") and r.get("result") in RESULTS]


def pick(pw_rows, n, seed):
    """심판 결과별로 섞어 돌아가며 n쌍 → [id] (고른 순서 = 표 순서). 모자라면 있는 만큼"""
    rng = random.Random(seed)
    groups = {k: [c["id"] for c in candidates(pw_rows) if c["result"] == k] for k in RESULTS}
    for k in RESULTS:
        rng.shuffle(groups[k])
    out = []
    while len(out) < n and any(groups.values()):
        for k in RESULTS:
            if groups[k] and len(out) < n:
                out.append(groups[k].pop())
    rng.shuffle(out)  # 표에서 심판 결과 순서가 드러나지 않게
    return out


def positions(ids, seed):
    """{id: 답 A에 놓을 쪽 'a'·'b'} — a가 A인 쌍이 절반(홀수면 하나 더)"""
    rng = random.Random(f"{seed}:pos")
    sides = ["a"] * ((len(ids) + 1) // 2) + ["b"] * (len(ids) // 2)
    rng.shuffle(sides)
    return dict(zip(ids, sides))


def load_answers(pw, answers_dir):
    """쌍별 결과의 두 답변 파일 → (a, b). 질의 세트 해시가 쌍별 결과와 같아야 한다"""
    out = []
    for side in ("a", "b"):
        p = Path(answers_dir) / pw[side]["file"]
        ans = json.loads(p.read_text(encoding="utf-8"))
        if ans.get("queries_sha256") != pw.get("queries_sha256"):
            raise SystemExit(f"{p.name}의 질의 세트가 쌍별 결과와 다르다")
        out.append(ans)
    return out


def documents(ra, rb, pages=None):
    """심판이 본 것과 같은 문서 목록 (pairwise.documents_of와 같은 순서: a 순서, 그다음 b에만 있는 것).
    pages = {문서 키: [쪽 이미지 URI]} — 있으면 채점 페이지 옆에 원본 쪽을 띄운다"""
    seen, out = set(), []
    for c in [*ra["retrieved"], *rb["retrieved"]]:
        if c["key"] not in seen:
            seen.add(c["key"])
            out.append({"key": c["key"], "text": c["context"], "pages": (pages or {}).get(c["key"], [])})
    return out


def page_map(work, bundles):
    """합성 철 쪽 이미지: <work>/platform/*.json(문서→쪽 번호) + <bundles>/<철>/<쪽>.jpg → {문서 키: [file URI]}"""
    work, bundles = Path(work), Path(bundles)
    out = {}
    for f in sorted((work / "platform").glob("*.json")):
        for d in json.loads(f.read_text(encoding="utf-8")):
            imgs = [bundles / d["vol"] / f"{p}.jpg" for p in d["pages"]]
            out[f"{d['vol']}/{d['doc_id']}"] = [p.resolve().as_uri() for p in imgs if p.exists()]
    return out


def build(pw, ans_a, ans_b, n=20, seed=22, pages=None):
    """→ (표 줄 [dict], 열쇠 dict). 표 줄에는 심판 판정·모델 이름이 없다"""
    if not pw.get("complete"):
        raise SystemExit("쌍별 심판이 끝까지 돌지 않았다")
    ids = pick(pw["rows"], n, seed)
    if not ids:
        raise SystemExit("사람에게 보일 쌍이 없다 (모두 같은 답이거나 오류)")
    pos = positions(ids, seed)
    ra, rb = {r["id"]: r for r in ans_a["rows"]}, {r["id"]: r for r in ans_b["rows"]}
    rows = []
    for i, rid in enumerate(ids, 1):
        a, b = ra[rid], rb[rid]
        first, second = (a, b) if pos[rid] == "a" else (b, a)
        rows.append({"id": rid, "순번": i, "답 있음": "예" if a["answerable"] else "아니오", "질문": a["question"],
                     "참고 답": a.get("expected_answer") or "", "답 A": first["answer"], "답 B": second["answer"],
                     "docs": documents(a, b, pages), VERDICT_COL: "", "메모": ""})
    judge = {r["id"]: r["result"] for r in pw["rows"]}
    key = {"pairwise_label": pw["label"], "issue": "https://github.com/reddol18/docflow/issues/22",
           "a": pw["a"], "b": pw["b"], "queries_sha256": pw.get("queries_sha256"), "seed": seed, "n": len(ids),
           "pool": len(candidates(pw["rows"])),
           "judge_mix": {k: sum(1 for i in ids if judge[i] == k) for k in RESULTS},
           "position_a_is": pos}
    return rows, key


def csv_row(r):
    docs = "\n\n".join(d["text"] for d in r["docs"])
    out = {c: r.get(c, "") for c in COLUMNS}
    out["가져온 문서"] = docs
    return {k: (v if k == VERDICT_COL else csv_safe(v)) for k, v in out.items()}


def write_csv(path, rows):
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(csv_row(r) for r in rows)


HTML = """<!doctype html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>쌍별 답변 비교</title>
<style>
:root { --bg:#f6f6f4; --fg:#1c1c1a; --muted:#6a6a65; --card:#fff; --line:#dcdcd8; --on:#1f7a3a; --tie:#6a6a65; --acc:#2a5bd7; }
@media (prefers-color-scheme: dark) { :root { --bg:#1a1a19; --fg:#ececea; --muted:#a2a29d; --card:#242423;
  --line:#3a3a38; --on:#5cc27a; --tie:#a2a29d; --acc:#7ea2ff; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--fg); font:16px/1.65 system-ui, "Malgun Gothic", sans-serif; }
.wrap { max-width:1800px; margin:0 auto; padding:16px; }
.grid { display:grid; grid-template-columns:minmax(0, 760px) minmax(0, 1fr); gap:16px; align-items:start; }
.grid.noimg { grid-template-columns:1fr; }
.grid.noimg .viewer { display:none; }
@media (max-width: 1000px) { .grid { grid-template-columns:1fr; } }
.viewer { position:sticky; top:12px; background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:10px; height:calc(100vh - 24px); display:flex; flex-direction:column; }
.viewer .vt { font-size:14px; color:var(--muted); margin-bottom:6px; }
.viewer .vbox { flex:1; overflow:auto; background:#fff; border-radius:6px; }
.viewer img { width:100%; display:block; }
a.pg { margin-right:8px; font-size:14px; color:var(--acc); }
a.pg.sel { font-weight:700; text-decoration:none; color:var(--fg); }
.top { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-bottom:12px; }
.bar { flex:1; min-width:160px; height:8px; background:var(--line); border-radius:4px; overflow:hidden; }
.bar i { display:block; height:100%; background:var(--on); }
.count { color:var(--muted); font-size:14px; white-space:nowrap; }
button { font:inherit; cursor:pointer; border:1px solid var(--line); background:var(--card); color:var(--fg);
  border-radius:8px; padding:8px 14px; }
button:disabled { opacity:.4; cursor:default; }
.save { background:var(--acc); border-color:var(--acc); color:#fff; }
.block { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px 16px; margin-bottom:10px; }
.label { font-size:13px; color:var(--muted); margin-bottom:2px; }
.q { font-size:18px; font-weight:600; }
.pre { white-space:pre-wrap; word-break:break-word; }
.pair { display:grid; grid-template-columns:1fr 1fr; gap:10px; }
@media (max-width: 800px) { .pair { grid-template-columns:1fr; } }
.ans h3 { margin:0 0 6px; font-size:16px; }
.opts { display:flex; gap:8px; flex-wrap:wrap; }
.opts button { flex:1; min-width:110px; padding:12px; font-weight:600; }
.opts button.on { background:var(--on); border-color:var(--on); color:#fff; }
.opts button.on.tie { background:var(--tie); border-color:var(--tie); }
textarea { width:100%; font:inherit; background:var(--card); color:var(--fg); border:1px solid var(--line);
  border-radius:8px; padding:8px; margin-top:8px; }
details { margin-top:6px; }
summary { cursor:pointer; color:var(--muted); font-size:14px; }
.ret { border-top:1px solid var(--line); padding:8px 0; font-size:14px; }
.nav { display:flex; gap:8px; margin-top:14px; }
.nav button { flex:1; padding:12px; }
ol.crit { margin:4px 0 0; padding-left:20px; font-size:14px; }
</style>
</head>
<body>
<div class="wrap">
  <div class="top">
    <div class="bar"><i id="bar"></i></div>
    <span class="count" id="count"></span>
    <button class="save" id="save">CSV 저장</button>
  </div>
  <div class="grid" id="grid">
    <div>
      <details class="block"><summary>판단 기준 (심판 지시문과 같다)</summary><ol class="crit" id="crit"></ol></details>
      <div id="card"></div>
      <div class="nav">
        <button id="prev">← 이전</button>
        <button id="next">다음 →</button>
      </div>
    </div>
    <div class="viewer"><div class="vt" id="vt"></div><div class="vbox"><img id="vimg" alt="원본 쪽 이미지"></div></div>
  </div>
</div>
<script>
const DATA = __DATA__;
const KEY = "pw22v1:" + DATA.label + ":" + DATA.sheet;
let ans = {};
try { ans = JSON.parse(localStorage.getItem(KEY) || "{}"); } catch (e) { ans = {}; }
const persist = () => { try { localStorage.setItem(KEY, JSON.stringify(ans)); } catch (e) {} };
let cur = 0;
let view = null;  // {row, u}: 옆에 띄운 쪽 — 같은 쌍에서 고른 쪽은 다시 그려도 유지
const hasImg = DATA.rows.some(r => r.docs.some(d => (d.pages || []).length));
if (!hasImg) document.getElementById("grid").classList.add("noimg");
function showPage(key, i, u) {
  document.getElementById("vimg").src = u;
  document.getElementById("vt").textContent = `${key} · 쪽 ${i + 1}`;
}
function esc(s) { return String(s).replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","\\"":"&quot;"}[c])); }
const isDone = r => !!(ans[r.id] || {}).v;
document.getElementById("crit").innerHTML = DATA.criteria.map(c => `<li>${esc(c)}</li>`).join("");

function render() {
  const total = DATA.rows.length, done = DATA.rows.filter(isDone).length;
  document.getElementById("bar").style.width = (100 * done / total) + "%";
  document.getElementById("count").textContent = `${cur + 1}번째 / ${total} · 끝낸 것 ${done}`;
  const r = DATA.rows[cur], a = ans[r.id] || {};
  const kind = r.answerable ? "자료에 답이 있는 질문" : "자료에 답이 없는 질문";
  let h = `<div class="block"><div class="label">질문 ${r.no} · ${kind}</div><div class="q">${esc(r.question)}</div>`;
  h += `<div class="label" style="margin-top:8px">정답(참고)</div><div class="pre">${esc(r.reference)}</div></div>`;
  h += `<div class="pair"><div class="block ans"><h3>답 A</h3><div class="pre">${esc(r.A)}</div></div>` +
       `<div class="block ans"><h3>답 B</h3><div class="pre">${esc(r.B)}</div></div></div>`;
  h += `<div class="block"><div class="label">어느 답이 더 나은가요?</div><div class="opts">` +
    [["A", "답 A"], ["B", "답 B"], ["비김", "비김"]].map(([v, lab]) =>
      `<button class="${v === "비김" ? "tie" : ""}${a.v === v ? " on" : ""}" data-v="${v}">${lab}</button>`).join("") +
    `</div><textarea id="memo" rows="2" placeholder="메모 (선택)">${esc(a.memo || "")}</textarea></div>`;
  if (!view || view.row !== r.id) {
    const d0 = r.docs.find(d => (d.pages || []).length);
    view = d0 ? { row: r.id, u: d0.pages[0] } : { row: r.id, u: "" };
    if (d0) showPage(d0.key, 0, d0.pages[0]);
    else {
      document.getElementById("vimg").removeAttribute("src");
      document.getElementById("vt").textContent = "원본 쪽 없음";
    }
  }
  const links = d => (d.pages || []).map((u, i) =>
    `<a href="#" class="pg${view.u === u ? " sel" : ""}" data-u="${esc(u)}" data-key="${esc(d.key)}" data-i="${i}">` +
    `쪽 ${i + 1}</a>`).join("");
  h += `<details class="block" open><summary>모델이 받은 문서 ${r.docs.length}건 (쪽을 누르면 옆에 원본)</summary>` +
    r.docs.map((d, i) => `<div class="ret"><b>${i + 1}.</b> ${esc(d.key)} ${links(d)}` +
      `<details><summary>OCR 글</summary><div class="pre">${esc(d.text)}</div></details></div>`).join("") + `</details>`;
  const card = document.getElementById("card");
  card.innerHTML = h;
  card.querySelectorAll("a.pg").forEach(el => el.onclick = e => {
    e.preventDefault();
    view = { row: r.id, u: el.dataset.u };
    showPage(el.dataset.key, +el.dataset.i, el.dataset.u);
    card.querySelectorAll("a.pg").forEach(x => x.classList.toggle("sel", x === el));
  });
  card.querySelectorAll(".opts button").forEach(b => b.onclick = () => {
    ans[r.id] = ans[r.id] || {};
    ans[r.id].v = ans[r.id].v === b.dataset.v ? "" : b.dataset.v;
    persist(); render();
    // 0.25초 안에 다른 쪽으로 옮겼으면 넘기지 않는다 (카드를 건너뛰지 않게)
    const at = cur;
    if (isDone(r) && cur < DATA.rows.length - 1) setTimeout(() => { if (cur !== at) return; cur++; render(); window.scrollTo(0, 0); }, 250);
  });
  document.getElementById("memo").oninput = e => { ans[r.id] = ans[r.id] || {}; ans[r.id].memo = e.target.value; persist(); };
  document.getElementById("prev").disabled = cur === 0;
  document.getElementById("next").disabled = cur === DATA.rows.length - 1;
}
document.getElementById("prev").onclick = () => { cur--; render(); window.scrollTo(0, 0); };
document.getElementById("next").onclick = () => { cur++; render(); window.scrollTo(0, 0); };

// CSV: pairwise_sheet.py가 쓰는 칸과 같다. 수식으로 읽힐 글 앞에는 ' (CSV 수식 인젝션)
const safe = s => { s = s == null ? "" : String(s); return /^[=+\\-@\\t\\r]/.test(s) ? "'" + s : s; };
const cell = s => { s = s == null ? "" : String(s); return /[",\\r\\n]/.test(s) ? '"' + s.replace(/"/g, '""') + '"' : s; };
document.getElementById("save").onclick = () => {
  const left = DATA.rows.filter(r => !isDone(r)).length;
  if (left && !confirm(`아직 안 끝낸 쌍이 ${left}개 있습니다. 그래도 저장할까요?`)) return;
  const lines = [DATA.columns.map(cell).join(",")];
  for (const r of DATA.rows) {
    const a = ans[r.id] || {};
    const verdict = ["A", "B", "비김"].includes(a.v) ? a.v : "";  // localStorage 값은 허용 목록만
    const v = { "id": safe(r.id), "순번": r.no, "답 있음": r.answerable ? "예" : "아니오", "질문": safe(r.question),
      "참고 답": safe(r.reference), "답 A": safe(r.A), "답 B": safe(r.B), "가져온 문서": "",
      [DATA.verdict_col]: verdict, "메모": safe(a.memo || "") };
    lines.push(DATA.columns.map(c => cell(v[c])).join(","));
  }
  const blob = new Blob(["\\ufeff" + lines.join("\\r\\n") + "\\r\\n"], { type: "text/csv;charset=utf-8" });
  const el = document.createElement("a"); el.href = URL.createObjectURL(blob); el.download = DATA.sheet + ".csv"; el.click();
};
document.addEventListener("keydown", e => {
  if (e.target.tagName === "TEXTAREA") return;
  if (e.key === "ArrowRight" && cur < DATA.rows.length - 1) { cur++; render(); }
  if (e.key === "ArrowLeft" && cur > 0) { cur--; render(); }
});
const firstTodo = DATA.rows.findIndex(r => !isDone(r)); if (firstTodo > 0) cur = firstTodo;
render();
</script>
</body>
</html>
"""


def html(rows, label, sheet):
    """혼자 도는 채점 페이지 — 표 줄만 넣는다 (열쇠·심판 판정·모델 이름 없음)"""
    data = {"label": label, "sheet": sheet, "columns": COLUMNS, "verdict_col": VERDICT_COL, "criteria": CRITERIA,
            "rows": [{"id": r["id"], "no": r["순번"], "answerable": r["답 있음"] == "예", "question": r["질문"],
                      "reference": r["참고 답"], "A": r["답 A"], "B": r["답 B"], "docs": r["docs"]} for r in rows]}
    blob = json.dumps(data, ensure_ascii=False).replace("<", "\\u003c")  # 글 속 '</script>'가 페이지를 깨지 않게
    return HTML.replace("__DATA__", blob)


def paths(pw_path, label, out_dir=None):
    d = Path(out_dir) if out_dir else Path(pw_path).resolve().parent.parent / "human"
    sheet = f"{label}_pw_human"
    p = {"csv": d / f"{sheet}.csv", "html": d / f"{label}_pw_score.html", "key": d / "_key" / f"{label}_pw_key.json"}
    for v in p.values():
        if v.resolve().is_relative_to(APP_ROOT):
            raise SystemExit("채점표·열쇠는 저장소 밖에 쓴다")
        if v.exists():
            raise SystemExit(f"이미 있다 — 채운 표·열쇠를 덮지 않는다: {v}")
    return p, sheet


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--pairwise", required=True, help="pairwise.py 결과 JSON")
    ap.add_argument("--answers-dir", default="", help="두 답변 파일이 있는 폴더 (기본: 쌍별 결과 폴더의 위)")
    ap.add_argument("--n", type=int, default=20)
    ap.add_argument("--seed", type=int, default=22)
    ap.add_argument("--out-dir", default="", help="기본: <답변 폴더>/human")
    ap.add_argument("--work", default="", help="합성 work 폴더 (platform/*.json). 기본: <답변 폴더>/..")
    ap.add_argument("--bundles", default="", help="합성 철 쪽 이미지 폴더 (<철>/<쪽>.jpg). 기본: <work>/../bundles")
    a = ap.parse_args(argv)
    pw = json.loads(Path(a.pairwise).read_text(encoding="utf-8"))
    answers_dir = Path(a.answers_dir or Path(a.pairwise).resolve().parent.parent)
    ans_a, ans_b = load_answers(pw, answers_dir)
    work = Path(a.work) if a.work else answers_dir.resolve().parent
    bundles = Path(a.bundles) if a.bundles else work.parent / "bundles"
    pages = page_map(work, bundles) if (work / "platform").is_dir() and bundles.is_dir() else {}
    rows, key = build(pw, ans_a, ans_b, a.n, a.seed, pages)
    shown = sum(1 for r in rows for d in r["docs"] if d["pages"])
    print(f"원본 쪽 이미지: 문서 {shown}/{sum(len(r['docs']) for r in rows)}건" if pages else "원본 쪽 이미지: 없음 (--work·--bundles)")
    p, sheet = paths(a.pairwise, pw["label"], a.out_dir)
    p["key"].parent.mkdir(parents=True, exist_ok=True)
    write_csv(p["csv"], rows)
    p["html"].write_text(html(rows, pw["label"], sheet), encoding="utf-8")
    key["sheet_sha256"] = hashlib.sha256(p["csv"].read_bytes()).hexdigest()
    p["key"].write_text(json.dumps(key, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"쌍 {len(rows)}개 (후보 {key['pool']}개, seed {a.seed})")
    print(f"채점 페이지: {p['html']}")
    print(f"빈 채점표 CSV: {p['csv']}")
    print(f"자리 열쇠 (채점 끝까지 열지 않는다): {p['key']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
