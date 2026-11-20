"""손글씨 칸 비전 모델 비교 — 합성 철 (https://github.com/reddol18/docflow/issues/80 조건 4).

합성 철(eval/synth)의 손글씨 칸을 잘라 후보 비전 모델에 읽히고 칸마다 정답과 맞춰 센다. 생성기가 정답을 안다.
  운영 칸  serial(문서번호 칸)·date(시행일자 칸): 운영과 같은 길 — 첫 쪽 OCR에서 운영 locate(layout.value_box, 머리부 비율 넓히기,
           eval/synth/to_work.locate)로 칸을 찾고, app/graph.read처럼 쪽 이미지에서 잘라 PNG로, app/llm.read_cell과 같은
           지시문(CELL_PROMPT)·설정(temperature 0, max_tokens 40, detail high, logprobs)으로 묻는다.
           맞음: 문서번호 칸 → app/graph.merge와 같게 '-'가 있을 때의 일련번호(layout.serial_of)가 정답 일련번호와 같음,
                 시행일자 칸 → layout.date_of가 정답 날짜와 같음. 칸을 못 찾으면(box_fail) 모든 후보가 같이 틀림.
  기타 칸  recv_no·recv_date(접수란 번호·일자), sign_*(서명란 이름) — 운영은 읽지 않는 칸. 라벨 상자(annotation.cell)를
           조금 넓혀 자르고, 운영 지시문이 없어 OTHER_PROMPT 하나로 묻는다. 맞음: 공백·마침표를 뺀 글자가 같음.
'판독 불가'(gt note, 낡아서 사람도 읽기 어려운 칸)는 따로 센다 (읽을 수 있는 칸만의 정답 수도 낸다).

실행 (doc_platform 폴더, 합성 work의 평가 가상환경 — eval/promptfoo/run.ps1 -Work가 만든 것):
  $py = '..\\work_synth\\w47\\promptfoo\\.venv\\Scripts\\python.exe'
  $w = @('--work', '..\\work_synth\\w47', '--bundles', '..\\synth_bundles\\w47_cells')
  & $py -I -X utf8 eval/hw_compare.py --openai --model gpt-4.1-mini --label gpt41mini --concurrency 4 @w
  & $py -I -X utf8 eval/hw_compare.py --base-url http://127.0.0.1:11434/v1 --model qwen2.5vl:7b --label local_qwen25vl_7b @w  (이 PC)
  & $py -I -X utf8 eval/hw_compare.py --base-url http://127.0.0.1:18434/v1 --model qwen2.5vl:32b --label remote_qwen25vl_32b `
        --concurrency 4 @w                                                            (런파드 — SSH 터널)
  & $py -I -X utf8 eval/hw_compare.py --table ..\\work_synth\\w47\\hw\\gpt41mini.json ..\\work_synth\\w47\\hw\\local_qwen25vl_7b.json
  --dry-run: 부르지 않고 칸만 잘라 <work>/hw/crops/에 남긴다 (칸 수·자르기 확인).
결과: <work>/hw/<label>.json (칸별 답·정답·토큰·비용·지연)과 표. 합성 철만 받는다 (eval/compare_common.py).
"""
import argparse
import base64
import concurrent.futures as cf
import hashlib
import json
import math
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I: eval 폴더 (compare_common)
sys.path.insert(0, str(Path(__file__).resolve().parent / "synth"))  # to_work.locate

import compare_common as cc  # noqa: E402
import cv2  # noqa: E402

from app import layout, ocr  # noqa: E402

PROD_CELLS = {"docno": "serial", "date": "date"}  # 운영 판독 칸(app/graph.CELLS) → 합성 라벨 칸 이름
OTHER_CELLS = ("recv_no", "recv_date", "sign_drafter", "sign_reviewer", "sign_approver", "sign_attach")
OTHER_PROMPT = ("1990년대 공문서에서 사람이 손으로 쓴 칸 하나를 잘라낸 이미지다 (접수란의 번호·일자 또는 서명란의 사람 이름). "
                "손글씨를 보이는 그대로 한 줄로 적어라. 숫자는 아라비아 숫자로, 이름은 한글로 적는다. "
                "흘려 써서 애매해도 가장 그럴듯한 글자를 적는다. 값만 적고 설명은 쓰지 않는다.")
CELL_GENERATORS = {"synth-0.2"}  # annotation.cell을 남기는 eval/synth/bundle.py VERSION
PAD = 16 # 기타 칸: 라벨 상자를 사방으로 넓힐 픽셀 (글자 끝이 잘리지 않게)
GROUPS = [("serial", "문서번호 칸 (운영)"), ("date", "시행일자 칸 (운영)"), ("recv_no", "접수 번호"),
          ("recv_date", "접수 일자"), ("sign", "서명 이름")]


def group_of(cell):
    return "sign" if cell.startswith("sign_") else cell


def norm_other(s):
    return re.sub(r"[\s.·,]", "", s or "")


def judge(cell, truth, text):
    """칸 하나의 맞음 여부"""
    if cell == "serial":  # app/graph.merge: 문서번호 칸 판독에 '-'가 있을 때만 그 일련번호를 쓴다
        s_doc = layout.serial_of(text) if re.search(r"[-~]", text or "") else ""
        return bool(s_doc) and s_doc == truth
    if cell == "date":
        return bool(text) and layout.date_of(text) == truth
    return bool(norm_other(text)) and norm_other(text) == norm_other(truth)


def sha256(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def label_cells(label_json):
    """합성 라벨 → {칸: (정답 글자, [x0,y0,x1,y1])}. annotation.cell 단어를 순서대로 잇고 상자를 합친다"""
    out = {}
    for a in label_json["annotations"]:
        c = a.get("annotation.cell")
        if not c:
            continue
        x, y, w, h = a["annotation.bbox"]
        t, b = out.get(c, ("", None))
        b = [x, y, x + w, y + h] if b is None else [min(b[0], x), min(b[1], y), max(b[2], x + w), max(b[3], y + h)]
        out[c] = ((t + " " + a["annotation.text"]).strip(), b)
    return out


def png_crop(img, box, pad=0):
    """app/graph.read와 같은 자르기 (쪽 안으로 자름) → PNG 바이트"""
    H, W = img.shape[:2]
    x0, y0, x1, y1 = max(box[0] - pad, 0), max(box[1] - pad, 0), min(box[2] + pad, W), min(box[3] + pad, H)
    ok, buf = cv2.imencode(".png", img[y0:y1, x0:x1])
    if not ok:
        raise RuntimeError("PNG 인코딩 실패")
    return buf.tobytes()


def collect(work, bundles, vols):
    """읽을 칸 목록 [{vol, doc_id, page, cell, truth, hard, box, box_fail, png}] — 부르기 전에 모두 만든다"""
    import to_work  # noqa: PLC0415 — 운영 locate (app/graph.locate·widen과 같음)
    gt = cc.load_gt(work)
    cells = []
    for vol in vols:
        gen = json.loads((Path(bundles) / vol / "meta.json").read_text(encoding="utf-8")).get("generator")
        if gen not in CELL_GENERATORS:  # 예전 판 철은 칸 이름이 없어 기타 칸이 조용히 빠진다 → 철 단위로 멈춘다
            raise SystemExit(f"손글씨 칸 이름(annotation.cell)이 없는 생성기 판({gen}) 철: {vol} — eval/synth/bundle.py로 다시 만든다")
        n0 = len(cells)
        for d in cc.stored_docs(work, vol):
            page = d["pages"][0]
            o = cc.page_ocr(work, vol, page)
            img_p = Path(bundles) / vol / f"{page}.jpg"
            if sha256(img_p) != o.get("image_sha256"):  # OCR(상자)를 읽은 이미지와 다르면 칸 위치가 어긋난다
                raise SystemExit(f"합성 철 이미지가 work OCR과 다르다 (다른 생성 결과?): {img_p}")
            img = ocr.read_image(img_p)
            lab = label_cells(json.loads(img_p.with_suffix(".json").read_text(encoding="utf-8")))
            row = gt.get(page)
            note = (row or {}).get("note", "")
            base = {"vol": vol, "doc_id": d["doc_id"], "page": page}
            if row:  # 정답 문서 시작 쪽으로 나뉜 문서만 운영 칸을 센다 (운영 split이 잘못 나눈 문서는 정답 없음)
                _, boxes, fail = to_work.locate(o["items"])
                truths = {"serial": layout.serial_of(row["docno"]), "date": row["date"]}
                for k, cell in PROD_CELLS.items():
                    b = boxes.get(k)
                    cells.append({**base, "cell": cell, "kind": "prod", "truth": truths[cell],
                                  "hard": f"{k}:판독 불가" in note, "box": b, "box_fail": "" if b else fail.get(k, ""),
                                  "png": png_crop(img, b) if b else None})
            for p in d["pages"]:  # 기타 칸: 문서의 모든 쪽 (서명란은 이어지는 쪽에, 검토자는 붙임 쪽에 있을 수 있다)
                pl = lab if p == page else label_cells(json.loads((Path(bundles) / vol / f"{p}.json").read_text(encoding="utf-8")))
                pimg = img if p == page else None
                for cell in OTHER_CELLS:
                    if cell not in pl:
                        continue
                    if pimg is None:
                        pimg = ocr.read_image(Path(bundles) / vol / f"{p}.jpg")
                    truth, b = pl[cell]
                    cells.append({"vol": vol, "doc_id": d["doc_id"], "page": p, "cell": cell, "kind": "other", "truth": truth,
                                  "hard": False, "box": b, "box_fail": "", "png": png_crop(pimg, b, PAD)})
        if not any(c["kind"] == "other" for c in cells[n0:]):  # 철마다 기타 칸 정답이 있어야 한다
            raise SystemExit(f"손글씨 칸 정답(annotation.cell)이 없는 철: {vol} — eval/synth/bundle.py로 다시 만든다")
    return cells


def prompt_of(cell):
    from app import llm  # noqa: PLC0415 — 운영 지시문 그대로
    if cell == "serial":
        return llm.CELL_PROMPT["docno"]
    if cell == "date":
        return llm.CELL_PROMPT["date"]
    return OTHER_PROMPT


class Reader:
    def __init__(self, client, model, priced, budget):
        from app import llm  # noqa: PLC0415
        self.llm, self.client, self.model, self.priced, self.budget = llm, client, model, priced, budget
        self.logprobs = True  # 주지 않는 서버면 한 번 실패한 뒤 빼고 부른다

    def est(self, prompt):
        llm = self.llm
        return llm.estimate_usd(self.model, llm.tokens_max(prompt) + llm.IMAGE_TOKENS.get(self.model, max(llm.IMAGE_TOKENS.values())),
                                llm.CELL_MAX_TOKENS)

    def _call(self, prompt, url, logprobs):
        kw = {"logprobs": True} if logprobs else {}
        return self.client.chat.completions.create(
            model=self.model, temperature=0, max_tokens=self.llm.CELL_MAX_TOKENS, **kw,
            messages=[{"role": "user", "content": [{"type": "text", "text": prompt},
                                                   {"type": "image_url", "image_url": {"url": url, "detail": "high"}}]}])

    def _call_once_more(self, prompt, url):
        """→ (응답, 마지막 호출 시작 시각). logprobs를 넣어 불렀다가 logprobs 때문에 실패하면 (다른 스레드가 플래그를
        먼저 바꿨든 아니든) 이번 호출에서 빼고 한 번 더 — 판단은 이번 호출에 넣은 값으로 한다 (공유 플래그 아님)"""
        sent = self.logprobs
        t0 = time.perf_counter()
        try:
            return self._call(prompt, url, sent), t0
        except Exception as e:
            if not sent or "logprob" not in str(e).lower():
                raise
            self.logprobs = False  # 다음 칸부터는 처음부터 뺀다
        t0 = time.perf_counter()  # 지연은 마지막 호출만
        return self._call(prompt, url, False), t0

    def read(self, c):
        """칸 하나 → 결과 dict (png 제외)"""
        out = {k: v for k, v in c.items() if k != "png"}
        res = {"text": "", "ok": False, "conf": 0.0, "tin": 0, "tout": 0, "usd": 0.0, "ms": 0, "error": ""}
        if c["png"] is None:
            return {**out, **res, "error": "box_fail"}
        prompt = prompt_of(c["cell"])
        est = self.est(prompt) if self.priced else 0.0
        if self.priced and not self.budget.reserve(est):
            return {**out, **res, "error": "budget"}
        url = "data:image/png;base64," + base64.b64encode(c["png"]).decode()
        t0 = time.perf_counter()
        try:
            r, t0 = self._call_once_more(prompt, url)
            res["ms"] = round((time.perf_counter() - t0) * 1000)
            res["tin"] = getattr(r.usage, "prompt_tokens", 0) or 0
            res["tout"] = getattr(r.usage, "completion_tokens", 0) or 0
            if self.priced:  # 응답을 받았으면 과금됨 — 아래에서 예외가 나도 결과 줄에 비용·토큰을 남긴다
                pi, po = self.llm.price_of(self.model)
                res["usd"] = (res["tin"] * pi + res["tout"] * po) / 1e6 if r.usage else est
            ch = r.choices[0]
            lps = [t.logprob for t in (ch.logprobs.content or [])] if getattr(ch, "logprobs", None) else []
            text = (ch.message.content or "").strip()
            res.update(text=text, ok=judge(c["cell"], c["truth"], text),
                       conf=round(math.exp(sum(lps) / len(lps)), 4) if lps else 0.0)
        except Exception as e:  # noqa: BLE001 — 칸 하나 실패는 틀림으로 세고 이어 간다 (오류 종류만 남김)
            res.update(text="", ok=False, conf=0.0, error=type(e).__name__)
            if not res["ms"]:
                res["ms"] = round((time.perf_counter() - t0) * 1000)
        finally:
            if self.priced:
                self.budget.settle(est, res["usd"])
        return {**out, **res}


def summarize(rows):
    """{묶음: {n, ok, n_legible, ok_legible}} + 전체 토큰·비용·지연"""
    s = {}
    for g, _ in GROUPS:
        rs = [r for r in rows if group_of(r["cell"]) == g]
        leg = [r for r in rs if not r["hard"]]
        s[g] = {"n": len(rs), "ok": sum(r["ok"] for r in rs), "n_legible": len(leg), "ok_legible": sum(r["ok"] for r in leg)}
    called = [r for r in rows if r["error"] != "box_fail"]
    ms = [r["ms"] for r in called if not r["error"]]
    s["_"] = {"cells": len(rows), "box_fail": sum(r["error"] == "box_fail" for r in rows),
              "errors": sum(bool(r["error"]) and r["error"] != "box_fail" for r in rows),
              "budget_skipped": sum(r["error"] == "budget" for r in rows),
              "tin": sum(r["tin"] for r in rows), "tout": sum(r["tout"] for r in rows),
              "usd": round(sum(r["usd"] for r in rows), 6),
              "ms_mean": round(sum(ms) / len(ms)) if ms else 0, "ms_p95": cc.p95(ms)}
    return s


def table(results):
    """결과 파일들 → 마크다운 표 (후보 한 줄씩). 칸 값 '맞음/전체 (판독 불가 칸을 뺀 맞음/전체)'"""
    head = "| 후보 | " + " | ".join(name for _, name in GROUPS) + " | 오류 | 입력/출력 토큰 | 비용 | 칸당 지연 평균 / p95 |"
    lines = [head, "|---|" + "---|" * (len(GROUPS) + 4)]
    for res in results:
        s = res["summary"]
        cols = [f"{s[g]['ok']}/{s[g]['n']} ({s[g]['ok_legible']}/{s[g]['n_legible']})" for g, _ in GROUPS]
        u = s["_"]
        lat = f"{u['ms_mean'] / 1000:.1f}s / {u['ms_p95'] / 1000:.1f}s"
        lines.append(f"| {res['model']} ({res['where']}) | " + " | ".join(cols)
                     + f" | {u['errors']} | {u['tin']:,}/{u['tout']:,} | ${u['usd']:.4f} | {lat} |")
    return "\n".join(lines)


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    if argv and argv[0] == "--table":
        print(table([json.loads(Path(p).read_text(encoding="utf-8")) for p in argv[1:]]))
        return 0
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    cc.add_args(ap)
    ap.add_argument("--bundles", required=True, help="합성 철 폴더 (eval/synth/bundle.py 출력, annotation.cell 있는 판)")
    ap.add_argument("--concurrency", type=int, default=1)
    ap.add_argument("--limit", type=int, default=0, help="앞에서 칸 N개만 (연결 확인용)")
    ap.add_argument("--dry-run", action="store_true", help="부르지 않고 잘라낸 칸만 <work>/hw/crops/에")
    a = ap.parse_args(argv)
    label = cc.label_ok(a.label)
    vols = cc.synthetic_vols(a.work, cc.vols_arg(a.vols), bundles=a.bundles)
    out = cc.out_dir(a.work, "hw")
    cells = collect(a.work, a.bundles, vols)
    if a.limit:
        cells = cells[:a.limit]
    print(f"합성 철 {len(vols)}개, 칸 {len(cells)}개 (운영 칸 {sum(c['kind'] == 'prod' for c in cells)}, "
          f"칸 못 찾음 {sum(c['png'] is None for c in cells)})", flush=True)
    if a.dry_run:
        d = out / "crops"
        d.mkdir(exist_ok=True)
        for c in cells:
            if c["png"]:
                (d / f"{c['page']}_{c['cell']}.png").write_bytes(c["png"])
        print(f"잘라낸 칸: {d}")
        return 0
    client, priced = cc.make_client(a.openai, a.base_url)
    if priced:
        from app import llm  # noqa: PLC0415
        llm.price_of(a.model)  # 가격표에 없는 OpenAI 모델은 부르지 않는다
    budget = cc.Budget(a.max_usd)
    reader = Reader(client, a.model, priced, budget)
    t0 = time.time()
    with cf.ThreadPoolExecutor(max(1, a.concurrency)) as ex:
        rows = list(ex.map(reader.read, cells))
    wall = round(time.time() - t0, 1)
    res = {"label": label, "model": a.model, "where": "OpenAI" if a.openai else cc.check_base_url(a.base_url).split("/v1")[0],
           "vols": vols, "wall_s": wall, "concurrency": a.concurrency, "logprobs": reader.logprobs,
           "summary": summarize(rows), "cells": rows}
    p = out / f"{label}.json"
    p.write_text(json.dumps(res, ensure_ascii=False, indent=1), encoding="utf-8")
    print(table([res]))
    u = res["summary"]["_"]
    print(f"실행 시간 {wall}s, 오류 {u['errors']} (예산으로 건너뜀 {u['budget_skipped']}), 비용 ${u['usd']:.6f} (상한 ${a.max_usd})")
    print(f"결과: {p}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
