# docflow — 스캔 공문서 색인 플랫폼

1990년대 공문서 스캔 묶음(철)을 올리면 **문서별로 나누고 → 색인 4칸(문서번호·시행일자·수신·제목)을 뽑고 →
자신 없는 칸만 사람에게 넘기고 → 검색**까지 하는 프로토타입입니다.
기록물 디지털화 현장에서 사람이 쪽마다 손으로 치던 색인 입력을 "검토 필요" 표시가 붙은 칸 확인으로 바꾸는 것이 목표입니다.

- OCR: **PaddleOCR**(한국어, CPU, 로컬) + 손글씨 칸만 **GPT 비전**으로 재판독
- 흐름: **LangGraph** — 문서별 병렬 하위 그래프, 칸 위치 재탐색 순환, `interrupt()`로 사람 검토 후 재개, Postgres 체크포인트
- 검색: **Qdrant**(벡터) + Postgres(낱말) → RRF로 합침, 근거 문서를 단 GPT 답변
- 실행: **Kubernetes**(k3s) 위 FastAPI 서비스, 대시보드는 **Next.js**

> 화면 캡처의 원본 쪽 이미지는 실명·직인 보호를 위해 흐리게 처리했습니다. 데이터: AI Hub 「공공행정문서 OCR」 샘플(김해시 인·허가 7개 철 680쪽, 1994~2001).

## 화면

| 대시보드 | 진행 단계 (LangGraph) |
|---|---|
| ![대시보드](docs/screens/dashboard.png) | ![진행 단계](docs/screens/progress.png) |

| 결과 검토·수정 | 검색 |
|---|---|
| ![에디터](docs/screens/editor.png) | ![검색](docs/screens/search.png) |

## 구조

```
Next.js 대시보드 ──/api/*──▶ docflow (FastAPI + LangGraph + PaddleOCR)   ── k8s namespace: docs
                                ├─ Postgres  문서 색인 · 비용 장부 · 노드 이벤트 · LangGraph 체크포인트
                                ├─ Qdrant    문서 벡터 (text-embedding-3-small, 연도 필터)
                                └─ OpenAI    4칸 추출 · 손글씨 칸 판독 (gpt-4.1-mini), 임베딩
네트워크: 기본 차단, docflow → Postgres/Qdrant/OpenAI(443)만 허용
```

### LangGraph 흐름

```
철:   ocr → split ─(문서마다 Send, 병렬 4)→ doc ×N → validate ─(검토 필요 & human)→ review [interrupt] → index
                                                           └(없음 또는 auto)────────────────────→ index
문서: extract → locate ─(칸 못 찾음)→ widen → locate …
                      └→ read → merge
```

| 노드 | 하는 일 |
|---|---|
| ocr | 쪽 이미지 → 단어 상자·줄 텍스트. 쪽마다 결과를 저장해 재실행 시 건너뜀 |
| split | 규칙으로 문서 시작 쪽 판별 (머리부 기관명·우편번호 + 문서번호/시행일자) |
| extract | 첫 쪽 텍스트 → GPT가 4칸 + 확신도. OCR 오독은 문맥으로 교정, 수신은 규칙 보정 |
| locate / widen | 인쇄된 '문서번호'·'시행일자' 옆 손글씨 칸 위치. 못 찾으면 머리부 범위 40→48→56% |
| read | 손글씨 칸 이미지를 GPT 비전으로 판독. 확신도 = 토큰 확률 기하평균(logprobs) |
| validate | 검토 필요 표시: 형식·범위·흐름(같은 철 앞뒤 문서와 비교)·확신도 |
| review | `interrupt()`로 멈춤 → 대시보드에서 고친 값이 `Command(resume=…)`로 들어오면 이어 감 |
| index | Postgres upsert + 임베딩 → Qdrant |

**왜 LangGraph인가:** 사람 검토에서 몇 시간 멈췄다 이어 가야 하고(체크포인트), 문서마다 병렬로 돌리고(Send + reducer),
칸을 못 찾으면 다시 찾는 순환이 있고, 어떤 경로를 지났는지가 남아야 자동 처리율을 잴 수 있기 때문입니다.

## 측정 결과

7개 철 680쪽 → 문서 450건. 사람이 만든 정답 164건(문서 시작 쪽 86 + 색인 칸)으로 채점 (`eval/score.py`).

| 항목 | docflow | 참고: 이전 PoC |
|---|---|---|
| 문서 시작 판별 recall / precision | **99% / 100%** | 99% / 100% |
| 문서번호 일련번호 (손글씨) | 65% | 81% ¹ |
| 시행일자 (손글씨) | 79% | 87% ¹ |
| 수신 | 46% | 77% |
| 제목 | 88% | 93% |
| 4칸 모두 정답 | 25% | 59% |
| 검토 표시가 붙은 문서 | 80% | 87% |
| **표시 없이 틀린 문서** (사람이 놓치는 것) | **10%** | 1% |

¹ PoC는 같은 데이터로 학습된 손글씨 모델(ko-trocr)이라 높게 나왔을 수 있어 참고만. 데이터 누출이 없는 Claude 비전 기준은 78% / 88%.

- **GPT 비용:** 680쪽 한 번에 약 $0.35 (추출 $0.23, 손글씨 칸 $0.12, 임베딩 $0.003). OCR은 로컬 CPU라 0 — 쪽당 7~11초.
- **모델은 재서 골랐다:** 손글씨 칸 20개로 6가지 설정, 텍스트 추출은 정답 22건으로 4가지를 비교 → 둘 다 gpt-4.1-mini (`docs/design.md` 5절).
- **확신도만으로는 부족했다:** 비전 판독은 틀려도 확신도가 0.9 이상인 경우가 많았다. 텍스트 추출 값과 손글씨 판독 값이 다르면 표시('불일치')하도록 바꿔
  틀린 일련번호의 98%가 검토로 넘어간다.
- **약점 — 수신:** 1990년대 표 양식이라 OCR 줄에 수신 칸과 결재란('국장', '과장')이 섞이고, 텍스트만 보는 GPT가 가려내지 못한다.
  다음 단계는 머리부 이미지를 비전으로 함께 보게 하는 것.

## 실행

```bash
# 백엔드 (k8s)
kubectl apply -f k8s/docs.yaml
sh scripts/mac/make_secrets.sh < openai_key      # 키는 표준 입력으로만, 화면에 찍지 않음
sh scripts/mac/build_image.sh                    # PaddleOCR 모델을 이미지에 포함 (2.0GB)
kubectl port-forward -n docs svc/docflow 8000:8000

# 대시보드
cd frontend && npm install && npm run dev        # http://localhost:3000  (DOCFLOW_URL 기본 http://127.0.0.1:8000)
```

## 폴더

| | |
|---|---|
| `app/` | FastAPI · LangGraph 흐름(`graph.py`) · OCR · 규칙(`layout.py`) · GPT 호출(`llm.py`) · 저장소 |
| `frontend/` | Next.js 16 대시보드 (업로드 · 진행 단계 · 검토 에디터 · 검색) |
| `k8s/` | 네임스페이스 · 네트워크 정책 · Postgres · Qdrant · docflow |
| `eval/` | 정답 채점(`score.py`), 모델·지시문 비교(`cell_variants.py`, `extract_variants.py`). 정답 데이터와 채점 규칙은 이전 PoC 폴더에 있어 이 레포만으로는 돌지 않음 |
| `docs/` | 설계(`design.md`), 실습 기록(`lab_log.md`) |
