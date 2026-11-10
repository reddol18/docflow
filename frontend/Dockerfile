# 프런트 운영 이미지 docflow-web:dev (https://github.com/reddol18/docflow/issues/48): 빌드 단계에서 next build, 실행은 next start.
# 개발 서버(next dev)·HMR(/_next/webpack-hmr)·개발 오버레이·CSP 'unsafe-eval'(src/proxy.ts 개발 분기)이 들어가지 않는다.
# 맥에서 scripts/mac/build_image.sh가 빌드한다 (k8s/docs.yaml web 파드). 이 PC의 localhost:3000 개발 서버(npm run dev)와는 따로다.
# 실행 설정(명령·환경)은 이 파일 한 곳 — k8s는 tls 컨테이너 명령만 덮는다.
# 기반 이미지는 다이제스트로 고정한다 (https://github.com/reddol18/docflow/issues/46). 판을 올리면 태그와 다이제스트를 함께
# 바꾼다 (지금 다이제스트 확인: scripts/mac/image_tags.sh). 취약점 요약은 배포 끝 scan_image.sh가 docflow-web:dev로 본다.
FROM node:24.22.2-alpine3.24@sha256:a34ab6d7f6f4813f4d0c5469d74685604b962fbdbf5793a7bc7b4998ecb867c8 AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
# package-lock.json 그대로 (판·무결성 해시 고정)
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# /api/* → docflow rewrite(next.config.ts)는 빌드 때 routes-manifest에 박힌다 — 실행 때 DOCFLOW_URL을 바꿔도 rewrite는 그대로.
# 클러스터 안 주소. 흐림 빌드는 캡처 때만 (기본 끔)
ARG DOCFLOW_URL=http://docflow:8000
ARG NEXT_PUBLIC_MASK_IMAGES=0
ENV DOCFLOW_URL=$DOCFLOW_URL NEXT_PUBLIC_MASK_IMAGES=$NEXT_PUBLIC_MASK_IMAGES
# 개발 의존성을 빼고, 실행 단계로 옮길 것만 /out에 모아 권한을 여기서 맞춘다 (실행 단계에서 chmod -R 하면 모든 파일이 층에 한 번 더 실린다)
RUN npx next build && npm prune --omit=dev --no-audit --no-fund \
    && rm -rf .next/cache && mkdir -p /out .next/cache \
    && cp -a package.json next.config.ts tsconfig.json node_modules public .next tls-proxy.mjs /out/ \
    && chmod -R u=rwX,go=rX /out

FROM node:24.22.2-alpine3.24@sha256:a34ab6d7f6f4813f4d0c5469d74685604b962fbdbf5793a7bc7b4998ecb867c8
WORKDIR /app
# DOCFLOW_URL(실행 때): 흐림 route(src/lib/masked.ts)가 읽는다 — 위 빌드 인자와 같은 값. HOME: 읽기 전용 루트라 emptyDir(/tmp)
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 DOCFLOW_URL=http://docflow:8000 HOME=/tmp
# 파일은 root 소유(읽기만) — 실행 사용자 node(1000)는 고칠 수 없다. 쓰는 곳(.next/cache, /tmp)은 k8s emptyDir
COPY --from=build /out/ ./
USER node
EXPOSE 8443
# next start는 파드 안 127.0.0.1:3000에서만 받는다 — 바깥은 tls 컨테이너(node tls-proxy.mjs, k8s/docs.yaml)가 8443 TLS로
CMD ["node_modules/.bin/next", "start", "-H", "127.0.0.1", "-p", "3000"]
