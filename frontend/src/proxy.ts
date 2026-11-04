import { NextResponse, type NextRequest } from "next/server";

// 외부 공개(터널)용 서버에서만 켠다. 환경변수가 없는 로컬 개발 서버는 그대로 통과.
const AUTH = process.env.DOCFLOW_BASIC_AUTH; // "user:pass"
const READONLY = process.env.DOCFLOW_READONLY === "1";

export function proxy(req: NextRequest) {
  if (AUTH && req.headers.get("authorization") !== `Basic ${btoa(AUTH)}`) {
    return new NextResponse("Authentication required", {
      status: 401,
      headers: { "WWW-Authenticate": 'Basic realm="docflow", charset="UTF-8"' },
    });
  }
  const path = req.nextUrl.pathname;
  // 업로드·실행·검토 저장을 막는다 (OpenAI 비용·데이터 변경). 검색·답변(GET)과 로그인·로그아웃은 열어 둔다.
  const authPath = path === "/api/auth/login" || path === "/api/auth/logout";
  if (READONLY && path.startsWith("/api/") && !authPath && req.method !== "GET" && req.method !== "HEAD") {
    return NextResponse.json({ detail: "외부 접속은 읽기 전용입니다" }, { status: 403 });
  }
  return NextResponse.next();
}

export const config = {
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
