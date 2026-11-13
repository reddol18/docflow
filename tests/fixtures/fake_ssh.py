"""가짜 ssh·scp (tests/test_remote_helper.py 전용). 맥 없이 scripts/lib/remote.ps1이 넘기는 것을 그대로 남긴다.

FAKE_SSH_DIR 폴더에 argv.json(받은 인자), script.txt(base64를 푼 스크립트 바이트), stdin.bin(표준 입력 바이트)을 쓴다.
FAKE_SSH_MODE: echo(기본, 한글 한 줄 출력 + 표준 오류 한 줄) | sleep(30초 잠 — 시간 제한 시험)
FAKE_SSH_EXIT: 종료 코드 (기본 0)
"""
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

CMD = re.compile(r"^sh -c 'exec 4<&0; echo ([A-Za-z0-9+/=]+) \| base64 -D \| sh /dev/fd/3 3<&0 0<&4 4<&-'$")


def main() -> int:
    out = Path(os.environ["FAKE_SSH_DIR"])
    argv = sys.argv[1:]
    (out / "argv.json").write_text(json.dumps(argv, ensure_ascii=False), encoding="utf-8")
    if os.environ.get("FAKE_SSH_MODE") == "sleep":
        time.sleep(30)
        return 0
    m = CMD.match(argv[-1]) if argv else None
    if m:
        (out / "script.txt").write_bytes(base64.b64decode(m.group(1)))
    (out / "stdin.bin").write_bytes(sys.stdin.buffer.read())
    sys.stdout.buffer.write("출력 한글 줄\n둘째 줄\n".encode("utf-8"))
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write("오류 줄\n".encode("utf-8"))
    return int(os.environ.get("FAKE_SSH_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
