"""run.ps1이 promptfoo를 부르기 전에 묻는 것 — 이번에 걸러질 제공자·지시문 판으로 (1) OpenAI 키가 필요한가 (2) 기준 후보와 겹치는가.

  python -I -X utf8 eval/promptfoo/plan.py --providers=<정규식> --prompts=<정규식> --local-model=<태그>
  → 표준 출력에 JSON {"providers": [걸러진 label], "prompts": [걸러진 label], "need_key": bool, "external": bool,
                      "baseline_overlap": bool}
external = 이 PC 밖(OpenAI·원격 GPU)으로 문서 글자를 보내는 후보가 있다 → run.ps1이 합성 철만인지 확인한다 (#80).

거르기는 promptfoo 0.125.0 --filter-providers/--filter-prompts와 같게 한다: 정규식(대소문자 구분)을 제공자 id와 label에 각각 대
어 하나라도 맞으면 남긴다 (지시문 판도 id·label). 빈 정규식 = 모두. JS RegExp.test = Python re.search.
키가 필요한 제공자 = id가 'openai:'로 시작하고 apiBaseUrl이 없는 것 (로컬 Ollama는 apiBaseUrl이 있다).
"""
import argparse
import json
import re
import sys
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent


def _hit(rx, *names):
    return not rx or any(n and re.search(rx, n) for n in names)


LOCAL_LABELS = {"local-ollama", "replay"}  # 이 PC 안에서 끝나는 후보 — 나머지(OpenAI·remote)는 외부로 글자를 보낸다


def plan(config, criteria, providers_rx="", prompts_rx="", local_model="", remote_model=""):
    """config·criteria = 읽은 YAML(dict). 반환은 모듈 설명의 JSON과 같다"""
    provs = []
    for p in config.get("providers") or []:
        p = p if isinstance(p, dict) else {"id": p}
        pid = (str(p.get("id", "")).replace("{{ env.DOCFLOW_LOCAL_MODEL }}", local_model)  # promptfoo가 거르기 전에 채우는 이름
               .replace("{{ env.DOCFLOW_REMOTE_MODEL }}", remote_model))
        provs.append({"id": pid, "label": p.get("label"), "config": p.get("config") or {}})
    chosen = [p for p in provs if _hit(providers_rx, p["id"], p["label"])]
    prompts = [p for p in config.get("prompts") or [] if _hit(prompts_rx, p.get("id"), p.get("label"))]
    base = criteria.get("baseline") or {}
    base_prov = any(p["label"] == base.get("provider") for p in chosen)
    base_prompt = any((p.get("label") or p.get("id")) == base.get("prompt") for p in prompts)
    return {"providers": [p["label"] or p["id"] for p in chosen],
            "prompts": [p.get("label") or p.get("id") for p in prompts],
            "need_key": any(p["id"].startswith("openai:") and not p["config"].get("apiBaseUrl") for p in chosen),
            "external": any(p["label"] not in LOCAL_LABELS for p in chosen),
            "baseline_overlap": base_prov and base_prompt}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(HERE / "promptfooconfig.yaml"))
    ap.add_argument("--criteria", default=str(HERE / "criteria.yaml"))
    ap.add_argument("--providers", default="")
    ap.add_argument("--prompts", default="")
    ap.add_argument("--local-model", default="")
    ap.add_argument("--remote-model", default="")
    a = ap.parse_args()
    read = lambda p: yaml.safe_load(Path(p).read_text(encoding="utf-8"))  # noqa: E731
    print(json.dumps(plan(read(a.config), read(a.criteria), a.providers, a.prompts, a.local_model, a.remote_model),
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
