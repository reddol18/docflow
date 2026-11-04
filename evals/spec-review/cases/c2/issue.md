# 리포트 CSV를 캐시에 남기지 않기

## 목표
`/report.csv`(처리 수·검토 시간이 담긴 내부 자료)가 브라우저·중간 캐시에 남지 않는다.

## 완료 조건
- [ ] `/report.csv` 응답에 `Cache-Control: no-store`
- [ ] 시험: `/report.csv` 응답 헤더에 `Cache-Control: no-store`가 있다

## 범위 밖
- 다른 경로의 캐시 헤더 (전체 적용은 따로)
