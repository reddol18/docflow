"""지시문 판 — promptfoo 행렬의 prompts 축. 판마다 (system, user) 메시지를 만든다.

  current         지금 운영 지시문 = 색인 스키마 app/field_schemas/gimhae.yaml로 만든 것 (app/llm.extract와 같은 글자)
  before_ocr_fix  OCR 오독 교정·제목·수신 지시가 없던 처음 판
판의 글자는 eval/prompt_versions.py. 새 판을 시험하려면 거기 하나 더 두고, 여기 함수와 promptfooconfig.yaml prompts에 넣는다.
"""
import common
from prompt_versions import NEW, OLD

from app import fields

SCHEMA = fields.load(common.SCHEMA_FILE)
VERSIONS = {"current": NEW, "before_ocr_fix": OLD}


def _messages(name, context):
    v = context["vars"]
    return [{"role": "system", "content": VERSIONS[name]},
            {"role": "user", "content": common.user_text(v["vol"], v["doc_id"])}]


def current(context):
    return _messages("current", context)


def before_ocr_fix(context):
    return _messages("before_ocr_fix", context)
