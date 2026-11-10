import { NextResponse, type NextRequest } from "next/server";

// 외부 공개(터널)용 서버에서만 켠다. 환경변수가 없는 로컬 개발 서버는 그대로 통과.
const AUTH = process.env.DOCFLOW_BASIC_AUTH; // "user:pass"
const READONLY = process.env.DOCFLOW_READONLY === "1";
const DEV = process.env.NODE_ENV === "development";

// 응답 보안 헤더 (https://github.com/reddol18/docflow/issues/47, ASVS V14.4.3·V14.4.4·V14.4.7·V8.2.1)
// 스크립트는 요청마다 새 nonce로만 (Next가 자기 스크립트에 붙인다 — 그래서 레이아웃은 매 요청 렌더링).
// 스타일은 'unsafe-inline': 진행 막대 등 React style 속성이 서버 HTML의 style="…"로 나가기 때문 (스크립트 실행은 못 함).
// 'unsafe-eval'은 개발 서버(next dev)에만 — k8s web 파드는 운영 빌드(next start, NODE_ENV=production)라 없다 (https://github.com/reddol18/docflow/issues/48).
// upgrade-insecure-requests는 넣지 않는다 — 이 PC의 개발 서버는 http다. https 쪽은 TLS 종단(frontend/tls-proxy.mjs)이 HSTS를 붙인다.
function csp(nonce: string, dev = DEV): string {
  return [
    "default-src 'self'",
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'${dev ? " 'unsafe-eval'" : ""}`,
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' blob: data:",
    "font-src 'self'",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
  ].join("; ");
}

// 모든 응답에 같은 기본 헤더. 외부(docflow)로 넘기는 /api/* rewrite에는 실리지 않고 docflow 헤더가 그대로 가지만,
// Next 안에서 답하는 /api/* (흐림 이미지 /masked/head)와 여기서 막은 401·403에는 이것이 붙는다
const SECURITY_HEADERS: Record<string, string> = {
  "X-Content-Type-Options": "nosniff",
  "X-Frame-Options": "DENY",
  "Cache-Control": "no-store", // 색인·쪽 이미지가 보이는 화면도 브라우저·중간 캐시에 남기지 않는다
};
const NO_CONTENT_CSP = "default-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'";

function secure(res: NextResponse, policy = NO_CONTENT_CSP): NextResponse {
  for (const [k, v] of Object.entries(SECURITY_HEADERS)) res.headers.set(k, v);
  res.headers.set("Content-Security-Policy", policy);
  return res;
}

export function proxy(req: NextRequest) {
  const path = req.nextUrl.pathname;
  const api = path.startsWith("/api/");
  if (AUTH && req.headers.get("authorization") !== `Basic ${btoa(AUTH)}`) {
    return secure(new NextResponse("Authentication required", {
      status: 401,
      headers: { "WWW-Authenticate": 'Basic realm="docflow", charset="UTF-8"' },
    }));
  }
  // 업로드·실행·검토 저장을 막는다 (OpenAI 비용·데이터 변경). 검색·답변(GET)과 로그인·로그아웃은 열어 둔다.
  const authPath = path === "/api/auth/login" || path === "/api/auth/logout";
  if (READONLY && api && !authPath && req.method !== "GET" && req.method !== "HEAD") {
    return secure(NextResponse.json({ detail: "외부 접속은 읽기 전용입니다" }, { status: 403 }));
  }
  if (api) return secure(NextResponse.next()); // docflow로 넘기면 docflow 헤더가, Next가 답하면(/masked/head) 이것이 간다
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const policy = csp(nonce);
  const headers = new Headers(req.headers);
  headers.set("x-nonce", nonce);
  headers.set("Content-Security-Policy", policy); // Next가 여기서 nonce를 읽어 자기 스크립트에 붙인다
  return secure(NextResponse.next({ request: { headers } }), policy);
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
