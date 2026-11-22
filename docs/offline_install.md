# 폐쇄망 설치 (오프라인 묶음)

https://github.com/reddol18/docflow/issues/81. 인터넷이 없는 현장에 묶음 하나로 k3s 단일 노드 + docflow + 로컬 LLM(Ollama)을 설치하고,
바깥으로 아무것도 보내지 않는 상태에서 올리기 → 색인 → 검색이 도는지 확인하는 절차다.

## 구성

| 무엇 | 폐쇄망 값 | 어디서 정하나 |
|---|---|---|
| LLM 제공자 | `LLM_PROVIDER=local` → `http://ollama.llm.svc.cluster.local:11434/v1` | `k8s/offline/kustomization.yaml` |
| 추출·답변 | `qwen2.5:7b` | 〃 |
| 손글씨 칸 | `qwen2.5vl:7b` | 〃 |
| 임베딩 | `bge-m3`, `EMBED_DIM=1024` → Qdrant 모음 `documents_bge-m3_1024` | 〃, `app/store.py` `embed_config` |
| 시간 제한 | `LLM_TIMEOUT_SECONDS=900` (GPU 없이 CPU) | 〃, `app/llm.py` `local_timeout` |
| 로그인 | `AUTH_MODES=local` (Keycloak·OIDC 없음) | 〃 |
| 바깥 송신 | `docflow-egress`의 공인 IP 443 규칙 삭제 → Ollama 11434만 | 〃 |
| Ollama | `llm` 네임스페이스, egress 전부 차단, docs의 API·워커에서 11434만 받음 | `k8s/offline/llm.yaml` |
| CoreDNS | 클러스터 바깥 이름을 위쪽 DNS로 넘기지 않음 (`forward` 삭제) | `k8s/offline/Corefile` |

GPU는 쓰지 않는다 (2026-11-21 결정, CPU만). 모델 3개는 묶음에 들어 있고 설치 노드의 `/var/lib/docflow/ollama`에 풀린다.
모델 폴더는 읽기 전용으로 마운트한다. Ollama 0.40이 쓰는 `manifests-v2`·`metadata`만 emptyDir로 덮는다 (`k8s/offline/llm.yaml`).

## 1. 묶음 만들기 (인터넷 되는 곳)

필요한 것: 리눅스(이 PC는 WSL 배포판 `docflow81`, 디스크 F:), root, docker + buildx, k3s(같은 판), git, zstd, jq, python3.
Ollama 모델 폴더(이 PC `E:\dev\models`)는 읽기만 한다.

```sh
sudo bash scripts/offline/build_bundle.sh <저장소> /mnt/e/dev/models /mnt/f/docflow81/bundle
```

- 시작 전에 묶음 폴더 여유 30GB·`/` 여유 25GB를 확인하고, 모자라면 멈춘다.
- 앱 이미지는 지금 커밋을 `git archive`로 꺼내 빌드한다 (커밋 안 된 변경은 들어가지 않는다).
  - OCR 모델(PaddleOCR, 이미지 안 `/opt/paddlex`)은 운영 이미지와 **같은 목록** `app/models.lock`(파일 목록·sha256)을 쓴다 (https://github.com/reddol18/docflow/issues/102).
    묶음은 OCR 모델을 따로 싣지 않는다. 같은 `Dockerfile` 모델 층이 받은 뒤 대조하고, 하나라도 다르면(빠짐·남음·해시) 빌드가 멈춰 묶음도 만들어지지 않는다.
  - 빌드 출력에 `모델 일치: OCR 모델 파일 N개 = app/models.lock`(캐시면 `모델 층 캐시 사용`), `CONTENTS.txt`에 그 lock의 파일 수·sha256을 적는다.
  - 모델 판을 올리면 `.\scripts\model_lock_refresh.ps1`로 lock을 다시 만들어 커밋한 뒤 묶음을 처음부터 다시 만든다 (`app/` 변경이라 `--refresh`는 거절한다).
- 레지스트리 이미지(postgres·qdrant·redis·ollama)는 매니페스트의 다이제스트 그대로 `k3s ctr pull → export`로 넣는다.
  docker save로 넣으면 다이제스트가 바뀌어 kubelet이 받으러 나간다.
- k3s 설치 스크립트는 그 판의 태그에서 받아(`raw.githubusercontent.com/k3s-io/k3s/<판>/install.sh`) 고정 해시와 견준다. 판을 바꾸면 해시도 바꾼다.
- 이미지 취약점: 묶음을 다 만든 뒤 **묶음 안 tar 그대로** trivy로 스캔한다 (`scripts/offline/scan_bundle.sh`).
  - 대상: `images/*.tar.zst`(앱 이미지 docflow·docflow-web, ollama·postgres·qdrant·redis)와 `k3s/k3s-airgap-images-amd64.tar.zst`(k3s 시스템 이미지).
  - 레지스트리에서 태그로 다시 받지 않는다. 묶음에 든 것과 다를 수 있어서다.
  - tar를 따로 만든 containerd 네임스페이스에 잠깐 넣어 이미지 하나씩 나눈 뒤 `trivy image --input`으로 본다. 끝나면 지운다.
  - trivy는 `scripts/tool_pins.txt` linux-x86_64 줄로 확인한다.
  - 결과는 묶음 밖에 남는다: 요약 `<묶음 폴더>/trivy-<커밋>.tsv`(HIGH·CRITICAL·그중 고친 판 있음), JSON `/root/docflow-trivy-<커밋>/`.
  - HIGH·CRITICAL이 있어도 멈추지 않고 수를 적는다.
  - 있는 묶음만 다시 볼 때: `sudo bash scripts/offline/build_bundle.sh --scan <저장소> <묶음>` (또는 `scan_bundle.sh <묶음> <저장소> [결과 폴더]`)
  - 스캔이 실패하면(trivy 해시 불일치·디스크 모자람·이미지 스캔 실패·요약 없음) 묶음은 남기고 `build_bundle.sh`는 3으로 끝난다.
  - 층은 풀지 않고(`--no-unpack`) 넣고, 시작 전에 디스크 여유를 어림해 모자라면 멈춘다. 끝나면 이미지·내용·스냅숏·임시 tar가 남지 않았는지 적는다.
- 결과: `<묶음 폴더>/docflow-offline-<커밋>/`. 내용은 `CONTENTS.txt`, 해시는 `SHA256SUMS`.
  - `sha256sum <묶음>/SHA256SUMS` 값을 묶음과 **다른 길**(문서·별도 매체)로 현장에 전한다. 아래 '묶음 확인'에서 쓴다.
- 스크립트·매니페스트만 고쳤으면 `build_bundle.sh --refresh <저장소> <있는 묶음 폴더>`로 그 부분만 지금 커밋으로 바꾼다.
  - 이미지·모델은 그대로 두고 해시를 다시 만든다.
  - 이미지에 드는 파일(app·Dockerfile·frontend·requirements·이미지 다이제스트)이 바뀌었으면 거절한다. 그때는 처음부터 다시 만든다.

## 2. 설치 (폐쇄망)

`/` 여유 45GB 이상. 묶음 폴더를 설치 노드에 옮긴 뒤:

```sh
sudo OS_QUIET=1 bash <묶음>/scripts/install.sh --tools-only   # 묶음의 .deb로 iptables·conntrack·tcpdump만 먼저 (받기 없음)
                                                         # OS_QUIET=1: OS가 스스로 바깥에 묻는 서비스(timesyncd·apt·motd 타이머·snapd)를 끔
sudo bash <묶음>/scripts/egress_guard.sh on              # 시험할 때: 노드 바깥 송신을 막고 센다 (현장에서는 망이 이미 막혀 있다)
sudo SUMS_SHA256=<따로 받은 값> REQUIRE_EGRESS_GUARD=1 bash <묶음>/scripts/install.sh
```

### 묶음 확인

- `SUMS_SHA256=<따로 받은 값>`은 **필수**다. 설치 전에 `SHA256SUMS` 자체의 해시를 견주고, 다르면 멈춘다.
  묶음 안의 `SHA256SUMS`까지 함께 바꿔치기한 경우를 막는다.
  - 값이 없으면 멈춘다. 따로 받은 값이 없는데 진행해야 하면 `ALLOW_NO_SUMS=1`을 명시한다 (해시를 출력하니 손으로 견준다).
- 묶음에 심볼릭 링크가 하나라도 있으면 멈춘다. 목록에 있는 이름이라도 링크면 묶음 밖 파일을 가리킬 수 있다.
- `SHA256SUMS`에 없는 것(파일·장치·FIFO 등 폴더가 아닌 모든 것)이 묶음에 있으면 멈춘다.
  `.deb`·이미지 tar는 폴더째 읽으므로, 끼워 넣은 파일이 root로 설치되지 않게 한다.
- 옮긴 묶음을 미리 확인만 하려면 `install.sh --verify-only` (1단계까지만, root·디스크 확인 없음 — 읽기만 한다).

```sh
sudo SUMS_SHA256=<값> REQUIRE_EGRESS_GUARD=1 bash <묶음>/scripts/install.sh
```

`install.sh` 순서:

1. 묶음 확인 (위)
2. 없는 도구만 `.deb` 넣기
3. k3s airgap 설치 — 이미지 tar를 `agent/images`에 둔다. cgroup v1 커널이면 `fail-cgroupv1=false`.
   kubeconfig(`/etc/rancher/k3s/k3s.yaml`, cluster-admin)는 k3s 기본 0600이라 root만 읽는다
   kubelet `runtime-request-timeout=10m` — 큰 이미지(ollama)의 첫 컨테이너 만들기가 기본 2분을 넘어 kubelet이 한 번 더 만들던 것(restartCount 1)을 막는다
4. CoreDNS Corefile을 바꾼다 (`coredns.yaml.skip`으로 k3s가 되돌리지 않게)
5. 모델 풀기
6. 내부 CA·서버 인증서·비밀값 만들기
7. 매니페스트 적용·롤아웃
8. 첫 관리자 만들기 — 비밀번호는 `/root/docflow-admin-password`, 0600
9. 이미지 받기 시도가 없었는지 확인

접속:

- API: `sudo k3s kubectl -n docs port-forward svc/docflow 8000:8000` (kubeconfig가 0600이라 root로)
- 화면: `svc/web 8443:8443`, 주소는 `https://docflow.localhost:8443`. 내부 CA `/root/docflow-ca/ca.crt`를 신뢰해야 한다.

## 3. 확인 시험과 외부 송신 0 증거

```sh
E=<증거 폴더>; S=<묶음>/scripts
sudo bash $S/evidence.sh capture-start $E        # (d) tcpdump: 바깥 인터페이스(실제로 나간 것)·nflog(버린 것). 설치 전에
# … 설치 …
sudo bash $S/evidence.sh capture-start $E        # cni0(파드가 보내려 한 것) — k3s가 뜬 뒤 한 번 더
sudo bash $S/evidence.sh snapshot $E before      # (a) 정책·CoreDNS·LLM 설정 (b) 카운터 (c) conntrack
sudo bash $S/smoke_test.sh <쪽 이미지 폴더> $E "<검색어>" <기대 문서 id>   # 올리기 → 실행(review=auto) → 검색, 1위가 기대 문서면 PASS
sudo bash $S/evidence.sh snapshot $E after
sudo bash $S/evidence.sh capture-stop $E
sudo bash $S/evidence.sh ollama-log $E           # Ollama 로그·재시작 까닭, ollama.com 언급 줄 수
sudo bash $S/evidence.sh probe $E                # (e) 대조: 일부러 바깥에 나가 보고 막히는지·카운터가 오르는지
```

- 바깥 인터페이스는 `EGRESS_IF`, 없으면 IPv4 기본 경로의 인터페이스다 (`egress_guard.sh`와 같은 방법).
- tcpdump가 뜨지 못하거나 pcap을 읽지 못하면 '0개'로 적지 않고 오류로 끝난다.
- pcap은 머리만 담는다(`-s 128`). 버린 DNS만 512바이트(질의 이름). 증거 폴더는 0700이다. 단 WSL의 `/mnt/<드라이브>`(drvfs)는 chmod가 듣지 않고 Windows 권한을 따르므로, 증거는 리눅스 쪽 폴더에 둔다.

증거별로 0이어야 하는 것:

| 증거 | 0이어야 하는 것 |
|---|---|
| (b) `egress_guard.sh status` | 시험 동안 `pod-egress`·`node-egress`·`v6-egress` 패킷. 바깥에서 시작된 연결의 응답(conntrack 방향 REPLY, 예: 들어온 ssh)만 `established` 규칙이 먼저 통과시켜 세지 않는다. 노드·파드가 연 연결은 guard를 켜기 전에 맺었더라도 막고 센다. 링크 로컬 멀티캐스트(224.0.0.0/24, ff02::/16)는 `link-local-mcast`로 따로 센다 |
| (c) `c_conntrack_external.txt` | 노드·파드가 연 연결 중 클러스터·루프백·링크 로컬 멀티캐스트 밖 목적지 |
| (d) `cni0.count`·`eth0.count`·`nflog.count`·`nflogdns.count` | 패킷. eth0은 나가는 방향만 세고 링크 로컬 멀티캐스트(ff02::/16, 224.0.0.0/24 — mDNS·ND처럼 같은 링크에서 끝남)는 뺀다 |
| Ollama `ollama.ollama_com.count` | ollama.com 접속 시도 줄(주소가 든 실패 로그). 시작 때 설정 출력의 `OLLAMA_REMOTES:[ollama.com]`은 시도가 아니다 |
| (e) `e_probe.txt` | 바깥 연결이 모두 실패하고, 정책 없는 네임스페이스 파드의 시도만 `pod-egress` 카운터를 올린다 (카운터가 실제로 센다는 증거) |

`node-egress`가 0이 아니면 노드의 어떤 프로세스가 바깥에 묻는 것이다.
무엇을 보내려 했는지는 `egress_guard.sh`가 버리기 전에 남긴 사본으로 본다 (`evidence.sh capture-start`가 받는다).

- `nflog.flows.txt`: 출발·목적지.
- `nflog.dns.txt`: DNS 질의 이름.
- 사본은 커널이 자른다. DNS(포트 53)는 512바이트까지(이름이 잘리지 않게, NFLOG 그룹 6), 그 밖은 128바이트까지(머리만, 그룹 5)다.
  guard 전에 열어 둔 연결로 보내려던 본문이 증거에 남지 않게 한다.

2026-11-21 시험에서는 Ubuntu 기본 `systemd-timesyncd`가 `ntp.ubuntu.com`을 물었다. 그래서 `OS_QUIET=1`을 둔다.

WSL에서 시험할 때: 배포판들은 네트워크를 함께 쓴다. `egress_guard.sh on` 동안에는 같은 VM의 다른 배포판도 바깥에 못 나간다.

## 임베딩 모델 바꾸기

- 모음 이름은 모델과 차원에서 정해진다.
  - 기본(text-embedding-3-small, 1536)은 지금 이름 `documents`다. 지금 배포는 설정을 바꾸지 않아도 같은 모음을 쓴다.
  - 그 밖의 모델은 `documents_<모델>_<차원>`이다.
- 기본 모델이 아니면 `EMBED_DIM`이 꼭 있어야 한다. 있는 모음의 차원이 다르면 시작할 때 멈춘다.
- 모델을 바꾸면 새 모음이 빈 채로 생기고, 예전 모음은 지우지 않는다(되돌리기용).
  - 검색은 새 모음만 보므로 철을 다시 돌려(`/volumes/<철>/run`) 채운다.
  - 되돌리려면 예전 `EMBED_MODEL`·`EMBED_DIM`으로 바꿔 다시 띄운다.
- 예전 모음이 더는 필요 없을 때만 Qdrant에서 지운다.
- 모델 이름은 정규화한다. 끝의 `:latest`는 떼므로 `bge-m3`와 `bge-m3:latest`는 같은 모음이다.
  - 새로 만든 모음에는 메타데이터 `embed_model`(정규화한 모델 이름)·`embed_dim`을 적는다.
  - 시작할 때 설정과 다르면 멈춘다. 모델 이름을 이름 조각으로 바꾸면 다른 모델이 같은 모음 이름이 될 수 있어서다.
  - 예전 운영 모음 `documents`는 메타데이터가 없어 차원만 본다.
- 기본 모델(text-embedding-3-small)은 `EMBED_DIM=1536`만 받는다. OpenAI에 `dimensions=`를 보내지 않으므로, 다른 값이면 벡터 길이가 맞지 않는다.
- 모델이 돌려준 벡터 길이가 `EMBED_DIM`과 다르면 임베딩을 부른 작업이 멈춘다. 색인은 그 실행이 오류로 끝나고, 검색은 오류가 난다.
- **local → off로 바꿀 때:** `EMBED_MODEL`·`EMBED_DIM`을 그대로 두고 `LLM_PROVIDER`만 off로 바꾼다.
  - off 모드는 다시 색인·수정한 문서의 벡터를 지금 모음에서만 지운다.
  - `EMBED_*`를 지우면 기본 모음 `documents`를 보게 되어, `documents_<모델>_<차원>`에 옛 벡터가 남는다.
  - 이 상태(off + `EMBED_*` 없음 + 기본 아닌 모음 있음)면 시작할 때 경고를 남긴다.

## 되돌리기

- k3s 전체: `/usr/local/bin/k3s-uninstall.sh`. 데이터(PVC)도 지워진다.
- 모델: `/var/lib/docflow/ollama`
- 내부 CA: `/root/docflow-ca`
- 바깥 송신 막기 풀기: `egress_guard.sh off`
