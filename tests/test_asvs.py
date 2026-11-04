"""security/asvs.yaml 형식 확인: 칸과 항목이 서로 빠짐없이 가리키고, 템플릿 칸과 같고, 판정에 근거가 있는지."""
from pathlib import Path

import yaml

ROOT = Path(__file__).parent.parent
DOC = yaml.safe_load((ROOT / "security" / "asvs.yaml").read_text(encoding="utf-8"))
STATUS = {"충족", "일부", "미충족", "해당없음", "미판정"}


def test_items_unique_and_valid():
    ids = [i["id"] for i in DOC["items"]]
    assert len(ids) == len(set(ids))
    for i in DOC["items"]:
        assert i["status"] in STATUS, i
        assert i.get("text"), i
        if i["status"] != "미판정":
            assert i.get("evidence"), f"{i['id']}: 판정했으면 근거를 단다"


def test_boxes_and_items_cover_each_other():
    ids = {i["id"] for i in DOC["items"]}
    used = set()
    for box, refs in DOC["boxes"].items():
        missing = set(refs) - ids
        assert not missing, f"{box}: {missing}"
        used |= set(refs)
    assert ids == used, f"어느 칸도 가리키지 않는 항목 (리뷰에서 안 보게 된다): {ids - used}"


def test_boxes_match_template():
    tpl = yaml.safe_load((ROOT / ".github" / "ISSUE_TEMPLATE" / "feature.yml").read_text(encoding="utf-8"))
    sec = [b for b in tpl["body"] if b.get("type") == "checkboxes" and "보안" in b["attributes"]["label"]]
    assert len(sec) == 1, "템플릿에 보안 영향 칸이 하나여야"
    labels = {o["label"] for o in sec[0]["attributes"]["options"]}
    assert labels == set(DOC["boxes"]), f"템플릿 칸과 다름: {labels ^ set(DOC['boxes'])}"
