"""합성 양식 늘리기 (https://github.com/reddol18/docflow/issues/139 조건 3): agency1990(지어낸 도 사업소 1990년대 시행문)·
draft2000(2000년대 기안문, 가로 표 머리부). 같은 시드 같은 쪽, 4칸 정답이 그림 글자와 맞음, 칸 정의가 #9 설정 검증 통과,
김해 양식(기본)은 예전과 같은 난수 순서. 쪽 그리기는 Pillow만 (Augraphy 쓰는 철 만들기는 그 환경에서만)."""
import ast
import random
import re
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
SYNTH = ROOT / "eval" / "synth"
sys.path.insert(0, str(SYNTH))

import render  # noqa: E402

pytestmark = pytest.mark.skipif(not Path(render.Fonts().print).is_file(), reason="바탕 글꼴(Windows)이 없음")
NOFONTS = render.Fonts(hand=[])
NEW = ("agency1990", "draft2000")


def page(form, seed, **kw):
    c = render.new_case_form(random.Random(seed), form)
    return c, render.FIRST_PAGE[form](c, NOFONTS, random.Random(f"{seed}-p"), **kw)


@pytest.mark.parametrize("form", NEW)
def test_same_seed_same_page(form):
    c1, p1 = page(form, 7, typewriter=True)
    c2, p2 = page(form, 7, typewriter=True)
    assert c1 == c2 and c1.form == form
    assert p1.ann == p2.ann
    assert np.array_equal(np.asarray(p1.img), np.asarray(p2.img))
    assert page(form, 8)[0] != c1


@pytest.mark.parametrize("form", NEW)
@pytest.mark.parametrize("seed", range(10))
def test_field_words_match_answer(form, seed):
    """칸 값 단어(annotation.field)를 이으면 정답 — 4칸 모두 쪽에 있고 정답과 어긋나지 않는다"""
    c, p = page(form, seed)
    words = {f: [a["text"] for a in p.ann if a.get("field") == f] for f in render.FIELDS}
    gt = c.fields
    assert all(words[f] for f in render.FIELDS) and all(gt[f] for f in render.FIELDS)
    assert "".join(words["docno"]) == gt["docno"].replace(" ", "")
    assert " ".join(words["recipient"]) == gt["recipient"]
    assert " ".join(words["title"]) == gt["title"]
    yy, mm, dd = (int(x) for x in re.findall(r"\d+", " ".join(words["date"])))
    lo, hi = render.YEARS[form]
    year = yy if yy > 999 else 1900 + yy
    assert (year, mm, dd) == tuple(int(x) for x in gt["date"].split("-")) and lo <= year <= hi
    hand = [a for a in p.ann if a["ttype"] == "handwritten"]
    assert hand and all(a.get("cell") in render.HAND_CELLS for a in hand)
    cells = [a["cell"] for a in hand]
    # 손글씨 칸 하나 = hand() 한 번: 같은 칸 이름이 떨어져 두 번 나오지 않는다
    assert all(cells[i] == cells[i + 1] or cells[i + 1] not in cells[:i + 1] for i in range(len(cells) - 1)), cells
    for a in p.ann:
        x0, y0, x1, y1 = a["box"]
        assert 0 <= x0 < x1 <= render.W and 0 <= y0 < y1 <= render.H, a


def test_forms_differ_in_layout():
    """두 새 양식은 김해 꼴과 머리가 다르다: agency1990은 문서번호·시행일자가 한 줄, draft2000은 가로 표(항목명 줄 아래 값 줄),
    문서번호는 인쇄 '부서명-번호'(손글씨 아님)"""
    def box(p, f):
        bs = [a["box"] for a in p.ann if a.get("field") == f]
        return min(b[0] for b in bs), min(b[1] for b in bs), max(b[2] for b in bs), max(b[3] for b in bs)

    c = render.new_case(random.Random(1))
    g = render.first_page(c, NOFONTS, random.Random("1-p"))
    _, a = page("agency1990", 1)
    _, d = page("draft2000", 1)
    assert box(g, "date")[1] - box(g, "docno")[1] > 50  # 김해: 시행일자가 문서번호 아래 줄
    assert abs(box(a, "date")[1] - box(a, "docno")[1]) < 40 and box(a, "date")[0] > box(a, "docno")[2]  # 한 줄
    assert abs(box(d, "date")[1] - box(d, "docno")[1]) < 40
    labels = {x["text"]: x["box"] for x in d.ann if x["text"] in ("문서번호", "시행일자", "보존기간", "기안자")}
    assert len(labels) == 4 and labels["문서번호"][3] < box(d, "docno")[1]  # 항목명 줄 아래 값
    assert not any(x["ttype"] == "handwritten" for x in d.ann if x.get("field") in ("docno", "date"))
    assert re.fullmatch(r"[가-힣]+과-\d{4,5}", page("draft2000", 3)[0].fields["docno"])
    assert re.fullmatch(r"[가-힣]{2} \d{5}-\d{3,4}", page("agency1990", 3)[0].fields["docno"])


def test_names_are_invented_and_no_real_data_paths():
    """지명·기관명은 지어낸 목록에서만 — 새 양식 기관도 도(PROVINCE)·시군(CITY)은 실제 없는 이름"""
    assert set(render.PROVINCE) == {"새벌도", "한누리도"}
    assert set(render.CITY) == {"한빛시", "새솔시", "가람군", "누리시"}
    for form in NEW:
        for seed in range(20):
            c = render.new_case_form(random.Random(seed), form)
            assert c.city in render.CITY
            if form == "agency1990":
                prov, unit = c.org.split(" ")
                assert prov in render.PROVINCE and unit in {u for u, *_ in render.UNIT}
            else:
                assert c.org in render.CITY and c.dept[1] in render.DEPT2000
    for f in list(SYNTH.glob("*.py")) + [ROOT / "app" / "field_schemas" / n for n in render.SCHEMA_FILE.values()]:
        src = f.read_text(encoding="utf-8").lower().replace("\\", "/")
        for bad in ("aihub_extracted", "71299", "work/gt", "라벨링데이터", "원천데이터"):
            assert bad not in src, (f.name, bad)


def _graph_rules():
    """app/graph.py RULES (graph는 langgraph·DB를 불러와 여기서 import하지 않는다 — 글자에서 읽는다)"""
    tree = ast.parse((ROOT / "app" / "graph.py").read_text(encoding="utf-8"))
    for n in tree.body:
        if isinstance(n, ast.Assign) and any(getattr(t, "id", None) == "RULES" for t in n.targets):
            return ast.literal_eval(n.value)
    raise AssertionError("graph.RULES 없음")


@pytest.mark.parametrize("form", render.FORMS)
def test_form_field_schema_passes_config_validation(form, tmp_path):
    """양식별 칸 정의가 #9 설정 검증(app/fields.load: 기본 스키마 isad 연결·추출 방식·지시문·검색 칸)과 rule 이름 확인을 통과하고,
    사람이 고치는 칸 = 4칸 정답 열"""
    pytest.importorskip("yaml")
    sys.path.insert(0, str(ROOT))
    from app import fields

    src = fields.DIR / render.SCHEMA_FILE[form]
    s = fields.load(src)
    fields.check_rules(s, _graph_rules())
    assert fields.editable(s) == render.FIELDS
    assert fields.extract_prompt(s)
    # 폴더를 옮겨도 같은 폴더의 기본 스키마만 보고 통과 (운영 FIELD_SCHEMA로 줄 때와 같은 조건)
    shutil.copy(fields.BASE, tmp_path / "isad_g.yaml")
    shutil.copy(src, tmp_path / src.name)
    assert fields.editable(fields.load(tmp_path / src.name)) == render.FIELDS


def test_bundle_form_choice_and_gimhae_unchanged():
    """--form: 기본 gimhae는 예전 계획(난수 순서) 그대로, mix는 시드로 고르고, 새 양식 철은 기관·연도가 그 양식 것"""
    import bundle

    for seed in (1, 4701, 4710):
        r = random.Random(seed)  # 0.2 판 plan을 그대로 다시 짠다
        city, year = r.choice(render.CITY), r.randint(1991, 1998)
        old = [render.new_case(r, city=city, year=year)]
        r.random(), r.random()
        _, _, pages = bundle.plan(seed, 1)
        assert pages[0][0] == old[0] and pages[0][0].form == "gimhae"
        assert bundle.plan(seed, 3) == bundle.plan(seed, 3, "gimhae")
    assert bundle.pick_form(5) == "gimhae"
    got = {bundle.pick_form(s, "mix") for s in range(40)}
    assert got == set(render.FORMS)
    assert bundle.pick_form(9, "mix") == bundle.pick_form(9, "mix")
    with pytest.raises(ValueError):
        bundle.pick_form(1, "telegram")  # 외교 전보 꼴은 #14 범위
    for form in NEW:
        _, year, pages = bundle.plan(11, 4, form)
        lo, hi = render.YEARS[form]
        assert lo <= year <= hi and {c.form for c, *_ in pages} == {form}
        assert len({c.org for c, *_ in pages}) == 1  # 철 하나 = 기관 하나


@pytest.mark.parametrize("form", NEW)
def test_bundle_pages_and_labels(form, tmp_path, monkeypatch):
    """새 양식 철: 첫 쪽에만 4칸 정답, 라벨은 AI Hub 88 꼴, meta에 form. 낡게 하기는 가짜(Augraphy 없이)로 빨리 본다"""
    import json

    import bundle

    monkeypatch.setattr(bundle.age, "age", lambda img, ann, level, seed: (img, [a["box"] for a in ann],
                                                                          {f: 200.0 for f in render.FIELDS}, 0.0))
    d, rows, meta = bundle.make(tmp_path, seed=21, n_docs=3, level="light", fonts=NOFONTS, form=form)
    d2, rows2, _ = bundle.make(tmp_path / "b", seed=21, n_docs=3, level="light", fonts=NOFONTS, form=form)
    assert rows == rows2 and meta["form"] == form and meta["generator"] == bundle.VERSION
    for r in rows:
        assert (d / f"{r['page']}.json").read_bytes() == (d2 / f"{r['page']}.json").read_bytes()
        assert bool(r["docno"]) == (r["is_start"] == "Y")
    starts = [r for r in rows if r["is_start"] == "Y"]
    assert len(starts) == 3 and all(r[f] for r in starts for f in render.FIELDS)
    lab = json.loads((d / f"{starts[0]['page']}.json").read_text(encoding="utf-8"))
    assert lab["images"][0]["image.category"] == ("합성.기안문" if form == "draft2000" else "합성.시행문")
    assert {a["annotation.field"] for a in lab["annotations"] if "annotation.field" in a} == set(render.FIELDS)


# ---------- 코드 리뷰 1차 (https://github.com/reddol18/docflow/issues/139) ----------

GRAPH_HEAD_RX = r"([가-힣]{1,4})\s*(\d{5})"  # app/graph.py rule_values_of 문서번호 머리 (운영 규칙 — 바꾸지 않는다)


@pytest.mark.parametrize("seed", range(20))
def test_agency_docno_survives_gimhae_rule(seed):
    """agency1990은 김해 문서번호 규칙(rule: docno)을 그대로 쓴다 — 머리 '약칭 + 5자리 분류번호'가 빠지지 않아야 한다
    (4자리 분류번호면 머리가 빠져 '-318'이 된다)"""
    sys.path.insert(0, str(ROOT))
    from app import layout

    assert GRAPH_HEAD_RX in (ROOT / "app" / "graph.py").read_text(encoding="utf-8")
    assert re.search(r"\(\\d\{5\}\)\\s\*\[-~\]", (ROOT / "app" / "layout.py").read_text(encoding="utf-8"))  # prefix_of
    c = render.new_case_form(random.Random(seed), "agency1990")
    # rule_values_of와 같은 조립: 머리(약칭 + 5자리) + '-' + 일련번호 (손글씨 칸 판독·B 값 모두 그린 문서번호라고 보고)
    m = re.search(GRAPH_HEAD_RX, c.docno)
    assert m, c.docno
    assert f"{m.group(1)} {m.group(2)}-{layout.serial_of(c.docno)}" == c.docno
    assert layout.prefix_of(c.docno) == c.classno and len(c.classno) == 5
    assert not re.search(GRAPH_HEAD_RX, "수관 1420-318")  # 예전 4자리 — 머리를 못 찾는다


def _all_text(p):
    return " ".join(a["text"] for a in p.ann)


@pytest.mark.parametrize("form", render.FORMS)
@pytest.mark.parametrize("seed", range(8))
def test_sign_tail_matches_dept_fact(form, seed):
    """서명란 꼬리 '과-일련번호 ( 연도.' — eval/answer/make_queries.py 부서 사실 정규식이 읽고, 일련번호는 문서번호와 같다"""
    sys.path.insert(0, str(ROOT / "eval" / "answer"))
    import make_queries

    if form == "gimhae":
        c = render.new_case(random.Random(seed))
        p = render.first_page(c, NOFONTS, random.Random(f"{seed}-p"))
    else:
        c, p = page(form, seed)
    m = make_queries.RX["dept"].search(_all_text(p))
    assert m and m.group(1) == c.dept[1] and m.group(1).endswith("과")
    tail = re.search(r"(\S+과)-(\d+) \( (\d{4})\.", _all_text(p))
    assert tail.group(2) == c.serial == c.docno.rsplit("-", 1)[1] and int(tail.group(3)) == c.year


def test_seal_and_sender_names():
    """관인 글자는 잘리지 않는 짧은 이름(약칭 + '장인'), 군은 군수"""
    for seed in range(20):
        a = render.new_case_form(random.Random(seed), "agency1990")
        assert a.seal_text == f"{a.dept[0]}장인" and len(a.seal_text) == 4 and a.sender == f"{a.org}장"
    d = render.new_case_form(random.Random(1), "draft2000", city="가람군")
    assert d.sender == "가람군수" and d.seal_text == "가람군수인"
    d = render.new_case_form(random.Random(1), "draft2000", city="한빛시")
    assert d.sender == "한빛시장" and d.seal_text == "한빛시장인"
    p = render.Page(NOFONTS, random.Random(3))
    p.seal((400, 400), 220, "가람군수인", per_col=3)  # 다섯 글자 관인도 그린다 (예외 없음)
    assert np.asarray(p.img).min() < 255


def test_agency_header_order_matches_preamble():
    """agency1990 그림: 수신 한 줄, 그 아래 줄에 경유·참조 — 칸 정의 지시문 머리말도 같은 순서"""
    pytest.importorskip("yaml")
    import yaml

    _, p = page("agency1990", 2)
    y = {a["text"]: a["box"][1] for a in p.ann if a["text"] in ("수신", "경유", "참조", "제목")}
    assert y["수신"] < y["경유"] and abs(y["경유"] - y["참조"]) < 10 and y["참조"] < y["제목"]
    pre = yaml.safe_load((ROOT / "app" / "field_schemas" / "synth_agency1990.yaml").read_text(encoding="utf-8"))["extract"]["preamble"]
    assert "다음 줄에 수신, 그 아래 한 줄에 경유와 참조" in pre


def test_draft_title_no_double_notice():
    """draft2000 제목: 이미 '통보'·'알림'으로 끝나면 '알림'을 덧붙이지 않는다"""
    titles = {render.new_case_form(random.Random(s), "draft2000").title for s in range(300)}
    assert any(t.endswith(" 알림") for t in titles)
    assert not any(t.endswith(("통보 알림", "알림 알림")) for t in titles)


def test_vol_name_has_form():
    """철 이름: gimhae는 예전 그대로(w47 재현), 새 양식은 표시가 붙어 같은 시드 다른 양식이 겹치지 않는다"""
    import bundle

    assert bundle.vol_name(4701, 1995, 1) == bundle.vol_name(4701, 1995, 1, "gimhae") == "SYNTH4701-1995-0001"
    names = {bundle.vol_name(11, 1995, 1, f) for f in render.FORMS}
    assert len(names) == len(render.FORMS) and all(n.startswith("SYNTH0011") for n in names)


def _fake_age(monkeypatch, bundle):
    monkeypatch.setattr(bundle.age, "age", lambda img, ann, level, seed: (img, [a["box"] for a in ann],
                                                                          {f: 200.0 for f in render.FIELDS}, 0.0))


# ---------- 코드 리뷰 2차 (https://github.com/reddol18/docflow/issues/139) ----------

def test_remake_same_vol_drops_old_pages(tmp_path, monkeypatch):
    """같은 이름 철 폴더를 다시 만들면 옛 쪽 파일(.jpg·.json·check/)이 남지 않는다 — 쪽 수가 줄어도 폴더 = 새 철 그대로.
    다른 파일은 건드리지 않고, 합성 철이 아닌 폴더(meta synthetic 아님)면 지우지 않고 멈춘다"""
    import json

    import bundle

    _fake_age(monkeypatch, bundle)
    d, rows, _ = bundle.make(tmp_path, seed=21, n_docs=4, level="light", fonts=NOFONTS, form="draft2000", check=True)
    many = {p.name for p in d.rglob("*") if p.is_file()}
    (d / "메모.txt").write_text("남김", encoding="utf-8")
    d2, rows2, meta2 = bundle.make(tmp_path, seed=21, n_docs=1, level="light", fonts=NOFONTS, form="draft2000")
    assert d2 == d and len(rows2) < len(rows)
    pages = {r["page"] for r in rows2}
    left = {p.stem for p in d.glob("SYNTH*.*")} | {p.stem for p in (d / "check").glob("*.jpg")}
    assert left == pages and len(many) > len(pages) * 2 + 2  # 첫 철은 쪽이 더 많았다
    assert (d / "메모.txt").exists() and [p["page"] for p in meta2["pages"]] == [r["page"] for r in rows2]
    assert bundle.clear_old_pages(tmp_path / "없음", "SYNTH0021D-2004-0001") == 0
    other = tmp_path / "남의폴더"
    other.mkdir()
    (other / "meta.json").write_text(json.dumps({"vol": "x"}), encoding="utf-8")
    (other / "x-0001.jpg").write_bytes(b"")
    with pytest.raises(SystemExit, match="합성 철이 아닌"):
        bundle.clear_old_pages(other, "x")
    assert (other / "x-0001.jpg").exists()


def test_plan_rejects_mix_and_unknown_form():
    """plan()은 이미 푼 양식만 받는다 — mix·모르는 양식이면 KeyError가 아니라 ValueError (make는 pick_form을 먼저 부른다)"""
    import bundle

    for bad in ("mix", "telegram", None):
        with pytest.raises(ValueError, match="plan 양식"):
            bundle.plan(1, 2, bad)
    assert bundle.plan(1, 2, bundle.pick_form(1, "mix"))[2]


def test_scoring_fields_same_for_all_forms(monkeypatch):
    """eval/score.py 채점 칸 = 모든 양식 칸 정의의 고칠 수 있는 칸 (같지 않으면 김해 칸으로 조용히 채점하지 않고 멈춘다)"""
    pytest.importorskip("yaml")
    import importlib.util

    spec = importlib.util.spec_from_file_location("score_forms", ROOT / "eval" / "score.py")
    if not (ROOT.parent / "scripts" / "rescore.py").is_file():
        pytest.skip("PoC rescore.py 없음")
    score = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(score)
    assert score.FIELDS == list(render.FIELDS)
    assert set(score.form_fields()) == set(render.FORMS)
    monkeypatch.setattr(score, "form_fields", lambda: {"gimhae": ("docno", "date"), "draft2000": ("docno",)})
    with pytest.raises(SystemExit, match="채점 칸"):
        score.scoring_fields()
