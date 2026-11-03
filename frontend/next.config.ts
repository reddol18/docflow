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
  async rewrites() {
    return {
      // 흐림 빌드(외부 공개·캡처용): 원본 쪽 이미지는 서버에서 흐린 것만 내보낸다 (CSS 흐림은 이미지 주소로 우회 가능)
      beforeFiles:
        process.env.NEXT_PUBLIC_MASK_IMAGES === "1"
          ? [{ source: "/api/head/:vol/:page", destination: "/masked/head/:vol/:page" }]
          : [],
      afterFiles: [],
      fallback: [{ source: "/api/:path*", destination: `${DOCFLOW_URL}/:path*` }],
    };
  },
};

export default nextConfig;
