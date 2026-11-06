"""4칸 텍스트 추출 지시문·모델 비교 (docflow Pod 안에서 실행: OCR 결과와 키가 거기 있다).

사용: python extract_variants.py <vol> <정답 json> [변형,...]   정답 = {doc_id: {docno, date, recipient, title}}
채점은 PoC 규칙(공백·구분기호 무시)으로 칸별 일치 수를 센다. 수신은 규칙 보정 전 값이다.
파드에서 돌릴 때는 eval/prompt_versions.py를 이 파일과 같은 폴더에 함께 복사한다 (지시문 판을 거기서 읽는다.
scripts/mac/pod_run.sh가 tmp/ 파일을 다 복사). python -I로 돌려도 이 파일의 폴더에서 찾는다.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, "/app")
sys.path.insert(0, str(Path(__file__).resolve().parent))  # python -I는 스크립트 폴더를 sys.path에 넣지 않는다
from openai import OpenAI  # noqa: E402

# OLD = 처음 지시문, NEW = 지금 운영 지시문(색인 스키마 app/field_schemas/gimhae.yaml로 만든 것). 정의는 prompt_versions 한 곳
# (예전 이 파일의 OLD.replace(...)로 만든 NEW와 바이트 단위로 같음을 2026-11-07 확인하고 옮김)
from prompt_versions import NEW, OLD  # noqa: E402

from app import layout  # noqa: E402

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
