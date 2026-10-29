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
    return [{ source: "/api/:path*", destination: `${DOCFLOW_URL}/:path*` }];
  },
};

export default nextConfig;
