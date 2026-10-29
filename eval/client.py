"""Windows에서 docflow API를 부르는 작은 도구 (표준 라이브러리만). 먼저 tunnel.ps1로 localhost:8000을 연다.

  upload <vol> <폴더>          그 폴더의 <vol>-*.jpg를 20장씩 올린다 (#88 원본 폴더는 읽기만 한다)
  run <vol> [human|auto]
  status <vol>
  save <vol>                   문서 결과를 work/platform/<vol>.json으로 저장 (git 제외 폴더)
  get <경로>                   예: get "/search?q=공장설립"
"""
import json
import sys
import time
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

BASE = "http://127.0.0.1:8000"
ROOT = Path(__file__).resolve().parents[2]


def call(method, path, body=None, headers=None):
    req = urllib.request.Request(BASE + path, data=body, method=method, headers=headers or {})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


SRC88 = Path(r"E:\claude-dev\aihub_extracted\88\원천데이터\인.허가\5350109")


def upload(vol, folder=None):
    # 폴더를 안 주면 #88 원본에서 연도 폴더를 찾는다. 한글 경로를 PowerShell 5.1 스크립트에 쓰면 깨져서 여기서 정한다
    folder = Path(folder) if folder else SRC88 / vol.split("-")[1]
    files = sorted(folder.rglob(f"{vol}-*.jpg"))
    if not files:
        raise SystemExit(f"no files for {vol} in {folder}")
    for i in range(0, len(files), 20):
        b = uuid.uuid4().hex
        parts = []
        for f in files[i:i + 20]:
            parts.append(f"--{b}\r\nContent-Disposition: form-data; name=\"files\"; filename=\"{f.name}\"\r\n"
                         f"Content-Type: image/jpeg\r\n\r\n".encode() + f.read_bytes() + b"\r\n")
        body = b"".join(parts) + f"--{b}--\r\n".encode()
        for attempt in range(3):  # port-forward가 가끔 연결을 끊는다
            try:
                r = call("POST", f"/volumes/{vol}/pages", body, {"Content-Type": f"multipart/form-data; boundary={b}"})
                break
            except OSError as e:
                print(f"retry {attempt + 1}: {e}", flush=True)
                time.sleep(5)
        else:
            raise SystemExit(f"upload failed: {vol}")
        print(r, flush=True)


def main():
    cmd, args = sys.argv[1], sys.argv[2:]
    if cmd == "upload":
        upload(args[0], args[1] if len(args) > 1 else None)
    elif cmd == "run":
        mode = args[1] if len(args) > 1 else "human"
        print(call("POST", f"/volumes/{args[0]}/run?review={mode}"))
    elif cmd == "status":
        print(json.dumps(call("GET", f"/volumes/{args[0]}"), ensure_ascii=False, indent=1))
    elif cmd == "save":
        out = ROOT / "work" / "platform" / f"{args[0]}.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        docs = call("GET", f"/volumes/{args[0]}/documents")
        out.write_text(json.dumps(docs, ensure_ascii=False, indent=1), encoding="utf-8")
        print(f"{len(docs)} docs -> {out}")
    elif cmd == "get":
        p = urllib.parse.quote(args[0], safe="/?=&")
        print(json.dumps(call("GET", p), ensure_ascii=False, indent=1))
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
