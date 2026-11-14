"""가짜 ssh·scp (tests/test_remote_helper.py 전용, python -I로 부른다). 맥 없이 scripts/lib/remote.ps1이 넘기는 것을 남긴다.

FAKE_SSH_DIR 폴더에 쓴다:
  argv.json   마지막 호출의 인자
  calls.jsonl 호출마다 {argv, script, pidfile} (script = 풀어 낸 스크립트, ssh 명령 꼴이 아니면 null)
  script.txt  마지막 스크립트 바이트, boot.txt 부트스트랩 바이트, stdin.bin 표준 입력 바이트(-n이면 읽지 않고 빈 파일)
FAKE_SSH_MODE:
  echo (기본)  한글 두 줄 출력 + 표준 오류 한 줄, 종료 코드 FAKE_SSH_EXIT (기본 0)
  sleep        30초 잠 (시간 제한 시험)
  (모든 모드) 멈추기 스크립트(kill -TERM)면 그 pidfile을 kill.txt에 적고 '멈춤'을 출력
  decodefail   맥 /usr/bin/base64 -D가 실패한 것처럼: 부트스트랩의 오류 줄 + 종료 코드 97
"""
import base64
import json
import os
import re
import sys
import time
from pathlib import Path

CMD = re.compile(
    r"^sh -c 'b=\$\(echo ([A-Za-z0-9+/=]+) \| /usr/bin/base64 -D\) && \[ -n \"\$b\" \] && "
    r"exec /bin/sh -c \"\$b\" invoke-mac ([A-Za-z0-9+/=]+) (/tmp/invoke-mac\.[0-9a-f]{32}\.pid); "
    r"echo invoke-mac: decode-failed >&2; exit 97'$"
)


def main() -> int:
    out = Path(os.environ["FAKE_SSH_DIR"])
    argv = sys.argv[1:]
    (out / "argv.json").write_text(json.dumps(argv, ensure_ascii=False), encoding="utf-8")
    m = CMD.match(argv[-1]) if argv else None
    script = base64.b64decode(m.group(2)).decode("utf-8") if m else None
    with (out / "calls.jsonl").open("a", encoding="utf-8") as f:
        f.write(json.dumps({"argv": argv, "script": script, "pidfile": m.group(3) if m else None}, ensure_ascii=False) + "\n")
    if not m:  # scp
        return int(os.environ.get("FAKE_SSH_EXIT", "0"))
    (out / "boot.txt").write_bytes(base64.b64decode(m.group(1)))
    (out / "script.txt").write_bytes(base64.b64decode(m.group(2)))
    mode = os.environ.get("FAKE_SSH_MODE", "echo")
    if "kill -TERM" in script:
        (out / "kill.txt").write_text(re.search(r"^pf=(\S+)$", script, re.M).group(1), encoding="utf-8")
        sys.stdout.buffer.write("프로세스 그룹 4242 멈춤(TERM) / colima VM 안 docflow 빌드 없음\n".encode("utf-8"))
        return 0
    if mode == "sleep":
        time.sleep(30)
        return 0
    if mode == "decodefail":
        sys.stderr.buffer.write("invoke-mac: decode-failed (스크립트 base64 -D 실패 또는 빈 스크립트)\n".encode("utf-8"))
        return 97
    (out / "stdin.bin").write_bytes(b"" if "-n" in argv else sys.stdin.buffer.read())
    sys.stdout.buffer.write("출력 한글 줄\n둘째 줄\n".encode("utf-8"))
    sys.stdout.buffer.flush()
    sys.stderr.buffer.write("오류 줄\n".encode("utf-8"))
    return int(os.environ.get("FAKE_SSH_EXIT", "0"))


if __name__ == "__main__":
    sys.exit(main())
