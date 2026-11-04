# /healthz에 배포 판 표시

## 목표
배포 뒤 어느 판이 돌고 있는지 `/healthz`로 확인한다.

## 완료 조건
- [ ] `/healthz`가 `{"ok": true, "version": ...}`를 돌려준다. 값은 환경 변수 `APP_VERSION`, 없으면 `"dev"`
- [ ] 시험: 환경 변수가 없을 때 `"dev"`, 있을 때 그 값

## 범위 밖
- k8s 매니페스트에 APP_VERSION 넣기
