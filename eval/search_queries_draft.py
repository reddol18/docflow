"""검색 질의 세트 초안 (https://github.com/reddol18/docflow/issues/20): 질의마다 판정 규칙(제목 정규식·연도)으로
정답 문서를 뽑는다. 질의 문구와 규칙은 LLM(Claude)이 색인 제목을 보고 쓴 초안이고, 사람이 규칙·정답을 확인한 뒤
`checked: true`로 바꾼다. 사람이 고친 것은 `eval/data/search_queries.jsonl`에 직접 고친다(이 스크립트를 다시 돌리면 덮어씀).

  python -I eval/search_queries_draft.py <index.jsonl> eval/data/search_queries.jsonl <review.md>
  index.jsonl: documents 표에서 doc_id·vol·date·docno·title·recipient만 뽑은 것 (저장소 밖에 둔다, 원문 없음)
  review.md: 사람이 확인할 표 (질의·규칙·정답 수·정답 예 제목) — 저장소 밖"""
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

here = Path(sys.argv[1])
out_jsonl, out_md = Path(sys.argv[2]), Path(sys.argv[3])
docs = [json.loads(line) for line in here.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
docs = [d for d in docs if not d["vol"].startswith("test")]
T = {d["doc_id"]: d for d in docs}

# (유형, 질의, 제목 정규식, 연도 또는 None, 제외 정규식 또는 None)
TOPIC = [
    ("주제", "공장설립 완료신고 공장등록증 교부", r"완료\s*(신고|보고).*등록(증|사항)", None, None),
    ("주제", "공장설립 변경 승인", r"공장설립\s*(사업계획\s*)?변경\s*승인", None, r"보완"),
    ("주제", "공장설립 승인사항 통보", r"공장설립\s*승인(사항)?\s*(통보|수리)?", None, r"변경|취소|보완|지연"),
    ("주제", "공장설립 신고 수리", r"공장설립\s*(변경\s*)?신고\s*수리", None, None),
    ("주제", "공장설립 승인 취소", r"공장설립(승인|신고)\s*취소", None, None),
    ("주제", "공장등록 변경 수리", r"공장등록\s*변경", None, None),
    ("주제", "창업 사업계획 승인 취소", r"창업.*승인\s*취소", None, None),
    ("주제", "창업 사업계획 변경 승인", r"창업\s*사업계획\s*변경|창업사업계획변경", None, None),
    ("주제", "농공단지 입주계약 변경", r"입주계약\s*변경", None, None),
    ("주제", "농공단지 준공인가전 사용허가", r"준공인가전", None, None),
    ("주제", "개발제한구역 건축물 용도변경 허가", r"용\s*도\s*변경", None, r"보완"),
    ("주제", "개발제한구역 토지형질변경 허가", r"개발제한구역.*형질변경", None, None),
    ("주제", "토지형질변경 불허가", r"형질변경\s*불허가", None, None),
    ("주제", "건축물 사용검사필증 교부", r"사용검사", None, None),
    ("주제", "건축물 중간검사 필증", r"중간검사", None, None),
    ("주제", "분뇨정화조 준공 통보", r"정화조\s*준공", None, None),
    ("주제", "오수정화시설 설치 신고", r"오수정화시설", None, None),
    ("주제", "소음 배출시설 설치신고 수리", r"소음.*배출시설.*설치", None, None),
    ("주제", "대기배출시설 설치 신고", r"대기.*배출시설", None, None),
    ("주제", "식품위생법 위반업소 영업정지", r"식품위생법.*영업정지", None, None),
    ("주제", "행정심판청구서 집행정지신청서 송부", r"행정심판청구서\s*및?\s*집행정지", None, None),
    ("주제", "행정심판 재결서 송부", r"재결서", None, None),
    ("주제", "체육시설업 행정처분 의견서 제출", r"체육시설업.*의견서", None, None),
    ("주제", "국유재산 대부료", r"대부(료|계약)", None, None),
    ("주제", "지방채무현황 운용실태", r"지방채", None, None),
    ("주제", "부동산실명법 위반 과징금", r"부동산실명법", None, None),
    ("주제", "농지전용 허가", r"농지전용", None, None),
    ("주제", "송전철탑 원상복구", r"송전철탑", None, None),
    # 뜻은 같고 낱말이 다른 질의 (벡터 검색을 잰다)
    ("주제(바꿔 말하기)", "음식점 영업정지 처분", r"식품위생법.*영업정지", None, None),
    ("주제(바꿔 말하기)", "그린벨트 안 주택 이축 허가", r"이축", None, None),
    ("주제(바꿔 말하기)", "미성년자 혼숙 숙박업소 적발", r"혼숙", None, None),
    ("주제(바꿔 말하기)", "나라 땅을 빌려 쓰는 사용료", r"대부(료|계약)", None, None),
    ("주제(바꿔 말하기)", "당구장 영업 신고", r"당구장", None, None),
    ("주제(바꿔 말하기)", "공장 문을 닫아 등록을 취소", r"(공장등록|공장설립신고)\s*취소|취소원", None, None),
]
COND = [
    ("조건", "1999년 식품위생법 영업정지", r"식품위생법.*영업정지", "1999", None),
    ("조건", "1997년 공장설립 완료신고 공장등록증", r"완료\s*(신고|보고).*등록(증|사항)", "1997", None),
    ("조건", "2001년 창업 사업계획 승인 취소", r"창업.*승인\s*취소", "2001", None),
    ("조건", "1994년 개발제한구역 용도변경 허가", r"용\s*도\s*변경", "1994", r"보완"),
    ("조건", "2000년 공장설립 변경승인", r"공장설립\s*(사업계획\s*)?변경\s*승인", "2000", r"보완"),
    ("조건", "1996년 소음배출시설 설치신고", r"소음.*배출시설.*설치", "1996", None),
    ("조건", "1998년 국유재산 대부료", r"대부(료|계약)", "1998", None),
    ("조건", "1999년 행정심판청구서 송부", r"행정심판청구서", "1999", None),
]
NONE = ["도로 신설 허가", "1997년 도로 점용 허가", "아파트 분양 승인", "학교 급식 위생 점검", "산불 피해 보상",
        "버스 노선 변경", "수도요금 감면", "주민등록 전입신고", "문화재 발굴 조사", "어업 면허 발급"]
NONE_RX = {"도로 신설 허가": r"도로", "1997년 도로 점용 허가": r"도로|점용", "아파트 분양 승인": r"아파트",
           "학교 급식 위생 점검": r"급식|학교", "산불 피해 보상": r"산불", "버스 노선 변경": r"버스|노선",
           "수도요금 감면": r"수도", "주민등록 전입신고": r"주민등록|전입", "문화재 발굴 조사": r"문화재|발굴",
           "어업 면허 발급": r"어업"}


def match(rx, year, ex):
    out = []
    for d in docs:
        t = d["title"] or ""
        if re.search(rx, t) and not (ex and re.search(ex, t)) and (year is None or (d["date"] or "").startswith(year)):
            out.append(d["doc_id"])
    return out


rows = []
random.seed(20)
# 정확 조회: 문서번호가 철 안에서 하나뿐인 문서 12개
by_no = {}
for d in docs:
    if d["docno"] and re.search(r"\d{3,}-\d+", d["docno"]):
        by_no.setdefault(d["docno"].strip(), []).append(d["doc_id"])
uniq = sorted(k for k, v in by_no.items() if len(v) == 1)
for no in random.sample(uniq, 12):
    did = by_no[no][0]
    rows.append({"type": "정확 조회", "q": no, "rule": f"문서번호 = {no}", "relevant": [did],
                 "answer": f"문서번호 {no} 문서는 1건이다.", "must_cite": [did]})  # 제목·원문은 저장소에 넣지 않는다
for typ, q, rx, year, ex in TOPIC + COND:
    rel = match(rx, year, ex)
    rule = f"제목 ~ /{rx}/" + (f" 제외 /{ex}/" if ex else "") + (f", 시행일자 {year}년" if year else "")
    rows.append({"type": typ, "q": q, "year": year, "rule": rule, "relevant": rel,
                 "answer": f"관련 문서는 {len(rel)}건이다.", "must_cite": rel[:3]})
for q in NONE:
    hits = [d["doc_id"] for d in docs if re.search(NONE_RX[q], d["title"] or "")]
    assert not hits, (q, hits)  # 답 없음 질의는 제목에 관련 낱말이 없어야 한다
    rows.append({"type": "답 없음", "q": q, "rule": f"제목에 /{NONE_RX[q]}/ 0건 (확인함)", "relevant": [],
                 "answer": "해당 문서가 없다.", "must_cite": []})
for i, r in enumerate(rows, 1):
    r["id"] = f"q{i:02d}"
    r["checked"] = False  # 사람이 확인하면 true

with out_jsonl.open("w", encoding="utf-8", newline="\n") as f:
    for r in rows:
        keep = {k: r[k] for k in ("id", "type", "q", "year", "relevant", "answer", "must_cite", "rule", "checked") if k in r}
        f.write(json.dumps(keep, ensure_ascii=False) + "\n")

md = ["| id | 유형 | 질의 | 판정 규칙 | 정답 수 | 정답 예 (제목) |", "|---|---|---|---|---|---|"]
for r in rows:
    sample = " / ".join(T[d]["title"] for d in r["relevant"][:3])
    md.append(f"| {r['id']} | {r['type']} | {r['q']} | `{r['rule']}` | {len(r['relevant'])} | {sample} |")
out_md.write_text("\n".join(md) + "\n", encoding="utf-8")
print(len(rows), Counter(r["type"] for r in rows))
print("정답 0건인 답 있음 질의:", [r["q"] for r in rows if r["type"] != "답 없음" and not r["relevant"]])
