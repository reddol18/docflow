"""영상용 시간 압축 시나리오를 만든다. 대기열 동작은 시간 축을 같은 배율로 바꾸면 그대로다
(도착률 x k, 처리 시간·시간 제한·시각 / k).

실행: python -I -X utf8 sim/compress.py sim/s2_queue.json 10   → sim/s2_queue_x10.json
"""
import json
import sys
from pathlib import Path

TIME_KEYS = {"serviceMs", "timeoutMs"}
RATE_KEYS = {"rps", "rateLimitRps"}  # 토큰 버킷 크기(burst)는 개수라 그대로 둔다


def main():
    src, k = Path(sys.argv[1]), float(sys.argv[2])
    d = json.loads(src.read_text(encoding="utf-8"))
    tag = f"_x{int(k)}"
    d["name"] = f"{d['name']} ({int(k)}배 압축)"
    if d.get("description"):
        d["description"] += f" · 화면 시각 = 원래 시각 ÷ {int(k)}"
    d["seconds"] = d["seconds"] / k
    for n in d["design"]["nodes"]:
        c = n.get("config", {})
        for key in RATE_KEYS & c.keys():
            c[key] = c[key] * k
        for key in TIME_KEYS & c.keys():
            c[key] = c[key] / k
    for it in d.get("timeline", []):
        it["at"] = it["at"] / k
        for patch in it.get("set", {}).values():
            for key in RATE_KEYS & patch.keys():
                patch[key] = patch[key] * k
            for key in TIME_KEYS & patch.keys():
                patch[key] = patch[key] / k
    dst = src.with_name(src.stem + tag + ".json")
    dst.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    print(dst)


if __name__ == "__main__":
    main()
