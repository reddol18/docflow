"""합성 양식 목록과 양식별 칸 정의 파일 (https://github.com/reddol18/docflow/issues/139 조건 3).

그리기(render.py — Pillow·numpy)를 불러오지 않는 하네스(to_work·promptfoo·score·hw_compare)도 철의 양식(meta.json form)에 맞는
칸 정의를 고르게 여기 따로 둔다. render.py는 이 값을 그대로 다시 내보낸다 (render.FORMS·render.SCHEMA_FILE).
"""
FORMS = ("gimhae", "agency1990", "draft2000")
DEFAULT_FORM = "gimhae"  # meta.json에 form이 없는 옛 철(synth-0.1·0.2)은 김해 양식
# 양식별 칸 정의 (app/field_schemas/ 파일 이름 — https://github.com/reddol18/docflow/issues/9 설정 검증을 시험이 돌린다)
SCHEMA_FILE = {"gimhae": "gimhae.yaml", "agency1990": "synth_agency1990.yaml", "draft2000": "synth_draft2000.yaml"}
# 운영 손글씨 칸(문서번호 일련번호·시행일자)을 손으로 쓰는 양식. draft2000은 두 칸이 인쇄 글자라 손글씨 판독 비교(hw_compare)에서 뺀다
HAND_PROD_FORMS = ("gimhae", "agency1990")
# 철 이름 표시 (bundle.py): gimhae는 예전 이름 그대로(SYNTH<seed>-<연도>-<번호>), 새 양식은 시드 뒤에 붙인다
# — 같은 시드 다른 양식이 겹치지 않게
VOL_TAG = {"gimhae": "", "agency1990": "A", "draft2000": "D"}


def form_of(meta):
    """meta.json(dict)·synth_manifest 항목 → 양식. form이 없으면 김해 (옛 철 호환). 모르는 양식이면 ValueError"""
    form = (meta or {}).get("form") or DEFAULT_FORM
    if form not in FORMS:
        raise ValueError(f"모르는 양식 {form!r} (가능: {FORMS})")
    return form
