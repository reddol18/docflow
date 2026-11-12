"""scripts/mac/disk_check.sh — 이미지 디스크 사용률이 kubelet GC 문턱(- 여유)에 걸리면 멈추는지,
scripts/mac/image_list.sh — 이미지마다 누가 쓰는지 판단 (https://github.com/reddol18/docflow/issues/119).

맥(colima)에서 도는 sh 스크립트를 시험 값(DISK_TEST_*, IMAGE_LIST_TEST_*)으로 돌린다 — colima·kubectl·맥 df를 부르지 않는다.
sh가 있으면 그것으로, Windows에서는 WSL(`wsl -e sh`)로. 둘 다 없으면 건너뛴다(이유를 남김).
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
DISK = "scripts/mac/disk_check.sh"
LIST = "scripts/mac/image_list.sh"


def _runner():
    sh = shutil.which("sh")
    if sh:
        return [sh], False
    if os.name == "nt" and shutil.which("wsl"):
        try:
            ok = subprocess.run(["wsl", "-e", "sh", "-c", "echo ok"], capture_output=True, timeout=60).stdout.strip() == b"ok"
        except (OSError, subprocess.TimeoutExpired):
            ok = False
        if ok:
            return ["wsl", "-e"], True
    return None, False


RUNNER, VIA_WSL = _runner()
pytestmark = pytest.mark.skipif(RUNNER is None, reason="sh도 WSL도 없음 — 맥에서 손으로 확인 (PR 수동 확인 칸)")


def run(script, *args, **env):
    """환경 변수는 env 명령으로 넘긴다 (WSL은 Windows 환경 변수를 그대로 넘기지 않는다).
    PATH는 그대로 — colima·kubectl을 부르면 실패해야 한다."""
    if VIA_WSL:
        cmd, penv = ["wsl", "-e", "env", *(f"{k}={v}" for k, v in env.items()), "sh", script, *args], None
    else:
        cmd, penv = [RUNNER[0], script, *args], {**os.environ, **env}
    p = subprocess.run(cmd, cwd=ROOT, env=penv, capture_output=True, timeout=60)
    return p.returncode, p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")


# 모든 측정을 시험 값으로 (colima·kubectl을 부르지 않게), 문턱은 기본값(85/80)
BASE = dict(DISK_TEST_IMG_SIZE_GB="30", DISK_TEST_VM_AVAIL_GB="20", DISK_TEST_HOST_AVAIL_GB="30", DISK_GC_QUERY="0")


def disk(*args, **env):
    return run(DISK, *args, **{**BASE, **env})


def test_under_limit_passes_and_shows_threshold():
    code, out = disk("2", DISK_TEST_IMG_PCT="70")  # 70% + 2/30GB(6.7%) → 77% < 85-3
    assert code == 0, out
    assert "시험 값 사용" in out  # 시험 값이면 늘 보인다
    assert "지금 70%" in out and "2GB 더 쓰면 77%" in out and "멈춤 기준 82%" in out
    assert "GC high 85%" in out and "low 80%" in out and "kubelet 기본값" in out


def test_over_gc_threshold_stops():
    code, out = disk(DISK_TEST_IMG_PCT="84")  # 2026-11-10처럼 문턱 가까이
    assert code == 1
    assert "!! 이미지 디스크 사용률 84%가 멈춤 기준 82% 이상" in out and "image_list.sh" in out


def test_planned_gb_pushes_over_threshold():
    assert disk("0", DISK_TEST_IMG_PCT="76")[0] == 0
    code, out = disk("2", DISK_TEST_IMG_PCT="76")  # 76% + 6.7% → 83% (올림) ≥ 82%
    assert code == 1 and "83%" in out


def test_boundary_is_stop():
    assert disk(DISK_TEST_IMG_PCT="81")[0] == 0
    assert disk(DISK_TEST_IMG_PCT="82")[0] == 1  # 기준과 같으면 멈춤


def test_threshold_and_margin_from_env():
    code, out = disk(DISK_TEST_IMG_PCT="84", DISK_GC_HIGH_PCT="90", DISK_GC_LOW_PCT="85")
    assert code == 0 and "멈춤 기준 87%" in out and "문턱 출처: 환경 변수" in out
    code, out = disk(DISK_TEST_IMG_PCT="84", DISK_GC_MARGIN_PCT="0")
    assert code == 0 and "멈춤 기준 85%" in out
    assert disk(DISK_TEST_IMG_PCT="80", DISK_GC_MARGIN_PCT="10")[0] == 1


def test_free_gb_checks_still_stop():
    code, out = disk("2", DISK_TEST_IMG_PCT="50", DISK_TEST_VM_AVAIL_GB="5")  # 필요 4+2
    assert code == 1 and "colima 디스크 여유가 모자람" in out
    code, out = disk(DISK_TEST_IMG_PCT="50", DISK_TEST_HOST_AVAIL_GB="9")
    assert code == 1 and "맥 디스크 여유가 모자람" in out


@pytest.mark.parametrize("env", [dict(DISK_TEST_IMG_PCT="8x"), dict(DISK_TEST_IMG_PCT="50", DISK_GC_MARGIN_PCT="-1"),
                                 dict(DISK_TEST_IMG_PCT="50", DISK_GC_HIGH_PCT="85%")])
def test_bad_values_stop(env):
    code, out = disk(**env)
    assert code == 1 and "정수가 아님" in out


def test_bad_need_more_stops():
    code, out = disk("2GB", DISK_TEST_IMG_PCT="50")
    assert code == 1 and "정수가 아님" in out


def test_without_test_values_needs_colima():
    # 시험 값이 없으면 실제로 colima를 읽어야 하고, 못 읽으면 통과시키지 않는다 (이 PC에는 colima가 없다)
    code, out = run(DISK, DISK_GC_QUERY="0", DISK_TEST_HOST_AVAIL_GB="30")
    assert code != 0


def test_deploy_clears_test_values():
    text = (ROOT / "scripts/mac/deploy.sh").read_text(encoding="utf-8")
    for v in ("DISK_TEST_IMG_PCT", "DISK_TEST_IMG_SIZE_GB", "DISK_TEST_VM_AVAIL_GB", "DISK_TEST_HOST_AVAIL_GB",
              "DISK_GC_HIGH_PCT", "DISK_GC_LOW_PCT", "DISK_GC_QUERY"):
        assert v in text.split("unset", 1)[1].splitlines()[0], v
    # 롤아웃(apply·재시작) 전마다 이미지 확인
    lines = text.splitlines()
    for marker, img in (("kubectl apply -f", "docflow-web:dev"), ("rollout restart deploy/docflow", "docflow:dev"),
                        ("rollout restart deploy/web", "docflow-web:dev")):
        i = next(n for n, line in enumerate(lines) if marker in line and not line.lstrip().startswith("#"))
        before = [line for line in lines[max(0, i - 4):i] if line.startswith("ensure_image")]
        assert any(f"ensure_image {img} " in line for line in before), marker


WORKLOADS = "\n".join([
    "docs\tdocflow:dev ",
    "docs\tdocflow-web:dev docflow-web:dev ",
    "docs\tpostgres:17.11-alpine@sha256:" + "a" * 64 + " ",
    "bank-a\tbank-api:1.2 ",
    "sllm\tollama/ollama:0.5.0 ",
    "kube-system\trancher/mirrored-coredns-coredns:1.12.0 ",
])
IMAGES = "\n".join([
    "docflow dev 1111111111111111 2.0 GB",
    "docflow <none> 2222222222222222 1.9 GB",            # 예전 빌드 (태그 빠짐)
    "docflow-web dev 3333333333333333 400 MB",
    "postgres 17.11-alpine 4444444444444444 280 MB",
    "postgres <none> 4444444444444444 280 MB",           # 같은 ID의 다이제스트 행
    "docker.io/library/bank-api 1.2 5555555555555555 300 MB",
    "ollama/ollama 0.5.0 6666666666666666 3.4 GB",       # 0개로 줄였어도 정의가 있어 '쓰는 곳'
    "some/tool 1.0 7777777777777777 50 MB",              # 주인 모름
    "rancher/mirrored-pause 3.6 8888888888888888 700 KB",
    "rancher/mirrored-coredns-coredns 1.12.0 9999999999999999 70 MB",
    "docflow old 0000000000000000 1.8 GB",
    "bank-api <none> aaaaaaaaaaaaaaaa 290 MB",            # 다른 프로젝트의 예전 빌드
    "postgres 16-alpine bbbbbbbbbbbbbbbb 270 MB",         # docs의 예전 판
])


def test_image_list_classifies_without_touching_cluster():
    code, out = run(LIST, IMAGE_LIST_TEST_WORKLOADS=WORKLOADS, IMAGE_LIST_TEST_IMAGES=IMAGES)
    assert code == 0, out
    rows = {line.split()[0]: line for line in out.splitlines() if line[:1] in "0123456789abcdef" and line[:1] != ""}

    def verdict(image_id):
        return rows[image_id[:12]]

    assert "docs 사용 → 지우지 않음" in verdict("1111111111111111")
    assert "docflow:<none>" in verdict("2222222222222222") and "dangling" in verdict("2222222222222222")
    assert "후보" in verdict("2222222222222222")
    assert "docs 사용" in verdict("3333333333333333")
    assert "docs 사용" in verdict("4444444444444444")  # 태그·다이제스트 두 행 모두
    assert all("docs 사용" in line for line in out.splitlines() if line.startswith("444444444444"))
    assert "bank-a 사용 → 다른 프로젝트" in verdict("5555555555555555")
    assert "sllm 사용 → 다른 프로젝트" in verdict("6666666666666666")
    assert "주인 확인 전엔 지우지 않음" in verdict("7777777777777777")
    assert "시스템(pause)" in verdict("8888888888888888")
    assert "시스템(kube-system)" in verdict("9999999999999999")
    assert "docs 이미지의 예전 판 → 후보" in verdict("0000000000000000")
    assert "docs 이미지의 예전 판 → 후보" in verdict("bbbbbbbbbbbbbbbb")
    assert "dangling" in verdict("aaaaaaaaaaaaaaaa") and "bank-a가 씀 → 다른 프로젝트 것, 대상 아님" in verdict("aaaaaaaaaaaaaaaa")
    # 다른 프로젝트 것은 어느 것도 '후보'가 아니다
    for i in ("5555555555555555", "6666666666666666", "7777777777777777", "8888888888888888", "9999999999999999",
              "aaaaaaaaaaaaaaaa"):
        assert "후보" not in verdict(i), verdict(i)


def test_image_list_has_no_delete_command():
    text = (ROOT / LIST).read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#") and "echo" not in line)
    for bad in (" rmi", "image rm", "image prune", "builder prune", "system prune", "crictl rmi", "kubectl delete", "kubectl scale"):
        assert bad not in code, bad
