"""4칸 텍스트 추출 지시문·모델 비교 (docflow Pod 안에서 실행: OCR 결과와 키가 거기 있다).

사용: python extract_variants.py <vol> <정답 json> [변형,...]   정답 = {doc_id: {docno, date, recipient, title}}
채점은 PoC 규칙(공백·구분기호 무시)으로 칸별 일치 수를 센다. 수신은 규칙 보정 전 값이다.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")
from openai import OpenAI  # noqa: E402

from app import layout, llm  # noqa: E402

# OLD = 처음 지시문 (지금 app/llm.py의 EXTRACT_PROMPT는 NEW를 반영한 것)
OLD = llm.EXTRACT_PROMPT.split("OCR 텍스트에는")[0] + "칸마다 confidence(0~1)를 붙인다. 결재란 단어" + \
    llm.EXTRACT_PROMPT.split("칸마다 confidence(0~1)를 붙인다. 결재란 단어", 1)[1]
NEW = OLD.replace(
    "칸마다 confidence(0~1)를 붙인다.",
    "OCR 텍스트에는 글자를 잘못 읽은 곳이 많다 (예: '회담'→'회답', '필중'→'필증', '파른'→'따른', '실비'→'설비', '참유변상'→'장유면장'). "
    "행정 문서 문맥상 명백한 오독은 바른 글자로 고쳐 적는다. 숫자·고유명사는 근거 없이 바꾸지 않는다.\n"
    "제목은 '제목' 항목 줄의 끝까지 빠짐없이 적는다 (따옴표로 쓴 \"건의\", 괄호 안 내용 포함). "
    "수신은 '수신' 항목의 값만 적고(주소·상호·'대표'까지), 결재란·담당자·참조 부서 글자는 넣지 않는다.\n"
    "칸마다 confidence(0~1)를 붙인다.",
)
VARIANTS = {"old_4omini": (OLD, "gpt-4o-mini"), "new_4omini": (NEW, "gpt-4o-mini"),
            "new_41mini": (NEW, "gpt-4.1-mini"), "new_41": (NEW, "gpt-4.1")}
PRICE = {"gpt-4o-mini": (0.15e-6, 0.60e-6), "gpt-4.1-mini": (0.4e-6, 1.6e-6), "gpt-4.1": (2e-6, 8e-6)}
FIELDS = ["docno", "date", "recipient", "title"]


def ok(f, got, gt):
    if f == "date":
        return got[:len(gt)] == gt and len(got) >= len(gt) if gt else not got
    return layout.norm(got) == layout.norm(gt)


def main():
    vol, gt = sys.argv[1], json.loads(Path(sys.argv[2]).read_text(encoding="utf-8-sig"))
    names = sys.argv[3].split(",") if len(sys.argv) > 3 else list(VARIANTS)
    c = OpenAI(api_key=Path("/etc/openai/api-key").read_text().strip(), max_retries=4)
    for name in names:
        prompt, model = VARIANTS[name]
        pin, pout = PRICE[model]
        hit = {f: 0 for f in FIELDS}
        usd = 0.0
        for doc_id, g in sorted(gt.items()):
            d = json.loads((Path("/data/ocr") / vol / f"{doc_id}.json").read_text(encoding="utf-8"))
            r = c.chat.completions.create(model=model, temperature=0, response_format={"type": "json_object"},
                                          messages=[{"role": "system", "content": prompt},
                                                    {"role": "user", "content": "\n".join(d["lines"][:80])}])
            usd += r.usage.prompt_tokens * pin + r.usage.completion_tokens * pout
            out = json.loads(r.choices[0].message.content)
            for f in FIELDS:
                got = str((out.get(f) or {}).get("value") or "")
                good = ok(f, got, g[f])
                hit[f] += good
                if not good and f in ("title", "recipient"):
                    print(f"  {name} {doc_id.split('-')[-1]} {f}: {got!r} / gt {g[f]!r}", flush=True)
        n = len(gt)
        print(f"{name}: " + ", ".join(f"{f} {hit[f]}/{n}" for f in FIELDS) + f", ${usd:.4f}", flush=True)


if __name__ == "__main__":
    main()
