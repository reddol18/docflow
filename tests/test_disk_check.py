"""scripts/mac/disk_check.sh — 이미지 디스크 사용률이 kubelet GC 문턱(- 여유)에 걸리면 멈추는지,
scripts/mac/image_list.sh — 이미지마다 누가 쓰는지 판단 (https://github.com/reddol18/docflow/issues/119).

맥(colima)에서 도는 sh 스크립트를 시험 값(DISK_TEST_*, IMAGE_LIST_TEST_*)으로 돌린다 — colima·kubectl·맥 df를 부르지 않는다.
sh가 있으면 그것으로, Windows에서는 WSL(`wsl -e sh`)로. 둘 다 없으면 건너뛴다(이유를 남김).
"""
import os
import re
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


def run_code(code, **env):
    """sh -c 코드 (저장소 폴더에서)."""
    if VIA_WSL:
        cmd, penv = ["wsl", "-e", "env", *(f"{k}={v}" for k, v in env.items()), "sh", "-c", code], None
    else:
        cmd, penv = [RUNNER[0], "-c", code], {**os.environ, **env}
    p = subprocess.run(cmd, cwd=ROOT, env=penv, capture_output=True, timeout=120)
    return p.returncode, p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")


def run(script, *args, **env):
    """환경 변수는 env 명령으로 넘긴다 (WSL은 Windows 환경 변수를 그대로 넘기지 않는다).
    PATH는 그대로 — colima·kubectl을 부르면 실패해야 한다."""
    if VIA_WSL:
        cmd, penv = ["wsl", "-e", "env", *(f"{k}={v}" for k, v in env.items()), "sh", script, *args], None
    else:
        cmd, penv = [RUNNER[0], script, *args], {**os.environ, **env}
    p = subprocess.run(cmd, cwd=ROOT, env=penv, capture_output=True, timeout=60)
    return p.returncode, p.stdout.decode("utf-8", "replace") + p.stderr.decode("utf-8", "replace")



# 시험 값은 넷을 함께 (colima·kubectl·맥 df를 부르지 않게). 문턱은 조회하지 않고 기본값 85/80
BASE = dict(DISK_TEST_IMG_SIZE_GB="30", DISK_TEST_VM_AVAIL_GB="20", DISK_TEST_HOST_AVAIL_GB="30", DISK_GC_QUERY="0")
URL119 = "https://github.com/reddol18/docflow/issues/119"


def disk(*args, **env):
    return run(DISK, *args, **{**BASE, **env})


def test_under_limit_passes_and_shows_threshold():
    # kubelet 식: 100 - 내림(여유*100/크기). 70% → 여유 9GB, 2GB 더 → 7/30 = 23.3% → 77%
    code, out = disk("2", DISK_TEST_IMG_PCT="70")
    assert code == 0, out
    assert "!! 시험 값 사용: 이미지 70%/30GB, colima 여유 20GB, 맥 여유 30GB" in out  # 시험 값이면 늘 보인다
    assert "지금 70%, 이번에 2GB 더 쓰면 77% — 82% 이상이면 멈춤" in out
    assert "GC high 85% - 여유 3%p, low 80%; 문턱 출처: kubelet 기본값 (DISK_GC_QUERY=0, 조회 안 함)" in out
    assert "디스크 여유: colima 20GB (필요 6GB), 맥 30GB (필요 10GB)" in out


def test_over_gc_threshold_stops():
    code, out = disk(DISK_TEST_IMG_PCT="84")  # 2026-11-10처럼 문턱 가까이
    assert code == 1
    assert f"!! 이미지 디스크 사용률 84%가 멈춤 기준 82% 이상 — kubelet 이미지 GC(85%)가 배포 중 이미지를 지울 수 있다 ({URL119})" in out
    assert "sh ~/doc_platform/scripts/image_list.sh" in out


def test_planned_gb_pushes_over_threshold():
    assert disk("0", DISK_TEST_IMG_PCT="76")[0] == 0
    code, out = disk("2", DISK_TEST_IMG_PCT="76")  # 여유 7.2GB - 2 = 5.2/30 = 17.3% → 83%
    assert code == 1 and "2GB 더 쓰면 83%" in out and "83%가 멈춤 기준 82% 이상" in out


def test_boundary_is_stop():
    assert disk(DISK_TEST_IMG_PCT="81")[0] == 0
    code, out = disk(DISK_TEST_IMG_PCT="82")  # 기준과 같으면 멈춤 (kubelet도 high '이상'에서 GC)
    assert code == 1 and "82%가 멈춤 기준 82% 이상" in out


def test_megabytes_and_rebuild_mode():
    # 다시 빌드 경로: 이미지 하나 크기(MB)만큼, 여유 1%p (high 85 → 84% 이상이면 멈춤)
    code, out = disk("1400M", "rebuild", DISK_TEST_IMG_PCT="78")  # 여유 6.6GB - 1.37 = 17.4% → 83%
    assert code == 0, out
    assert "1400M 더 쓰면 83% — 84% 이상이면 멈춤 (GC high 85% - 여유 1%p [rebuild]" in out
    code, out = disk("1400M", "rebuild", DISK_TEST_IMG_PCT="79")  # → 84%: high 바로 아래까지 채우지 않는다
    assert code == 1 and "84%가 멈춤 기준 84% 이상" in out
    assert disk("0", "rebuild", DISK_TEST_IMG_PCT="83")[0] == 0
    assert disk("0", DISK_TEST_IMG_PCT="83")[0] == 1  # 보통 경로는 여유 3%p
    assert disk("0", "rebuild", DISK_TEST_IMG_PCT="83", DISK_GC_REBUILD_MARGIN_PCT="2")[0] == 1


def test_threshold_and_margin_from_env():
    code, out = disk(DISK_TEST_IMG_PCT="84", DISK_GC_HIGH_PCT="90", DISK_GC_LOW_PCT="85")
    assert code == 0 and "87% 이상이면 멈춤" in out and "low 85%; 문턱 출처: 환경 변수" in out
    code, out = disk(DISK_TEST_IMG_PCT="84", DISK_GC_MARGIN_PCT="0")
    assert code == 0 and "85% 이상이면 멈춤" in out
    assert disk(DISK_TEST_IMG_PCT="80", DISK_GC_MARGIN_PCT="10")[0] == 1


def run_fake(code, **env):
    """PATH 앞에 가짜 colima·kubectl(tests/fixtures/fakebin을 임시 폴더로 복사해 실행 권한)을 두고 sh 코드를 돌린다."""
    pre = ('B=$(mktemp -d); cp tests/fixtures/fakebin/* "$B/"; chmod +x "$B"/*; export PATH="$B:$PATH" FAKE="$B/state";'
           ' mkdir "$FAKE"; echo sha256:new > "$FAKE/new_id";\n')
    return run_code(pre + code + '\nrc=$?; rm -rf "$B"; exit $rc', **env)


def test_unreadable_threshold_is_conservative():
    # 문턱을 못 읽으면(가짜 kubectl이 실패. 진짜 kubectl이 PATH 앞에 있어도 KUBECONFIG=/dev/null) 멈추지 않고 보수적으로 high 80%
    env = {k: v for k, v in BASE.items() if k != "DISK_GC_QUERY"}
    code, out = run_fake(f"sh {DISK}", DISK_TEST_IMG_PCT="76", KUBECONFIG="/dev/null", **env)
    assert code == 0, out
    assert f"!! kubelet GC 문턱을 노드 configz에서 읽지 못함 — 보수적으로 high 80%로 판단 ({URL119})" in out
    assert "77% 이상이면 멈춤 (GC high 80% - 여유 3%p, low ?%; 문턱 출처: !! 실제 설정을 읽지 못해 보수적 값)" in out
    code, out = run_fake(f"sh {DISK}", DISK_TEST_IMG_PCT="77", KUBECONFIG="/dev/null", **env)
    assert code == 1 and "77%가 멈춤 기준 77% 이상" in out


def test_free_gb_checks_still_stop():
    code, out = disk("2", DISK_TEST_IMG_PCT="50", DISK_TEST_VM_AVAIL_GB="5")  # 필요 4+2
    assert code == 1 and "colima 5GB (필요 6GB)" in out and "colima 디스크 여유가 모자람" in out
    code, out = disk(DISK_TEST_IMG_PCT="50", DISK_TEST_HOST_AVAIL_GB="9")
    assert code == 1 and "맥 9GB (필요 10GB)" in out and "맥 디스크 여유가 모자람" in out


@pytest.mark.parametrize("args, env, msg", [
    ((), dict(DISK_TEST_IMG_PCT="8x"), "DISK_TEST_IMG_PCT 값이 0 이상의 정수가 아님: '8x'"),
    ((), dict(DISK_TEST_IMG_PCT="50", DISK_GC_MARGIN_PCT="-1"), "DISK_GC_MARGIN_PCT 값이 0 이상의 정수가 아님: '-1'"),
    ((), dict(DISK_TEST_IMG_PCT="50", DISK_GC_HIGH_PCT="85%"), "GC high 문턱 값이 0 이상의 정수가 아님: '85%'"),
    ((), dict(DISK_TEST_IMG_PCT="50", DISK_MIN_VM_GB="1234567"), "DISK_MIN_VM_GB 값이 너무 큼(6자리까지): '1234567'"),
    ((), dict(DISK_TEST_IMG_PCT="101"), "DISK_TEST_IMG_PCT는 100 이하"),
    (("2GB",), dict(DISK_TEST_IMG_PCT="50"), "더 쓸 양(GB) 값이 0 이상의 정수가 아님: '2GB'"),
    (("12M3",), dict(DISK_TEST_IMG_PCT="50"), "더 쓸 양(GB) 값이 0 이상의 정수가 아님: '12M3'"),
    (("1234567M",), dict(DISK_TEST_IMG_PCT="50"), "더 쓸 양(MB) 값이 너무 큼(6자리까지): '1234567'"),
    (("1", "fast"), dict(DISK_TEST_IMG_PCT="50"), "두 번째 인자는 rebuild만 받는다: 'fast'"),
    ((), dict(DISK_TEST_IMG_PCT="50", DISK_IMAGE_FS="/var/lib/x;rm"), "DISK_IMAGE_FS 경로 형식이 틀림: '/var/lib/x;rm'"),
    ((), dict(DISK_TEST_IMG_PCT="50", DISK_IMAGE_FS="var/lib"), "DISK_IMAGE_FS 경로 형식이 틀림: 'var/lib'"),
])
def test_bad_values_stop(args, env, msg):
    code, out = disk(*args, **env)
    assert code == 1 and f"disk_check: {msg}" in out, out
    assert "이번에" not in out  # 판단까지 가지 않는다


def test_partial_test_values_rejected():
    # 시험 값과 실측을 섞지 않는다 (예: 크기만 시험 값, 사용량은 실측)
    code, out = run(DISK, DISK_TEST_IMG_SIZE_GB="30", DISK_GC_QUERY="0")
    assert code == 1
    assert ("disk_check: 시험 값(DISK_TEST_IMG_PCT·DISK_TEST_IMG_SIZE_GB·DISK_TEST_VM_AVAIL_GB·DISK_TEST_HOST_AVAIL_GB)"
            "은 넷을 함께 줘야 한다 (1개만 있음)") in out
    code, out = run(DISK, DISK_TEST_IMG_PCT="50", DISK_TEST_IMG_SIZE_GB="30", DISK_TEST_VM_AVAIL_GB="9", DISK_GC_QUERY="0")
    assert code == 1 and "(3개만 있음)" in out


def test_without_test_values_needs_colima():
    # 시험 값이 없으면 실제로 colima를 읽어야 하고, 못 읽으면 그 원인을 말하고 멈춘다 (이 PC에는 colima가 없다)
    code, out = run(DISK, DISK_GC_QUERY="0")
    assert code == 1
    assert "disk_check: colima VM에 접속하지 못함 (꺼져 있나? 'colima list'로 확인) — 멈춤" in out
    assert "이번에" not in out


def _unset_vars(line):
    return set(re.findall(r"DISK_[A-Z_]+", line))


def test_deploy_clears_every_disk_check_variable():
    # disk_check.sh가 읽는 환경 변수 전부를 배포의 두 곳(맥 deploy.sh, Windows deploy.ps1의 사전 확인)이 지운다
    script = (ROOT / DISK).read_text(encoding="utf-8")
    read = set(re.findall(r"\$\{(DISK_[A-Z_]+)", script)) | set(re.findall(r"for v in ([A-Z_ ]+);", script)[0].split())
    assert {"DISK_TEST_IMG_PCT", "DISK_GC_MARGIN_PCT", "DISK_IMAGE_FS", "DISK_MIN_VM_GB", "DISK_MIN_HOST_GB", "DISK_GC_QUERY"} <= read
    sh = next(line for line in (ROOT / "scripts/mac/deploy.sh").read_text(encoding="utf-8").splitlines() if line.startswith("unset DISK_"))
    ps = next(line for line in (ROOT / "scripts/deploy.ps1").read_text(encoding="utf-8").splitlines() if "unset DISK_" in line)
    assert read <= _unset_vars(sh), read - _unset_vars(sh)
    assert read <= _unset_vars(ps), read - _unset_vars(ps)
    assert "disk_check.sh" in ps.split("unset", 1)[1]  # 같은 원격 명령에서 지우고 부른다


def test_deploy_checks_images_before_each_restart_and_build_pins():
    text = (ROOT / "scripts/mac/deploy.sh").read_text(encoding="utf-8")
    lines = text.splitlines()
    for marker, img, what in (("rollout restart deploy/docflow", "docflow:dev", "api"),
                              ("rollout restart deploy/web", "docflow-web:dev", "web")):
        i = next(n for n, line in enumerate(lines) if marker in line and not line.lstrip().startswith("#"))
        before = [line for line in lines[max(0, i - 3):i] if line.startswith("ensure_image")]
        assert before and before[-1].startswith(f"ensure_image {img} {what} "), marker
    assert 'build_image.sh "$2" rebuild' in text  # 다시 빌드는 rebuild 경로(이미지 하나 크기로 디스크 확인)
    assert "sh ~/doc_platform/scripts/build_image.sh all deploy" in text  # 배포 빌드는 고정 실패면 멈춤(apply 전)
    assert 'pin_image "$1" "$(image_id "$1")" strict ||' in text  # 재시작 전 고정 안 됨 → 다시 고정, 안 되면 멈춤
    assert not any(line.startswith("sh ~/doc_platform/scripts/disk_check.sh") for line in lines)  # 중복 확인 없음
    build = (ROOT / "scripts/mac/build_image.sh").read_text(encoding="utf-8")
    assert 'pin_image docflow:dev "$old_api" $PIN_MODE' in build and 'pin_image docflow-web:dev "$old_web" $PIN_MODE' in build
    assert 'PIN_MODE=""; [ -z "$MODE" ] || PIN_MODE=strict' in build  # deploy·rebuild = strict, 손 빌드 = 경고만
    assert 'size=450M; [ "$WHAT" != api ] || size=1400M' in build and 'disk_check.sh "$size" rebuild' in build
    assert "build_image.sh $WHAT rebuild" in build  # 막히면 정리 뒤 다시 빌드 안내
    assert "disk_check.sh 2 " in build


# ---- pin_image.sh: 가짜 colima로 분기 ----
PIN = "PIN_SLEEP=0; . scripts/mac/pin_image.sh; "


def test_pin_waits_until_cri_sees_image():
    code, out = run_fake('echo 3 > "$FAKE/hidden"; ' + PIN + 'pin_image docflow:dev "" strict; echo "rc=$?"; '
                         'grep -c "crictl inspecti -o" "$FAKE/calls"; head -1 "$FAKE/calls"')
    assert code == 0, out
    assert "== 이미지 고정: docflow:dev new (pinned: true — kubelet 이미지 GC 대상 아님)" in out and "rc=0" in out
    assert "첫 시도 실패" not in out
    assert "\n4\n" in out  # 안 보인 3번 + 보인 1번
    assert "ctr -n k8s.io images label docker.io/library/docflow:dev io.cri-containerd.pinned=pinned" in out  # 라벨이 먼저
    assert "== 옛 판 확인 생략: docflow:dev 빌드 전 ID 없음" in out and "image_list.sh '고정된 이미지'" in out


def test_pin_second_try_then_success():
    code, out = run_fake('echo 15 > "$FAKE/hidden"; ' + PIN + 'pin_image docflow:dev "" strict; echo "rc=$?"')
    assert "!! 이미지 고정: docflow:dev 첫 시도 실패 — 한 번 더" in out
    assert "== 이미지 고정: docflow:dev new" in out and "rc=0" in out


@pytest.mark.parametrize("state", ["label_fail", "never_pinned", "hidden_forever"])
def test_pin_failure_stops_deploy_path_and_warns_manual(state):
    setup = 'echo 99 > "$FAKE/hidden"; ' if state == "hidden_forever" else f'touch "$FAKE/{state}"; '
    code, out = run_fake(setup + PIN + 'pin_image docflow:dev "" strict; echo "rc=$?"')
    assert "!! 이미지 고정 실패: docflow:dev — 두 번 시도해도 pinned: true가 아님. 배포 경로라 멈춘다 (롤아웃하지 않음)" in out
    assert "rc=1" in out and "== 이미지 고정:" not in out
    code, out = run_fake(setup + PIN + 'pin_image docflow:dev ""; echo "rc=$?"')
    assert "!! 이미지 고정 실패: docflow:dev — GC에서 빠지지 않는다 (수동 빌드라 경고만" in out and "rc=0" in out


def test_pin_checks_old_image_released():
    code, out = run_fake('touch "$FAKE/old_exists" "$FAKE/old_pinned"; ' + PIN
                         + 'pin_image docflow:dev sha256:old strict; echo "rc=$?"')
    assert "!! 옛 판 고정이 남음: docflow:dev old — GC되지 않아 디스크를 잡는다" in out and "rc=0" in out
    code, out = run_fake('touch "$FAKE/old_exists"; ' + PIN + 'pin_image docflow:dev sha256:old strict; echo "rc=$?"')
    assert "== 옛 판 고정 없음: docflow:dev old (안 쓰이면 GC 대상)" in out
    code, out = run_fake(PIN + 'pin_image docflow:dev sha256:new strict; echo "rc=$?"')  # 같은 ID(캐시 빌드) → 옛 판 확인 없음
    assert "옛 판" not in out and "rc=0" in out


def test_user_messages_use_full_issue_url():
    # 이슈 번호는 늘 전체 URL로 (issues/<번호>·#<번호> 줄임 금지)
    for f in (DISK, "scripts/mac/deploy.sh", "scripts/mac/build_image.sh", "scripts/mac/pin_image.sh", LIST):
        text = (ROOT / f).read_text(encoding="utf-8")
        for m in re.finditer(r"issues/\d+", text):
            assert text[:m.start()].endswith("https://github.com/reddol18/docflow/"), (f, text[max(0, m.start() - 40):m.end()])
        assert not re.search(r"(?<![\w&])#\d{2,}\b", text), f


WORKLOADS = "\n".join([
    "docs\tdocflow:dev ",
    "docs\tdocflow-web:dev docflow-web:dev ",
    "docs\tpostgres:17.11-alpine@sha256:" + "a" * 64 + " ",
    "bank-a\tbank-api:1.2 ",
    "sllm\tollama/ollama:0.5.0 ",
    "kube-system\trancher/mirrored-coredns-coredns:1.12.0 ",
    "gateway\tghcr.io/acme/docflow:1.0 ",
])
IMAGES = "\n".join([
    "docflow dev 1111111111111111 2.0GB",
    "<none> <none> 1111111111111111 2.0GB",               # 같은 ID의 sha256 ref
    "docflow <none> 2222222222222222 1.9GB",              # 예전 빌드 (태그 빠짐)
    "docflow-web dev 3333333333333333 400MB",
    "postgres 17.11-alpine 4444444444444444 280MB",
    "postgres <none> 4444444444444444 280MB",             # 같은 ID의 다이제스트 행
    "<none> <none> 4444444444444444 280MB",
    "docker.io/library/bank-api 1.2 5555555555555555 300MB",
    "ollama/ollama 0.5.0 6666666666666666 3.4GB",         # 0개로 줄였어도 정의가 있어 '쓰는 곳'
    "some/tool 1.0 7777777777777777 50MB",                # 주인 모름
    "registry.k8s.io/pause 3.10.2 8888888888888888 700kB",
    "rancher/mirrored-coredns-coredns 1.12.0 9999999999999999 70MB",
    "docflow old 0000000000000000 1.8GB",
    "bank-api <none> aaaaaaaaaaaaaaaa 290MB",             # 다른 프로젝트의 예전 빌드
    "postgres 16-alpine bbbbbbbbbbbbbbbb 270MB",          # docs의 예전 판
    "<none> <none> cccccccccccccccc 2.0GB",               # 이름 없음
    "ghcr.io/acme/docflow 0.9 dddddddddddddddd 1GB",      # 이름 끝만 같은 다른 레지스트리 이미지 (gateway 것)
    "my/pause 1 eeeeeeeeeeeeeeee 1MB",                    # pause가 이름에 들었을 뿐
])


def test_image_list_one_verdict_per_image_id():
    code, out = run(LIST, IMAGE_LIST_TEST_WORKLOADS=WORKLOADS, IMAGE_LIST_TEST_IMAGES=IMAGES)
    assert code == 0, out
    body = [line for line in out.splitlines() if re.match(r"^[0-9a-f]{12} ", line)]
    ids = [line.split()[0] for line in body]
    assert len(ids) == len(set(ids)) == 15  # ID마다 한 줄 (태그·다이제스트·<none> 행을 묶음)
    rows = dict(zip(ids, body))

    def v(image_id):
        return rows[image_id[:12]]

    assert "docflow:dev" in v("1111111111111111") and v("1111111111111111").endswith("docs 사용 → 지우지 않음")
    assert "docflow:<none>" in v("2222222222222222")
    assert v("2222222222222222").endswith("태그 없음(dangling), docs 이미지의 예전 판 → 후보 (사용자 판단)")
    assert v("3333333333333333").endswith("docs 사용 → 지우지 않음")
    assert "postgres:17.11-alpine" in v("4444444444444444") and v("4444444444444444").endswith("docs 사용 → 지우지 않음")
    assert v("5555555555555555").endswith("bank-a 사용 → 다른 프로젝트·시스템 것, 대상 아님")
    assert v("6666666666666666").endswith("sllm 사용 → 다른 프로젝트·시스템 것, 대상 아님")
    assert v("7777777777777777").endswith("워크로드 없음, 주인 모름 → 다른 프로젝트 것일 수 있음. 주인 확인 전엔 지우지 않음")
    assert v("8888888888888888").endswith("시스템(pause) → 대상 아님")
    assert v("9999999999999999").endswith("시스템(kube-system) → 대상 아님")
    assert v("0000000000000000").endswith("워크로드 없음, docs 이미지의 예전 판 → 후보 (사용자 판단)")
    assert v("bbbbbbbbbbbbbbbb").endswith("워크로드 없음, docs 이미지의 예전 판 → 후보 (사용자 판단)")
    assert v("aaaaaaaaaaaaaaaa").endswith("태그 없음(dangling), 같은 이름을 bank-a가 씀 → 다른 프로젝트·시스템 것, 대상 아님")
    assert v("cccccccccccccccc").endswith("이름 없음(dangling), 주인 모름 → 크기·만든 때를 보고 사용자 판단 (다른 프로젝트 것일 수 있음)")
    assert v("dddddddddddddddd").endswith("워크로드 없음, 같은 이름을 gateway가 씀 → 다른 프로젝트·시스템 것, 대상 아님")
    assert v("eeeeeeeeeeeeeeee").endswith("워크로드 없음, 주인 모름 → 다른 프로젝트 것일 수 있음. 주인 확인 전엔 지우지 않음")
    # 다른 프로젝트·시스템·주인 모름은 어느 것도 '후보'가 아니다
    for i in ("5555555555555555", "6666666666666666", "7777777777777777", "8888888888888888", "9999999999999999",
              "aaaaaaaaaaaaaaaa", "cccccccccccccccc", "dddddddddddddddd", "eeeeeeeeeeeeeeee"):
        assert "후보" not in v(i), v(i)


def test_image_list_has_no_delete_command():
    text = (ROOT / LIST).read_text(encoding="utf-8")
    code = "\n".join(line for line in text.splitlines() if not line.lstrip().startswith("#") and "echo" not in line)
    for bad in (" rmi", "image rm", "image prune", "builder prune", "buildctl prune", "system prune", "images rm", "label",
                "kubectl delete", "kubectl scale"):
        assert bad not in code, bad
