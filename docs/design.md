# docflow — 공문서 스캔 색인 플랫폼 (프로토타입)

1990년대 공문서 스캔 묶음(철)을 올리면 **문서별로 나누고, 색인 4칸(문서번호·시행일자·수신·제목)을 뽑고,
자신 없는 칸은 사람 검토로 넘긴 뒤, 검색(벡터 + 낱말)과 질문 답변까지** 하는 서비스.
records_poc 3일 PoC(`work/gt/day3_summary.md`)에서 스크립트로 이어 붙였던 단계를 LangGraph 흐름으로 옮겨
쿠버네티스(맥북 k3s) 위의 서비스로 만들었다.

## 1. 구성

```
Windows ─(ssh 터널 → kubectl port-forward)→ docs 네임스페이스 (k3s, 맥북 colima 4 CPU / 8GB)
                                             ├ docflow   FastAPI + LangGraph + PaddleOCR(CPU)   요청 1.5 CPU / 2Gi, 한도 5Gi
                                             ├ postgres  문서 색인 표 · 비용 장부 · LangGraph 체크포인트
                                             └ qdrant    문서 벡터 (text-embedding-3-small, 1536차원, 연도 필터)
                                   바깥으로 나가는 통신: docflow → OpenAI(443)만
```

- 네트워크: 기본 차단 + DNS. docflow → postgres/qdrant, docflow → 공인 IP 443만 연다. postgres/qdrant는 docflow에서만 받는다.
- OCR 모델은 이미지 빌드 때 받아 넣는다 (실행 중 Pod는 모델 서버에 나갈 수 없음). 이미지 2.0GB.
- 은행 게이트웨이 실습(`bank_llm_gateway`)은 같은 클러스터에 그대로 있고, 이 작업 동안 0개로 줄여 둠
  (`scripts/mac/free_bank_stack.sh undo`로 복구). 멀티테넌트·보안·다중 LLM은 다음 단계.

## 2. 흐름 (LangGraph)

철 하나 = 그래프 실행 한 번 (`thread_id` = 철 id). 상태는 노드가 끝날 때마다 Postgres에 저장된다.

```
철:   ocr → split ─(문서마다 Send, 동시 4)→ doc → validate ─(표시 있음 & human)→ review [interrupt] → index
                                                        └(없음 또는 auto)──────────────────────→ index
문서: extract → locate ─(칸 못 찾음 & 더 넓힐 수 있음)→ widen → locate (순환)
                      └→ read → merge
```

| 노드 | 하는 일 | PoC에서 가져온 것 |
|---|---|---|
| ocr | PaddleOCR 3.3.3 한국어, 긴 변 1920. 쪽마다 결과를 파일로 남김 → 다시 돌리면 건너뜀 | `run_ocr.py` 그대로 |
| split | 문서 시작 쪽 판별 (머리부 기관명·우편번호 + 문서번호/시행일자, '별지' 제외) | `gt_draft.analyze` 규칙 |
| extract | 문서 첫 쪽 텍스트 → GPT(gpt-4o-mini)가 4칸 + 확신도, 수신은 규칙 보정 | 추출 프롬프트, `recipient_fix` (목록·참조 규칙) |
| locate / widen | 인쇄된 '문서번호'·'시행일자' 상자 오른쪽 손글씨 칸 위치. 못 찾으면 머리부 범위 40→48→56% | `crop_fields` (PoC는 40% 고정) |
| read | 잘라낸 칸을 GPT 비전(gpt-4.1-mini)으로 판독, 확신도 = 토큰 확률 기하평균 | PoC는 ko-trocr(로컬) |
| merge | B 값의 분류번호 + 손글씨 일련번호, 손글씨 일자 우선 | `hw_flags.build` |
| validate | 검토 필요 표시: 형식·범위·흐름(앞뒤 3건)·확신도 + 수신·제목은 GPT 확신도 < 0.8 | `hw_flags` + 'LLM 표시' |
| review | `interrupt()`로 멈춤 → 검토 화면에서 고친 값이 `Command(resume=...)`로 들어오면 이어 감 | PoC의 검토 화면을 흐름 안으로 |
| index | Postgres upsert + 임베딩 → Qdrant | PoC의 SQLite FTS 대신 |

**왜 LangGraph인가 (그냥 함수 호출이 아니라)**
- 사람 검토에서 몇 시간 멈췄다 이어 가야 한다. 그 사이 Pod가 바뀌어도 상태가 Postgres에 있다.
- 문서마다 병렬(Send)로 돌리고 결과를 reducer로 합친다. 일부 문서가 실패해도 성공한 문서 결과는 체크포인트에 남는다.
- 칸 위치를 못 찾으면 범위를 넓혀 다시 찾는 순환이 그래프에 드러난다.
- 어떤 문서가 어떤 경로를 지났는지(재판독 범위, 표시 종류)가 결과에 남는다 → 자동 처리율을 잴 근거.
- 단계가 2~3개뿐인 일직선 작업이었다면 과했을 것이다.

**역할 나누기:** 철 단위 대기열은 지금은 FastAPI 안의 작업자 1개(OCR이 CPU를 다 씀). 처리량이 필요해지면
어제 게이트웨이처럼 Redis Streams + KEDA로 docflow 작업자를 늘리는 쪽이 자연스럽다 (LangGraph는 "무슨 단계를 어떤 순서로",
대기열은 "언제, 몇 개를").

## 3. API

| | |
|---|---|
| `POST /volumes/{vol}/pages` | 쪽 이미지 올리기 (multipart) |
| `POST /volumes/{vol}/run?review=human\|auto` | 흐름 시작. auto = 검토에서 멈추지 않음(평가용) |
| `POST /volumes/{vol}/resume` | 끊긴 곳부터 다시 |
| `GET /volumes/{vol}`, `/documents` | 상태(다음 노드·검토 대기·비용), 문서별 결과 |
| `GET/POST /review/{vol}` | 사람 검토 화면 → 제출하면 흐름 재개 |
| `GET /search?q=&year_from=&year_to=` | Qdrant 벡터 + Postgres 낱말 결과를 RRF로 합침 |
| `GET /ask?q=` | 상위 5건으로 GPT가 답하고 근거 doc_id를 단다 |
| `GET /graph` | 흐름 그림 (mermaid) |

## 4. 비용

모든 GPT 호출은 Postgres `usage` 표에 토큰·비용을 남기고, 하루 사용액이 `DAILY_BUDGET_USD`(3.0)를 넘으면 호출하지 않는다
(사후 정산이라 동시 호출 몇 건만큼은 넘을 수 있음). 1997-0002 철(13쪽, 문서 10건) 실측: 문서당 약 $0.0005
(추출 $0.0002, 손글씨 칸 2개 $0.00026, 임베딩 $0.00001). OCR은 로컬이라 0.

## 5. 실측 기록

### 전체 680쪽 (7개 철, 정답 164건, `eval/score.py`)
| 항목 | 텍스트 추출(B)만 | 최종 (손글씨 판독·규칙 합침) |
|---|---|---|
| 문서번호 | 30% | 64% |
| 일련번호 | 32% | 65% |
| 시행일자 | 62% | 79% |
| 수신 | 46% | 46% |
| 제목 | 88% | 88% |

- 문서 시작 판별 recall 85/86, precision 100%. 4칸 모두 정답 25%.
- 검토 표시: 일련번호 69% 칸에 표시 → 틀린 것의 98% 포함. 일자 30% 표시 → 49% 포함. 문서 단위로 80% 표시, 표시 없이 틀린 문서 10% (16건).
- 한 번 처리 비용 약 $0.35. 실험·재실행 포함 누적 약 $0.71 + 비교 스크립트 약 $0.2.

### 손글씨 칸 판독 모델 비교 (1997-0002 철 10건, 칸 20개, `eval/cell_variants.py`)
처음엔 gpt-4o-mini(low)로 시작했는데 일련번호를 빈칸("도시 58430-")으로 답하면서 확신도는 1.0에 가까웠다.

| 모델·설정 | 일련번호 | 일자 | 20칸 비용 |
|---|---|---|---|
| gpt-4o-mini low, 첫 지시문 ("비면 '-'까지만") | 5/10 | 6/10 | $0.0089 |
| gpt-4o-mini low, 새 지시문 ("손글씨를 숫자로 읽어라") | 4/10 | 6/10 | $0.0090 |
| gpt-4o-mini high, 새 지시문 | 4/10 | 6/10 | $0.0345 |
| gpt-4o high | 5/10 | 7/10 | $0.0261 |
| gpt-4.1 high | 7/10 | 7/10 | $0.0209 |
| **gpt-4.1-mini high (채택)** | **7/10** | **8/10** | **$0.0026** |

- gpt-4o-mini는 이미지 토큰을 크게 잡아 비싸고, 흘림체 숫자를 못 읽었다. gpt-4.1-mini는 이미지를 패치 단위로 세서 가장 쌌다.
- 10건뿐이라 1건 = 10%p. 680쪽 전체 결과로 다시 본다.
- 틀린 답에도 확신도가 높은 경우가 많다(0.9 이상). PoC 기준값(일련번호 0.9, 일자 0.8)은 ko-trocr 기준이라 다시 맞춰야 한다.

## 6. 한계 (지금)
- 정답은 PoC와 같은 김해 7개 철 164건뿐. 외부 양식은 20쪽(9건)만 확인했었다.
- 쪽 이미지에 실명·서명·직인이 있다. 이미지·OCR 결과는 맥 클러스터 PVC와 `work/`(git 제외)에만 둔다.
- 낱말 검색은 형태소 분석 없는 부분 일치. 벡터 검색과 RRF로 합칠 뿐 튜닝은 안 했다.
- 철 대기열이 프로세스 메모리에 있다 → Pod가 죽으면 대기 중이던 철은 다시 걸어야 한다(진행 중이던 철은 `/resume`).
- 사람 검토 화면에 인증이 없다 (port-forward로만 접근).
