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

GPU는 쓰지 않는다 (2026-11-21 결정, CPU만). 모델 3개는 묶음에 들어 있고 설치 노드의 `/var/lib/docflow/ollama`에 풀린다(읽기 전용 마운트).

## 1. 묶음 만들기 (인터넷 되는 곳)

필요한 것: 리눅스(이 PC는 WSL 배포판 `docflow81`, 디스크 F:), root, docker, k3s(같은 판), git, zstd, jq, python3.
Ollama 모델 폴더(이 PC `E:\dev\models`)는 읽기만 한다.

```sh
sudo bash scripts/offline/build_bundle.sh <저장소> /mnt/e/dev/models /mnt/f/docflow81/bundle
```

- 시작 전에 묶음 폴더 여유 30GB·`/` 여유 25GB를 확인하고, 모자라면 멈춘다.
- 앱 이미지는 지금 커밋을 `git archive`로 꺼내 빌드한다 (커밋 안 된 변경은 들어가지 않는다).
- 레지스트리 이미지(postgres·qdrant·redis·ollama)는 매니페스트의 다이제스트 그대로 `k3s ctr pull → export`로 넣는다.
  docker save로 넣으면 다이제스트가 바뀌어 kubelet이 받으러 나간다.
- 결과: `<묶음 폴더>/docflow-offline-<커밋>/`. 내용은 `CONTENTS.txt`, 해시는 `SHA256SUMS`.
- 스크립트·매니페스트만 고쳤으면 `build_bundle.sh --refresh <저장소> <있는 묶음 폴더>`로 그 부분만 지금 커밋으로 바꾼다.
  - 이미지·모델은 그대로 두고 해시를 다시 만든다.
  - 이미지에 드는 파일(app·Dockerfile·frontend·requirements·이미지 다이제스트)이 바뀌었으면 거절한다. 그때는 처음부터 다시 만든다.

## 2. 설치 (폐쇄망)

`/` 여유 45GB 이상. 묶음 폴더를 설치 노드에 옮긴 뒤:

```sh
sudo bash <묶음>/scripts/install.sh --tools-only         # 묶음의 .deb로 iptables·conntrack·tcpdump만 먼저 (받기 없음)
sudo bash <묶음>/scripts/egress_guard.sh on              # 시험할 때: 노드 바깥 송신을 막고 센다 (현장에서는 망이 이미 막혀 있다)
sudo REQUIRE_EGRESS_GUARD=1 bash <묶음>/scripts/install.sh
```

`install.sh` 순서:

1. 묶음 해시 확인
2. 없는 도구만 `.deb` 넣기
3. k3s airgap 설치 — 이미지 tar를 `agent/images`에 둔다. cgroup v1 커널이면 `fail-cgroupv1=false`
4. CoreDNS Corefile을 바꾼다 (`coredns.yaml.skip`으로 k3s가 되돌리지 않게)
5. 모델 풀기
6. 내부 CA·서버 인증서·비밀값 만들기
7. 매니페스트 적용·롤아웃
8. 첫 관리자 만들기 — 비밀번호는 `/root/docflow-admin-password`, 0600
9. 이미지 받기 시도가 없었는지 확인

접속:

- API: `k3s kubectl -n docs port-forward svc/docflow 8000:8000`
- 화면: `svc/web 8443:8443`, 주소는 `https://docflow.localhost:8443`. 내부 CA `/root/docflow-ca/ca.crt`를 신뢰해야 한다.

## 3. 확인 시험과 외부 송신 0 증거

```sh
E=<증거 폴더>; S=<묶음>/scripts
sudo bash $S/evidence.sh snapshot $E before      # (a) 정책·CoreDNS·LLM 설정 (b) 카운터 (c) conntrack
sudo bash $S/evidence.sh capture-start $E        # (d) tcpdump: cni0(파드가 보내려 한 것) · eth0(실제로 나간 것)
sudo bash $S/smoke_test.sh <쪽 이미지 폴더> $E "<검색어>"   # 올리기 → 실행(review=auto) → 검색
sudo bash $S/evidence.sh capture-stop $E
sudo bash $S/evidence.sh snapshot $E after
sudo bash $S/evidence.sh probe $E                # (e) 대조: 일부러 바깥에 나가 보고 막히는지·카운터가 오르는지
```

증거별로 0이어야 하는 것:

| 증거 | 0이어야 하는 것 |
|---|---|
| (b) `egress_guard.sh status` | 시험 동안 `pod-egress`·`node-egress` 패킷 |
| (c) `c_conntrack_external.txt` | 클러스터·루프백 밖 목적지 |
| (d) `cni0.count`·`eth0.count` | 패킷 |
| (e) `e_probe.txt` | 바깥 연결이 모두 실패하고, 정책 없는 네임스페이스 파드의 시도만 `pod-egress` 카운터를 올린다 (카운터가 실제로 센다는 증거) |

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

## 되돌리기

- k3s 전체: `/usr/local/bin/k3s-uninstall.sh`. 데이터(PVC)도 지워진다.
- 모델: `/var/lib/docflow/ollama`
- 내부 CA: `/root/docflow-ca`
- 바깥 송신 막기 풀기: `egress_guard.sh off`
