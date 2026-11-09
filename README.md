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
| extract | 첫 쪽 텍스트 → GPT가 색인 칸(스키마의 `prompt` 칸, 김해는 4칸) + 확신도. OCR 오독은 문맥으로 교정, 수신은 규칙 보정 |
| locate / widen | 인쇄된 '문서번호'·'시행일자' 옆 손글씨 칸 위치. 못 찾으면 머리부 범위 40→48→56% |
| read | 손글씨 칸 이미지를 GPT 비전으로 판독. 확신도 = 토큰 확률 기하평균(logprobs) |
| validate | 검토 필요 표시: 형식·범위·흐름(같은 철 앞뒤 문서와 비교)·확신도 |
| review | `interrupt()`로 멈춤 → 대시보드에서 고친 값이 `Command(resume=…)`로 들어오면 이어 감 |
| index | Postgres upsert + 임베딩 → Qdrant |

**왜 LangGraph인가:** 사람 검토에서 몇 시간 멈췄다 이어 가야 하고(체크포인트), 문서마다 병렬로 돌리고(Send + reducer),
칸을 못 찾으면 다시 찾는 순환이 있고, 어떤 경로를 지났는지가 남아야 자동 처리율을 잴 수 있기 때문입니다.

### 감사 로그·오류 (https://github.com/reddol18/docflow/issues/28)

| 남는 곳 | 내용 | 원문 값 |
|---|---|---|
| `audit` 표 | 업로드·실행·이어 가기·검토 저장·문서 수정·작업 시간 보내기마다 시각, 사용자, 행위, 철·문서 id, 결과(`ok` / `rejected_<상태>` / `error`+분류 코드), 개수·칸 이름. 뒤에서 돈 실행의 끝(`run_done`: `ok` / `review_waiting` / `error`)도 시작한 사람으로 | 없음 |
| `edits` 표 | 고친 칸의 이전 값·새 값·사용자 (https://github.com/reddol18/docflow/issues/10) | 있음 |
| `events` 표 (진행 화면) | 노드 시작·끝·오류. 오류는 분류 코드(`budget`·`llm`·`db`·`vector`·`network`·`storage`·`internal`)와 짧은 문구만 | 일부 있음 — 손글씨 판독 값(문서번호·일자), 사용자 이름 |
| 서버 로그 (표준 오류) | 오류 traceback. 키·Bearer/Basic·JWT, 이름이 key·secret·token·password로 끝나는 값, 주소 속 비밀번호는 `***`로 가림 (`app/errors.py`) | 예외 문구에 실명 등이 들어 있으면 남음 |

- 로그인 안 함(401)·권한 없음(403)·요청 형식 검증 실패(422, FastAPI가 처리 함수 앞에서 막음)는 `audit`에 남지 않는다. 로그인·로그아웃도 아직 남기지 않는다.
- 감사 기록 저장이 실패해도 요청은 원래 결과대로 끝나고, 실패는 서버 로그에 남는다.
- **보존:** `audit`은 1년 (2026-11-06 확정). 앱이 시작할 때마다(배포·재시작) 1년 지난 행을 지운다 (`store.prune_audit`).
- **납품 후 삭제 범위:** 쪽 이미지·OCR 결과·색인(`documents`)·벡터·`edits`(원문 값이 있음)·`events`는 지운다. `audit`은 원문 값이 없어 보존 기간까지 남긴다(누가 언제 무엇을 했는지는 납품 뒤에도 확인할 수 있어야 하므로).

### 납품 형식 출력·일괄 삭제 (https://github.com/reddol18/docflow/issues/12)

1. `GET /export/check?vol=…` — 출력 전 검증. 필수 열 빈칸·형식 위반(예: 일부만 읽은 날짜)·검토 안 끝난 건(검토 표시가 있는데 사람 검토 전) 수와 어느 문서·열인지. 철을 안 고르면 끝난 철 전부.
2. `GET /export.xlsx?vol=…` (관리자) — 철 목록 시트 + 건 색인 시트. 건은 철 안 쪽 순서(원질서)대로, 철마다 순번 1부터. 글자 칸은 모두 문자열로 써서 `=`로 시작하는 값이 수식이 되지 않는다.
3. `DELETE /volumes/{vol}?confirm={vol}` (관리자) — 내보낸 철만. 쪽 이미지·OCR·잘라낸 칸(`DATA_DIR`), `documents`·`edits`·`events`·검사 표·LangGraph 체크포인트, Qdrant 벡터를 지운다. `usage`(비용)·`audit`·`review_time`은 값이 없어 남긴다. 지운 개수는 감사 로그(`purge`)에.

양식 열 매핑은 YAML (`app/export_templates/aihub88.yaml`, 바꾸려면 `EXPORT_TEMPLATE=경로`): 열 이름 ↔ 원천(철 이름 조각·쪽 수, 건 순번·쪽, 색인 칸, 고정값) ↔ 형식(`text`·`int`·`year`·`date`+출력 모양). 기본은 AI Hub 88 공개 라벨 기준 예시(철번호·생산기관코드·생산연도 / 문서번호·생산일자 `YYYYMMDD`·수신·제목·시작쪽·끝쪽). 매핑이 틀리면 앱이 시작하지 않는다.

### 색인 칸 스키마 (https://github.com/reddol18/docflow/issues/9)

색인 칸은 코드가 아니라 YAML(`app/field_schemas/`)이 정한다. 쓰는 파일은 `FIELD_SCHEMA=경로`, 없으면 김해 매핑.

- **기본 스키마** `isad_g.yaml` — ISAD(G) 요소(참조코드·제목·일자·기술계층·쪽수·생산부서·내용 요약·보존기간·공개구분·저작권·이용조건·언어·위치·주기 …). 칸마다 ISAD(G) 영역(`area`)이 있다. 그대로 쓰려면 납품 양식(`EXPORT_TEMPLATE`)도 이 칸 이름에 맞춘 것을 줘야 한다 (기본 양식은 김해 칸 이름).
- **기관 매핑** `gimhae.yaml` — 지금 운영 자료(김해 인·허가)의 4칸(문서번호·시행일자·수신·제목)과 숨김 칸 일련번호. 칸마다 `isad:`로 기본 스키마 칸에 잇는다. 추출 지시문(`extract` + 칸별 `prompt`)도 여기 있다 — 바꾸면 점수를 다시 잰다.
- 칸 항목: 이름, 표시명(`label`), 단위(`level`: `document` 건 / `volume` 철), 추출 방식(`method`: `rule` 규칙 · `llm_extract` LLM 추출 · `llm_generate` LLM 문장 생성 · `vocab` 통제어휘 선택 · `fixed` 사업 고정값 · `manual` 사람 입력 · `hidden` 숨김), 표기 규칙(`normalize`). 빠진 항목·모르는 방식·모르는 키가 있으면 앱이 시작하지 않는다.
- 표기 규칙: `date_unknown`(미상 → `9999-99-99`), `decade`(`1950년대` → `1950-00-00 ~ 1959-00-00`), `location`(→ `서고-서가-층-열`), `hanja_hangul`(한자 → 한글 음, Unicode Unihan 표 `hanja_hangul.tsv` — 글자마다 대표 음 하나라 지명·성의 金(김) 같은 예외는 못 가림), `strip`.
- 사업 고정값(`fixed`, 예: 상위계층·저작권·공개구분·이용조건)은 스키마에 한 번 적으면 모든 건에 들어간다. 철 단위 칸은 지금 고정값·사람 입력만 된다.
- 저장: `documents.fields`(jsonb)에 칸 값 전부, 검색용 열 `search_text`(스키마 `search.keyword` 칸)·`year`(`search.year` 칸 앞 네 자리, 미상 제외). 예전 고정 열은 앱이 시작할 때 한 번 옮기고 지운다(`store.migrate_documents`, 한 트랜잭션). 열을 지우면 되돌릴 수 없으므로 이 판을 처음 배포하기 전에 Postgres를 백업한다(`pg_dump`). 이전 이미지로 되돌릴 때는 그 백업을 복원한다 (예전 코드는 새 열 구성을 읽지 못한다).
- 사람이 고친 값(검토·수정·검사)에도 같은 표기 규칙을 적용한다.
- 칸 이름이 아니라 `rule` 이름으로 판단한다 (https://github.com/reddol18/docflow/issues/72) — 기관 매핑은 칸 이름을 마음대로 지어도 된다.
  - 손글씨 칸 판독: `rule`이 `docno`·`serial`·`date`인 칸만 잘라 읽는다 (`app/graph.py` `HW_CELLS`). `/schema`가 칸마다 `rule`과 보여 줄 잘라낸 칸(`hw`)을 내보내고, 검토 화면·잘라낸 칸 경로가 그것으로 판단한다.
  - 검토 표시(`app/flags.py`): 김해 규칙(일자·일련번호·수신)은 그 `rule` 칸이 있을 때만 돈다. LLM 확신도 표시는 LLM 추출 칸(`llm_extract`·`vocab`, 수신 같은 규칙 칸의 LLM 값 포함, 손글씨 규칙 칸·`llm_generate` 요약 제외)마다 붙는다. 표시 키는 칸의 `flag_key`(없으면 칸 이름).
  - 검색 화면 열은 스키마 `search.columns`(없으면 보이는 칸 전부, `/schema`의 `search_columns`).
  - 칸 이름을 바꾼 김해 매핑으로 칸 값·표시가 같은지: `eval/rename_replay.py <저장된 철 JSON…>`. 다른 고객 시험 스키마(지어낸 값)는 `tests/fixtures/schemas/`.
- 철 단위 칸은 고정값·사람 입력만 된다 (철 단위 추출 없음).

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

### 모델·지시문 비교 하네스 (https://github.com/reddol18/docflow/issues/22)

모델이나 추출 지시문을 바꿀 때 같은 데이터셋(7개 철 450건)·같은 통과 기준으로 비교한다. promptfoo 0.125.0(`eval/promptfoo/package-lock.json` 고정)으로
후보 × 지시문 판 × 문서 행렬을 돌리고, `eval/promptfoo/gate.py`가 후보 응답으로 철마다 `app/graph.py` merge·validate를 다시 돌려 `eval/score.py` `metrics()`로 채점한다.

```powershell
.\eval\promptfoo\run.ps1                       # gpt-4.1-mini(기준)·gpt-4o-mini × 지시문 current·before_ocr_fix × 7개 철 (1800회, 약 20분·$0.66)
.\eval\promptfoo\run.ps1 -Local -Vols 5350109-1999-0002   # + 로컬 모델(이 PC의 Ollama, 기본 llama3.1:8b). 꺼져 있으면 켜고, 켠 것만 끈다
.\eval\promptfoo\run.ps1 -Local -LocalModel qwen2.5:7b -BaselineFrom ..\work\promptfoo\results_full_20261107.json
                                               # 로컬 모델만 돌리고 기준(gpt-4.1-mini·current) 줄은 지난 결과에서 가져온다 (OpenAI 호출 0, 키 불필요)
.\eval\promptfoo\run.ps1 -Replay               # LLM 호출 없이 저장된 추출 값으로 채점 연결만 확인 (eval/score.py와 같은 숫자)
.\eval\promptfoo\run.ps1 -View                 # 마지막 결과를 promptfoo 비교 화면으로 (http://127.0.0.1:15500)
```

종료 코드: 0 통과, 1 기준을 어긴 후보 있음, 2 입력 문제. 통과 기준은 `eval/promptfoo/criteria.yaml` — 기준 후보(gpt-4.1-mini·current) 대비
표시 없이 틀린 문서 +1.0%p 초과, 칸별 정답률 −3.0%p 초과, 응답 읽기 실패 1건 이상이면 실패.

전체 행렬 결과 (2026-11-07, 7개 철 450건, 정답 164건):

| 후보 | 판 | docno | date | recipient | title | 일련번호 표시 재현율 | 일자 표시 재현율 | 표시 없이 틀림 | 읽기 실패 | 건당 비용 | 평균 지연 | p95 지연 | 판정 |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| gpt-4.1-mini | before_ocr_fix | 63.4% | 78.7% | 46.3% | 87.2% | 98.2% | 54.3% | 1.8% | 0 | $0.00048 | 1.4s | 1.8s | 통과 |
| gpt-4.1-mini | current | 64.0% | 78.7% | 47.0% | 87.2% | 100.0% | 57.1% | 1.8% | 0 | $0.00055 | 1.4s | 1.8s | 기준 |
| gpt-4o-mini | before_ocr_fix | 63.4% | 78.0% | 50.6% | 81.7% | 98.2% | 38.9% | 3.7% | 1 | $0.00020 | 2.5s | 3.2s | 실패: 표시 없이 틀림 +1.9%p, title −5.5%p, 읽기 실패 1 |
| gpt-4o-mini | current | 63.4% | 78.0% | 56.1% | 77.4% | 98.2% | 41.7% | 7.3% | 1 | $0.00023 | 2.5s | 3.5s | 실패: 표시 없이 틀림 +5.5%p, title −9.8%p, 읽기 실패 1 |

- **열:** docno~title = 칸별 최종 정답률(손글씨 판독·검토 표시 규칙까지 거친 값), 표시 재현율 = 틀린 칸 중 검토 표시가 붙은 비율,
  표시 없이 틀림 = 검토 표시 없이 한 칸이라도 틀린 문서 비율, 읽기 실패 = 응답이 JSON으로 안 읽힌 문서 수, 건당 비용 = 토큰 × `app/llm.py` PRICE,
  지연 = promptfoo가 잰 호출 시간(`-Cache`로 다시 쓴 응답은 빼고).
  후보마다 LLM 추출 값만 바꾸고 손글씨 판독 등은 저장 값을 쓴 채 지금 코드의 검토 표시 규칙으로 다시 조립한다.
  위 '측정 결과' 표(표시 없이 틀림 10%)는 그 뒤 바뀐 표시 규칙 전의 저장 결과라 숫자가 다르다.
- **채점 연결 확인:** `-Replay`(저장된 추출 값, LLM 호출 없음)의 점수는 저장된 b·hw를 지금 코드로 다시 조립한 결과(`eval/schema_replay.py` `replay()`, 후보와 같은 길)와 같고,
  `eval/score.py`를 같은 문서에 단독으로 돌린 숫자(docno 64.0%, date 78.7%, recipient 45.7%, title 88.4%, 표시 재현율 100%·57.1%)와도 같다.
- **로컬 모델:** 이 Windows PC의 Ollama만 쓴다(맥은 배포 시험 환경이라 실험에 쓰지 않는다). 띄우기 전에 모델 폴더 드라이브 여유 10GB를 확인한다.
  llama3.1:8b는 작은 철(6건)에서 제목이 기준보다 크게 떨어져 실패, 건당 지연 약 9초·비용 0. 한국어 소형 모델 3개의 전체 450건 측정은
  `docs/design.md` "로컬 텍스트 모델 4칸 추출" (https://github.com/reddol18/docflow/issues/13).
- **기준 다시 쓰기(`-BaselineFrom`, `-Local`과 함께, `-Replay`와는 같이 못 씀):** 로컬 모델만 비교할 때 기준 모델을 다시 부르지 않는다.
  기본 후보 `^local-ollama$`, 판 `^current$`. promptfoo를 부르기 전에 `eval/promptfoo/plan.py`가 promptfoo와 같은 규칙(제공자 id·label에
  대소문자 구분 정규식)으로 걸러질 후보를 세어, 기준(`criteria.yaml` baseline)과 겹치면 멈추고(돈 쓰고 버리는 호출 방지), OpenAI 후보가
  없으면 키를 읽지 않는다. 기준 파일은 `work/promptfoo/` 아래 사본을 `gate.py --baseline-results`에 넘긴다(끝나면 지움).
  `gate.py`는 기준 줄만 이번 결과의 철에 맞춰 가져오고, 기준 모델 id·temperature·response_format·apiBaseUrl이 이번 설정과 다르거나,
  같은 판 후보의 지시문(`prompt.raw`)이 문서마다 기준과 다르거나, 문서 묶음이 다르면 종료 코드 2. `max_tokens` 같은 그 밖의 차이는 gate.md에 경고.
  `gate.md`에는 판정 표 아래 텍스트 추출(B)만 칸별 정답률 표가 붙는다.
- **답변 채점(Ragas·쌍별 승률)은 아직 없다:** `criteria.yaml`의 `answer:` 칸은 자리만 있다 → https://github.com/reddol18/docflow/issues/21.
- **결과·개인정보:** 실제 문서 글자가 담긴 결과(`results.json`, 다시 조립한 문서, promptfoo 결과 DB)는 저장소 밖 `work/promptfoo/`에만 쓴다.
  promptfoo 공유·원격 생성·사용 통계·업데이트 확인은 끈다(`PROMPTFOO_DISABLE_*`, `--no-share`). 단, promptfoo 0.125.0은 사용 통계를 꺼도
  'telemetry disabled' 이벤트 한 번을 r.promptfoo.app으로 보낸다(문서 글자·결과는 없음).
  비교 화면(`-View`)은 `eval/promptfoo/localhost_only.cjs`로 127.0.0.1에만 묶고, Host·Origin이 `127.0.0.1:15500`/`localhost:15500`이 아닌 요청은
  403, 응답의 `Access-Control-Allow-Origin`은 지운다(promptfoo 기본은 모든 인터페이스·모든 출처 허용). 시험(`tests/test_model_harness.py`)은 지어낸 값만 쓴다.
- **키·비용:** OpenAI 키는 Windows 사용자 환경변수에서 읽어 promptfoo를 부르는 동안만 프로세스 환경에 둔다(Ollama·`gate.py`는 물려받지 않음).
  월 한도를 건 이 하네스 전용 OpenAI 프로젝트 키를 권한다. 응답마다 `max_tokens: 512`(정상 응답 최대 310토큰),
  단가(`app/llm.py` PRICE)를 모르는 원격 모델이 섞이면 0원으로 치지 않고 종료 코드 2.
- **로컬 모델 주소:** `run.ps1 -Local`이 켜는 Ollama는 `OLLAMA_HOST=127.0.0.1:11434`로 켠다(이 PC의 Machine 환경변수 `0.0.0.0:11434`를 덮음,
  켠 뒤 듣는 주소를 확인하고 127.0.0.1 밖이면 멈춤). 이미 켜 둔 Ollama는 건드리지 않고, 모든 인터페이스에서 듣고 있으면 경고만 한다.

## 부하 시뮬레이션 (breakscale_wrap)

200쪽짜리 철이 한꺼번에 올라오면 어떻게 되는지를 직접 만든 시뮬레이터 [breakscale_wrap](https://github.com/reddol18/breakscale_wrap)으로 돌렸습니다.
트래픽은 두 구성이 같습니다: 평소 분당 3쪽, 120~220초에 약 200쪽 몰림 (seed 1에서 210쪽).

| A. 지금 구성 — 맥 k3s 1노드, OCR Pod 1개 | B. 클라우드 — 공유 대기열 + OCR 워커 Pod 1~6개 (KEDA) |
|---|---|
| ![A](docs/media/load_a_mac.gif) | ![B](docs/media/load_b_cloud.gif) |
| [mp4](docs/media/load_a_mac.mp4) · 영상 130초 = 실제 65분 | [mp4](docs/media/load_b_cloud.mp4) · 영상 40초 = 실제 20분 |

| | A. 맥 1노드 | B. 클라우드 |
|---|---|---|
| OCR 처리 상한 | 분당 6.7쪽 (쪽당 9초 × 1개) | 분당 40쪽 (6개) |
| 대기열 최대 | 약 190쪽 | 약 190쪽 |
| 몰림 끝 → 마지막 쪽 OCR 끝 | **약 56분** | **약 6분** (증설 대기 110초 포함) |
| 실패 | 0 (대기열이 받아 둠) | 0 |
| 몰림 1번에 더 드는 비용 | 0 | 노드 5대 × 약 12.5분 ≈ **$0.20** [가정: c6i.xlarge 서울 온디맨드 약 $0.19/h] |

- **병목은 OCR CPU 하나뿐입니다.** GPT 단계(쪽당 약 2.8초, 동시 4건)는 B에서도 가동률 50% 안팎이라 처리 상한이 분당 약 85쪽입니다. 워커를 12개 넘게 늘리면 그다음은 GPT 동시 호출 수와 계정 분당 한도가 막힙니다.
- **A에서 실패가 없는 이유는 대기열 덕분이지만, 그 대기열이 Pod 메모리에 있습니다.** 56분 사이에 Pod가 재시작되면 대기 중인 쪽이 사라집니다 → [#5](https://github.com/reddol18/docflow/issues/5). B는 이 대기열을 Redis/SQS로 빼는 것이 전제입니다.
- **B로 가려면 바뀌는 것:** 철 단위 실행을 쪽 단위 OCR 작업으로 쪼개 대기열에 넣고, OCR 워커를 API와 분리한 Deployment로 띄워 KEDA가 대기열 길이로 늘립니다.
  LangGraph 그래프는 OCR이 끝난 철부터 이어서 돌립니다(체크포인트는 그대로 Postgres).
- **고정비 어림** [가정, 서울 온디맨드]: EKS 제어부 약 $73/월 + OCR 노드 1대 상시 약 $140/월 + API·DB 노드 약 $76/월 ≈ **월 $290**. GPT는 680쪽에 약 $0.35.

**넣은 값과 출처**

| 값 | 출처 |
|---|---|
| OCR 쪽당 9초, Pod 1개가 한 번에 한 쪽 | [실측] 맥 k3s, 7~11초 |
| GPT 쪽당 2.8초, 동시 4건 | [실측 근사] 추출 2.2초 + 손글씨 판독 1.8초를 문서당 1.45쪽으로 나눔 |
| 몰림 크기·평소 유입량 | [가정] |
| 증설 대기 110초 (KEDA 감지 30초 뒤 시작, 노드 준비 + 2GB 이미지 받기 + 모델 적재) | [가정]. 첫 쪽 15.6초(모델 적재 포함)는 실측 |
| 축소 시점 (대기열이 빈 뒤 5분) | [가정] KEDA 기본 cooldown과 비슷하게 |

**모델의 한계**
- 실제 docflow는 철의 모든 쪽을 OCR한 뒤 문서별 GPT 단계로 넘어갑니다. 시뮬레이션은 쪽 단위 파이프라인으로 근사했습니다. OCR 처리 상한은 같습니다.
- B의 증설·축소 시각은 위 가정으로 계산해 시각표로 넣었습니다. breakscale_wrap의 자체 오토스케일러는 LB가 요청을 밀어 넣는 구조라, 워커가 대기열에서 꺼내 가는 이 구조에는 쓰지 않았습니다.
- 몰림 크기는 도착이 무작위라 seed마다 다릅니다(seed 2에서 약 130쪽). 위 숫자는 seed 1입니다.
- 시나리오: `sim/a_mac.json`, `sim/b_cloud.json` (영상용 30배 압축본 `*_x30.json`, `sim/compress.py`)

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

## 개발 절차

- 로컬 검사: `.\scripts\check.ps1` — 끝에 나오는 요약을 PR '테스트' 칸에 붙인다 (클라우드 CI 없음).
- **병합은 `scripts/merge.ps1`로만** 한다 (https://github.com/reddol18/docflow/issues/83). `.\scripts\merge.ps1 <PR> -DryRun`으로 먼저 확인한다.
  PR head 커밋의 로컬 검사 요약(막는 검사 모두 통과, 건너뜀 거부)·계약 리뷰·버그 리뷰 결과 댓글(댓글·리뷰 한 글 안에 이름과
  '| 지적 | 반영 |' 표)·보안 영향 칸에 체크가 있으면 보안 리뷰 결과 댓글이 있어야, 확인한 head 그대로 squash 병합(제목 끝 `(#N)`)한다.
  `docs/`·README·잠금 파일만 바꾼 PR은 리뷰 흔적을 요구하지 않고 osv·semgrep·trivy 건너뜀(`-Quick`)을 허용한다.
  실제 병합은 사용자 승인 뒤에만. 판정은 `scripts/merge_gate.py`가 하고, 흔적 찾기·코드 없음·보안 영향 함수는 주간 지표
  `scripts/weekly_metrics.py`와 공유한다. 주간 지표는 흔적이 있는지만 세고, 병합 검사가 더 엄격하다(head 기준, 표 필요, 건너뜀 거부, 리뷰는 PR 본문 제외).

## 폴더

| | |
|---|---|
| `app/` | FastAPI · LangGraph 흐름(`graph.py`) · OCR · 규칙(`layout.py`) · GPT 호출(`llm.py`) · 저장소 |
| `frontend/` | Next.js 16 대시보드 (업로드 · 진행 단계 · 검토 에디터 · 검색) |
| `k8s/` | 네임스페이스 · 네트워크 정책 · Postgres · Qdrant · docflow |
| `eval/` | 정답 채점(`score.py`), 모델·지시문 비교 하네스(`promptfoo/`, 예전 일회성 `cell_variants.py`, `extract_variants.py`). 정답 데이터와 채점 규칙은 이전 PoC 폴더에 있어 이 레포만으로는 돌지 않음 |
| `sim/` | breakscale_wrap 부하 시나리오 (지금 구성 / 클라우드)와 실행기 |
| `docs/` | 설계(`design.md`), 실습 기록(`lab_log.md`), 화면 캡처·시뮬레이션 영상 |
