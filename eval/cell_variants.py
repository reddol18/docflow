"""손글씨 칸 판독 지시문·해상도 비교 (docflow Pod 안에서 실행: 잘라낸 칸과 키가 거기 있다).

사용: python cell_variants.py <vol> <정답 json 파일>   정답 = {doc_id: {"serial": "...", "date": "YYYY-MM-DD"}}
같은 칸을 변형마다 한 번씩 읽어 일련번호·일자 정답 수를 센다. 비용은 store.usage에 남기지 않으므로 여기서 직접 계산해 출력한다.
"""
import base64
import json
import math
import sys
from pathlib import Path

sys.path.insert(0, "/app")
from openai import OpenAI  # noqa: E402

from app import layout  # noqa: E402

OLD = {
    "docno": "1990년대 공문서 머리부의 '문서번호' 칸을 잘라낸 이미지다. 부서 약칭과 분류번호는 인쇄, 일련번호는 손글씨인 경우가 많다. "
             "칸 안의 값을 보이는 그대로 한 줄로 적어라 (예: 지경 55142-1558). 항목명 '문서번호'와 설명은 쓰지 말고 값만. "
             "일련번호 칸이 비어 있으면 '-'까지만 적는다.",
    "date": "1990년대 공문서 머리부의 '시행일자' 칸을 잘라낸 이미지다. 숫자는 손글씨인 경우가 많다. "
            "칸 안의 값을 보이는 그대로 한 줄로 적어라 (예: 97. 5. 15.). 항목명 '시행일자'와 설명은 쓰지 말고 값만.",
}
NEW = {
    "docno": "1990년대 공문서 머리부의 '문서번호' 칸을 잘라낸 이미지다. 앞부분(부서 약칭, 5자리 분류번호, '-')은 인쇄 글자이고, "
             "'-' 뒤에는 사람이 손으로 쓴 일련번호 숫자 1~5자리가 흘림체로 적혀 있는 경우가 대부분이다. "
             "손글씨 획을 숫자로 읽어 '부서 분류번호-일련번호' 한 줄로 적어라 (예: 지경 55142-1558). "
             "흘려 써서 애매해도 가장 그럴듯한 숫자를 적는다. '-' 뒤에 아무 획도 없을 때만 '-'로 끝낸다. 값만 적고 설명은 쓰지 않는다.",
    "date": "1990년대 공문서 머리부의 '시행일자' 칸을 잘라낸 이미지다. 연·월·일 숫자는 사람이 손으로 쓴 흘림체인 경우가 많다. "
            "손글씨 획을 숫자로 읽어 '연. 월. 일.' 한 줄로 적어라 (예: 97. 5. 15.). 연도는 2자리일 수 있다. "
            "흘려 써서 애매해도 가장 그럴듯한 숫자를 적는다. 값만 적고 설명은 쓰지 않는다.",
}
VARIANTS = {"old_low": (OLD, "low", "gpt-4o-mini"), "new_low": (NEW, "low", "gpt-4o-mini"),
            "new_high": (NEW, "high", "gpt-4o-mini"), "4o_high": (NEW, "high", "gpt-4o"),
            "41mini_high": (NEW, "high", "gpt-4.1-mini"), "41_high": (NEW, "high", "gpt-4.1")}
# USD / 토큰 (입력, 출력)
PRICE = {"gpt-4o-mini": (0.15e-6, 0.60e-6), "gpt-4o": (2.5e-6, 10e-6), "gpt-4.1-mini": (0.4e-6, 1.6e-6),
         "gpt-4.1": (2e-6, 8e-6)}


def main():
    vol, gt = sys.argv[1], json.loads(Path(sys.argv[2]).read_text(encoding="utf-8-sig"))
    names = sys.argv[3].split(",") if len(sys.argv) > 3 else list(VARIANTS)
    c = OpenAI(api_key=Path("/etc/openai/api-key").read_text().strip(), max_retries=4)
    crops = Path("/data/crops") / vol
    for name in names:
        prompts, detail, model = VARIANTS[name]
        pin, pout = PRICE[model]
        ok = {"serial": 0, "date": 0}
        n = {"serial": 0, "date": 0}
        usd = 0.0
        for doc_id, g in sorted(gt.items()):
            for field, key in [("docno", "serial"), ("date", "date")]:
                p = crops / f"{doc_id}_{field}.png"
                if not p.exists() or g.get(key) is None:
                    continue
                url = "data:image/png;base64," + base64.b64encode(p.read_bytes()).decode()
                r = c.chat.completions.create(model=model, temperature=0, logprobs=True, max_tokens=40, messages=[
                    {"role": "user", "content": [{"type": "text", "text": prompts[field]},
                                                 {"type": "image_url", "image_url": {"url": url, "detail": detail}}]}])
                usd += r.usage.prompt_tokens * pin + r.usage.completion_tokens * pout
                text = (r.choices[0].message.content or "").strip()
                lps = [t.logprob for t in r.choices[0].logprobs.content or []]
                conf = math.exp(sum(lps) / len(lps)) if lps else 0
                got = layout.serial_of(text) if key == "serial" else layout.date_of(text)
                hit = got == g[key] or ("?" in g[key] and len(got) == len(g[key]))
                n[key] += 1
                ok[key] += hit
                print(f"  {name} {doc_id} {key}: {text!r} conf={conf:.3f} gt={g[key]!r} {'O' if hit else 'X'}", flush=True)
        print(f"{name}: serial {ok['serial']}/{n['serial']}, date {ok['date']}/{n['date']}, ${usd:.4f}", flush=True)


if __name__ == "__main__":
    main()
