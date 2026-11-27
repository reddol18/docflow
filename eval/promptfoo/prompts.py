"""지시문 판 — promptfoo 행렬의 prompts 축. 판마다 (system, user) 메시지를 만든다.

  current         지금 운영 지시문 = 그 철 양식의 색인 스키마로 만든 것 (app/llm.extract와 같은 글자).
                  실제 철·김해 합성 철은 app/field_schemas/gimhae.yaml (= prompt_versions.NEW), 새 양식 합성 철
                  (agency1990·draft2000, https://github.com/reddol18/docflow/issues/139)은 common.schema_of(철)의 칸 정의
  before_ocr_fix  OCR 오독 교정·제목·수신 지시가 없던 처음 판 (김해 지시문의 옛 글자 — 양식과 상관없이 그대로)
판의 글자는 eval/prompt_versions.py. 새 판을 시험하려면 거기 하나 더 두고, 여기 함수와 promptfooconfig.yaml prompts에 넣는다.
"""
import common
from prompt_versions import NEW, OLD

from app import fields

SCHEMA = fields.load(common.SCHEMA_FILE)  # 김해 (실제 철·form 없는 옛 합성 철)
VERSIONS = {"current": NEW, "before_ocr_fix": OLD}


def system_of(name, vol):
    """판·철 → 시스템 지시문. current는 철 양식의 칸 정의로 (김해 철이면 NEW와 같은 글자)"""
    if name == "current" and common.form_of(vol) != common.forms.DEFAULT_FORM:
        return fields.extract_prompt(common.schema_of(vol))
    return VERSIONS[name]


def _messages(name, context):
    v = context["vars"]
    return [{"role": "system", "content": system_of(name, v["vol"])},
            {"role": "user", "content": common.user_text(v["vol"], v["doc_id"])}]


def current(context):
    return _messages("current", context)


def before_ocr_fix(context):
    return _messages("before_ocr_fix", context)
