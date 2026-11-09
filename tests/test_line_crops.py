"""수신·제목 줄 자르기 (https://github.com/reddol18/docflow/issues/35). 시험 값은 모두 지어낸 것이다 (가상 기관·사람)."""
import json
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
import pytest
from fastapi.testclient import TestClient

from app import auth, backfill_crops, fields, layout, line_crops, store

W, H = 2464, 3504


def page(rows, name="p1"):
    """rows = [(y, [(x, 글자, 너비), …]), …] → OCR 결과 모양 {page, lines, items}"""
    items, lines = [], []
    for n, (y, words) in enumerate(rows):
        for x, t, w in words:
            items.append({"text": t, "score": 0.9, "box": [x, y, x + w, y + 50], "line": n})
        lines.append(" ".join(t for _, t, _ in sorted(words)))
    items.append({"text": ".", "score": 0.9, "box": [W - 10, H - 10, W, H], "line": len(rows)})  # 쪽 크기
    lines.append(".")
    return {"page": name, "lines": lines, "items": items}


HEAD = [
    (500, [(200, "문서번호", 220), (450, "가나 12345-678", 400), (1100, "시", 60), (1300, "장", 60)]),
    (600, [(200, "시행일자", 220), (450, "1999. 3. 4.", 300)]),
    (700, [(200, "경유", 160)]),
]


def doc(recipient_rows, title_rows, tail=()):
    body = [(1500, [(200, "1. 가상 본문 첫 줄입니다 아무 내용 없음", 2000)]),
            (1600, [(200, "둘째 줄 본문", 1800)])]
    return page(HEAD + recipient_rows + title_rows + body + list(tail))


def inside(box, x, y):
    return box[0] <= x <= box[2] and box[1] <= y <= box[3]


def test_recipient_with_continuation_and_ref_office():
    d = doc([(800, [(200, "수신", 160), (450, "가상시 나라동 1-2", 500), (1100, "부시장", 160)]),
             (880, [(450, "가나다상사 대표", 400), (1100, "국", 60)]),
             (960, [(200, "참조", 160), (450, "가상과장", 250), (1100, "과", 60)])],
            [(1200, [(200, "제목", 160), (450, "가상 허가 통보", 500)])])
    box, src, why = layout.line_box(d, "recipient")
    assert src == "head" and why == ""
    assert inside(box, 700, 825) and inside(box, 650, 905)  # 첫 줄·이어지는 줄
    assert inside(box, 575, 985)  # 참조 부서장 (fix_recipient가 수신 값에 붙이는 줄)
    assert box[2] < 1100  # 결재란(부시장·국)은 넣지 않는다
    assert box[3] < 1200  # 제목 줄은 넣지 않는다


def test_title_single_line_stops_before_body():
    d = doc([(800, [(200, "수신", 160), (450, "가상상사 대표", 400)])],
            [(1200, [(200, "제목", 160), (450, "가상 허가 통보", 500)]),
             (1290, [(450, "가상 본문처럼 보이는 줄", 800)])])
    box, src, _ = layout.line_box(d, "title")
    assert src == "head" and inside(box, 700, 1225)
    assert box[1] > 800 and box[3] < 1290  # 첫 줄이 짧으면 다음 줄로 넘어가지 않는다 (본문)


def test_title_wraps_to_next_line_when_first_line_is_full():
    d = doc([(800, [(200, "수신", 160), (450, "가상상사 대표", 400)])],
            [(1200, [(200, "제 목:", 200), (450, "아주 긴 가상 제목의 첫 줄이 쪽 오른쪽 끝까지", 1800)]),
             (1270, [(450, "이어지는 둘째 줄", 600)])])
    box, _, _ = layout.line_box(d, "title")
    assert inside(box, 1300, 1225) and inside(box, 700, 1295)
    assert box[3] < 1500  # 본문 '1.' 줄은 넣지 않는다


def test_fragmented_label_and_merged_rows():
    """'수'를 못 읽은 '신' 하나, OCR이 두 줄을 한 줄 번호로 묶은 경우"""
    d = doc([(800, [(260, "신", 60), (450, "가상구청장", 300)])],
            [(1200, [(200, "제목", 160), (450, "가상 회신", 300)])])
    box, src, _ = layout.line_box(d, "recipient")
    assert src == "head" and inside(box, 600, 825)
    merged = page(HEAD + [(800, [(200, "수", 60), (300, "신:가상서장", 300)])])
    # '참 조:가상과장'을 같은 줄 번호(3)로 붙인다
    merged["items"] += [{"text": "참", "score": 0.9, "box": [205, 900, 265, 950], "line": 3},
                        {"text": "조:가상과장", "score": 0.9, "box": [305, 900, 605, 950], "line": 3}]
    box, src, _ = layout.line_box(merged, "recipient")
    assert src == "head" and inside(box, 450, 825) and inside(box, 455, 925)


def test_list_ref_uses_bottom_list_lines():
    tail = [(3000, [(200, "수신처 : 가상가과장, 가상나과장,", 1200)]),
            (3080, [(200, "가상다과장, 가상라과장", 900)]),
            (3200, [(1100, "가 상 시 장", 400)])]
    d = doc([(800, [(200, "수신", 160), (450, "수신처 참조", 300)])],
            [(1200, [(200, "제목", 160), (450, "가상 회의 알림", 400)])], tail)
    assert layout.bottom_list(d["lines"]).startswith("가상가과장")
    box, src, _ = layout.line_box(d, "recipient")
    assert src == "list"
    assert inside(box, 800, 3025) and inside(box, 650, 3105)  # 목록 머리 줄 + 이어지는 줄
    assert box[3] < 3200 and box[1] > 1500  # 머리부·발신 명의는 넣지 않는다


def test_list_ref_without_list_falls_back_to_head():
    d = doc([(800, [(200, "수신", 160), (450, "수신처 참조", 300)])], [(1200, [(200, "제목", 160), (450, "가상", 200)])])
    box, src, _ = layout.line_box(d, "recipient")
    assert src == "head" and inside(box, 600, 825)


def test_not_found():
    d = page(HEAD + [(1500, [(200, "가상 본문만 있는 쪽", 1500)])])
    assert layout.line_box(d, "recipient") == (None, "", "항목명 못 찾음")
    assert layout.line_box(d, "title") == (None, "", "항목명 못 찾음")
    assert layout.line_box({"page": "x", "lines": [], "items": []}, "title")[0] is None
    with pytest.raises(ValueError):
        layout.line_box(d, "docno")


# ---------- 스키마 ----------

def _schema(**extra):
    return {"name": "t", "fields": [{"name": "who", "label": "받는이", "level": "document", "method": "manual",
                                     "area": "content", **extra}]}


def test_schema_crop_key():
    assert fields.line_crops(fields.load(fields.DIR / "gimhae.yaml")) == {"recipient": "recipient", "title": "title"}
    assert fields.line_crops(fields.parse(_schema(crop="recipient"))) == {"who": "recipient"}
    assert fields.line_crops(fields.parse(_schema())) == {}
    with pytest.raises(fields.SchemaError):
        fields.parse(_schema(crop="body"))
    with pytest.raises(fields.SchemaError):
        fields.parse({"name": "t", "fields": [{"name": "who", "label": "x", "level": "document", "method": "hidden",
                                               "area": "content", "crop": "title"}]})
    two = _schema(crop="title")
    two["fields"].append({**two["fields"][0], "name": "who2"})
    with pytest.raises(fields.SchemaError):
        fields.parse(two)


# ---------- 저장 ----------

def _put(tmp_path, monkeypatch, d, vol="v1", name="p1"):
    monkeypatch.setattr(store, "DATA", tmp_path)
    store.ocr_path(vol, name).parent.mkdir(parents=True, exist_ok=True)
    store.ocr_path(vol, name).write_text(json.dumps(d, ensure_ascii=False), encoding="utf-8")
    store.page_path(vol, name).parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", np.full((H, W, 3), 255, np.uint8))
    store.page_path(vol, name).write_bytes(buf.tobytes())


GOOD = doc([(800, [(200, "수신", 160), (450, "가상상사 대표", 400)])], [(1200, [(200, "제목", 160), (450, "가상 통보", 300)])])


def test_save_writes_png_and_removes_stale(tmp_path, monkeypatch):
    _put(tmp_path, monkeypatch, GOOD)
    assert line_crops.save("v1", "p1", ["recipient", "title"]) == {"recipient": "saved", "title": "saved"}
    img = cv2.imread(str(store.crop_path("v1", "p1", "title")))
    assert img is not None and img.shape[0] < 200
    assert line_crops.save("v1", "p1", ["title"], force=False) == {"title": "exists"}
    _put(tmp_path, monkeypatch, page(HEAD))  # 다시 OCR한 쪽에서 못 찾으면 옛 이미지를 지운다
    assert line_crops.save("v1", "p1", ["title"]) == {"title": "항목명 못 찾음"}
    assert not store.crop_path("v1", "p1", "title").exists()


def test_backfill_counts_and_idempotent(tmp_path, monkeypatch):
    _put(tmp_path, monkeypatch, GOOD, name="v1-0001")
    _put(tmp_path, monkeypatch, page([(500, [(200, "가상 둘째 쪽", 800)])]), name="v1-0002")  # 문서 시작 쪽 아님
    lines = []
    c = backfill_crops.run(["v1"], ["recipient", "title"], out=lines.append)
    assert c["docs"] == 1 and c["saved"] == 2 and lines == ["v1: 문서 1건"]
    assert not store.crop_path("v1", "v1-0002", "title").exists()
    c = backfill_crops.run(["v1"], ["recipient", "title"], out=lines.append)
    assert c["saved"] == 0 and c["exists"] == 2  # 다시 돌려도 같은 결과
    c = backfill_crops.run(["v1"], ["title"], dry=True, force=True, out=lines.append)
    assert c["would_crop"] == 1
    assert backfill_crops.vols() == ["v1"]


def test_backfill_imports_no_llm():
    """보충 스크립트는 LLM·임베딩 모듈을 불러오지도 않는다 (GPT 호출 0)"""
    root = Path(__file__).resolve().parents[1]
    code = "import sys, app.backfill_crops; print(sorted(m for m in ('app.llm','openai','app.graph') if m in sys.modules))"
    out = subprocess.run([sys.executable, "-c", code], cwd=root, capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "[]"


# ---------- /crops 경로 ----------

@pytest.fixture()
def client(monkeypatch, tmp_path):
    from app import main
    monkeypatch.setattr(auth, "MODES", frozenset({"local"}))
    monkeypatch.setattr(auth, "session_user", lambda t: auth.User(1, "v", "v", "viewer"))
    monkeypatch.setattr(main.store, "DATA", tmp_path)
    (tmp_path / "crops" / "vol").mkdir(parents=True)
    for n in ("d1_recipient.png", "d1_title.png", "d1_body.png"):
        (tmp_path / "crops" / "vol" / n).write_bytes(b"png")
    c = TestClient(main.app)
    return lambda path: c.get(path, headers={"authorization": "Bearer v"})


def test_crops_route_allows_recipient_title_only(client):
    assert client("/crops/vol/d1_recipient.png").status_code == 200
    assert client("/crops/vol/d1_title.png").content == b"png"
    assert client("/crops/vol/d2_recipient.png").status_code == 404
    assert client("/crops/vol/d2_title.png").status_code == 404
    for bad in ("d1_body.png", "d1_title.jpg", "d1_title.png.tmp", "d1_recipients.png", "..%2Fd1_title.png",
                "..%5Cd1_title.png", "%2E%2E%2Fvol%2Fd1_title.png"):
        assert client(f"/crops/vol/{bad}").status_code in (400, 404), bad
        assert client(f"/crops/vol/{bad}").status_code != 200
    for bad in ("d1_body.png", "d1_title.jpg", "d1_title.png.tmp", "..%5Cd1_title.png"):
        assert client(f"/crops/vol/{bad}").status_code == 400, bad
    assert client("/crops/%2E%2E/d1_title.png").status_code in (400, 404)


def test_schema_and_review_page_show_line_crops(monkeypatch, tmp_path, client):
    from types import SimpleNamespace

    from app import main
    by = {f["name"]: f for f in client("/schema").json()["fields"]}
    assert by["recipient"]["crop"] == "recipient" and by["title"]["crop"] == "title" and by["docno"]["crop"] is None
    d = {"doc_id": "d1", "pages": ["d1"], "flags": {"title": ["x"]}, "hw": {},
         "fields": {"docno": "", "date": "", "recipient": "가상상사", "title": "가상"}}
    snap = SimpleNamespace(interrupts=[SimpleNamespace(value={"flagged": [d, {**d, "doc_id": "d9"}]})])
    monkeypatch.setattr(main, "G", SimpleNamespace(get_state=lambda cfg: snap))
    body = client("/review/vol").text
    assert "/crops/vol/d1_recipient.png" in body and "/crops/vol/d1_title.png" in body
    assert "d9_recipient.png" not in body  # 못 찾은 문서는 이미지 자리를 그리지 않는다
