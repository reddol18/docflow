"""4칸 추출 지시문 판 — eval/extract_variants.py(파드 안 비교)와 eval/promptfoo/(모델 비교 하네스)가 같이 쓴다.
app.fields만 불러온다 (openai 없음 — promptfoo가 시험마다 Python을 새로 띄워서 가벼워야 한다).

  NEW  지금 운영 지시문 = 색인 스키마 app/field_schemas/gimhae.yaml로 만든 것 (app/llm.extract와 같은 글자)
  OLD  OCR 오독 교정·제목·수신 지시가 없던 처음 판
"""
from app import fields

NEW = fields.extract_prompt(fields.load(fields.DIR / "gimhae.yaml"))
OLD = NEW.split("OCR 텍스트에는")[0] + "칸마다 confidence(0~1)를 붙인다. 결재란 단어" + \
    NEW.split("칸마다 confidence(0~1)를 붙인다. 결재란 단어", 1)[1]
