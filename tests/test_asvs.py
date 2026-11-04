"""security/asvs.yaml 형식 확인: 칸이 가리키는 항목이 모두 있고, 판정 값이 정해진 것만 쓰는지."""
from pathlib import Path

import yaml

DOC = yaml.safe_load((Path(__file__).parent.parent / "security" / "asvs.yaml").read_text(encoding="utf-8"))
STATUS = {"충족", "일부", "미충족", "해당없음", "미판정"}


def test_items_unique_and_valid():
    ids = [i["id"] for i in DOC["items"]]
    assert len(ids) == len(set(ids))
    for i in DOC["items"]:
        assert i["status"] in STATUS, i
        assert i.get("text"), i
        if i["status"] != "미판정":
            assert i.get("evidence"), f"{i['id']}: 판정했으면 근거를 단다"


def test_boxes_point_to_items_and_match_template():
    ids = {i["id"] for i in DOC["items"]}
    for box, refs in DOC["boxes"].items():
        missing = set(refs) - ids
        assert not missing, f"{box}: {missing}"
    tpl = (Path(__file__).parent.parent / ".github" / "ISSUE_TEMPLATE" / "feature.yml").read_text(encoding="utf-8")
    labels = {ln.split("label:", 1)[1].strip() for ln in tpl.splitlines() if ln.strip().startswith("- label:")}
    assert labels == set(DOC["boxes"]), f"템플릿 칸과 다름: {labels ^ set(DOC['boxes'])}"
