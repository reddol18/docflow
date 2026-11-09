# 프런트 운영 이미지 docflow-web:dev (https://github.com/reddol18/docflow/issues/48): 빌드 단계에서 next build, 실행은 next start.
# 개발 서버(next dev)·HMR(/_next/webpack-hmr)·개발 오버레이·CSP 'unsafe-eval'(src/proxy.ts 개발 분기)이 들어가지 않는다.
# 맥에서 scripts/mac/build_image.sh가 빌드한다 (k8s/docs.yaml web 파드). 이 PC의 localhost:3000 개발 서버(npm run dev)와는 따로다.
# 기반 이미지는 다이제스트로 고정한다 (https://github.com/reddol18/docflow/issues/46). 판을 올리면 태그와 다이제스트를 함께
# 바꾼다 (지금 다이제스트 확인: scripts/mac/image_tags.sh). 취약점 요약은 배포 끝 scan_image.sh가 docflow-web:dev로 본다.
FROM node:24.22.2-alpine3.24@sha256:a34ab6d7f6f4813f4d0c5469d74685604b962fbdbf5793a7bc7b4998ecb867c8 AS build
WORKDIR /app
ENV NEXT_TELEMETRY_DISABLED=1
# package-lock.json 그대로 (판·무결성 해시 고정)
COPY package.json package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY . .
# /api/* → docflow rewrite(next.config.ts)는 빌드 때 정해진다 — 클러스터 안 주소. 흐림 빌드는 캡처 때만 (기본 끔)
ARG DOCFLOW_URL=http://docflow:8000
ARG NEXT_PUBLIC_MASK_IMAGES=0
ENV DOCFLOW_URL=$DOCFLOW_URL NEXT_PUBLIC_MASK_IMAGES=$NEXT_PUBLIC_MASK_IMAGES
RUN npx next build && npm prune --omit=dev --no-audit --no-fund

FROM node:24.22.2-alpine3.24@sha256:a34ab6d7f6f4813f4d0c5469d74685604b962fbdbf5793a7bc7b4998ecb867c8
WORKDIR /app
ENV NODE_ENV=production NEXT_TELEMETRY_DISABLED=1 DOCFLOW_URL=http://docflow:8000 HOME=/tmp
# 파일은 root 소유(읽기만) — 실행 사용자 node(1000)는 고칠 수 없다. 쓰는 곳(.next/cache, /tmp)은 k8s emptyDir
COPY --from=build /app/package.json /app/next.config.ts /app/tsconfig.json ./
COPY --from=build /app/node_modules ./node_modules
COPY --from=build /app/public ./public
COPY --from=build /app/.next ./.next
COPY tls-proxy.mjs ./
RUN rm -rf .next/cache && mkdir .next/cache && chmod -R a+rX /app
USER node
EXPOSE 8443
CMD ["node_modules/.bin/next", "start", "-H", "127.0.0.1", "-p", "3000"]
