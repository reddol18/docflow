import type { NextConfig } from "next";

// 브라우저는 /api/* 로만 부르고, Next 서버가 docflow API로 넘긴다 (CORS 불필요).
// 로컬: ssh 터널 → kubectl port-forward 로 127.0.0.1:8000 이 docflow.
const DOCFLOW_URL = process.env.DOCFLOW_URL ?? "http://127.0.0.1:8000";

const nextConfig: NextConfig = {
  // 대시보드는 브라우저에서 API를 계속 다시 읽는 화면이라 Cache Components(미리 렌더링)는 쓰지 않는다
  cacheComponents: false,
  turbopack: {
    rules: {
      "*.css": {
        loaders: ["@tailwindcss/turbopack"],
        as: "*.css",
      },
    },
  },
  // 보안 헤더 (https://github.com/reddol18/docflow/issues/47): 페이지와 Next가 답하는 /api/*(흐림 이미지)는 src/proxy.ts,
  // docflow로 넘긴 /api/* 응답은 docflow(app/main.py)가 붙인다.
  // 외부로 넘기는 rewrite 응답에는 proxy.ts·headers()의 헤더가 실리지 않고, docflow 응답 헤더는 그대로 전달된다 (2026-11-04 확인)
  // proxy.ts matcher가 빼는 정적 파일(_next/static 등)의 nosniff는 여기서
  async headers() {
    return [{ source: "/:path*", headers: [{ key: "X-Content-Type-Options", value: "nosniff" }] }];
  },
  async rewrites() {
    return {
      // 흐림 빌드(외부 공개·캡처용): 원본 쪽 이미지·잘라 둔 이미지(수신 줄 실명 등)는 서버에서 흐린 것만 내보낸다
      // (CSS 흐림은 이미지 주소로 우회 가능). src/lib/masked.ts
      beforeFiles:
        process.env.NEXT_PUBLIC_MASK_IMAGES === "1"
          ? [
              { source: "/api/head/:vol/:page", destination: "/masked/head/:vol/:page" },
              { source: "/api/crops/:vol/:name", destination: "/masked/crops/:vol/:name" },
            ]
          : [],
      afterFiles: [],
      fallback: [{ source: "/api/:path*", destination: `${DOCFLOW_URL}/:path*` }],
    };
  },
};

export default nextConfig;
