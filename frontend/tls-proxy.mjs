// web 파드의 TLS 종단 (https://github.com/reddol18/docflow/issues/48). 시험: frontend/tls-proxy.test.mjs (node --test, scripts/check.ps1)
// 맥 colima k3s에는 인그레스 컨트롤러(traefik)가 꺼져 있어(2026-11-10 확인) 새 이미지를 들이지 않고 프런트 이미지의 node로 받는다.
//   https://docflow.localhost:8443 (TLS 1.2 이상, 인증서는 Secret tls-web) → http://127.0.0.1:3000 (같은 파드의 next start)
// 모든 응답에 HSTS를 붙인다 — docflow 응답(/api/*)도 Next를 거쳐 여기로 온다.
// 이름이 docflow.localhost라 HSTS가 localhost(개발 서버 3000)에는 걸리지 않는다.
// Host가 PUBLIC_HOST가 아니면 421 (다른 이름으로 온 요청·DNS 리바인딩을 받지 않는다). 이 PC에서 다른 로컬 포트로 터널을 열면 421 —
// 8443 그대로 쓴다 (ssh -L 8443:127.0.0.1:8443).
// 하지 않는 것: HTTP/2, 웹소켓(Upgrade는 받지 않음 — 운영 빌드에는 HMR이 없다), 인증서 자동 재적재(바꾸면 파드를 다시 띄운다).
// 설정은 이 파일 한 곳 (k8s/docs.yaml은 명령만).
import fs from "node:fs";
import http from "node:http";
import https from "node:https";
import { pipeline } from "node:stream";

export const PUBLIC_HOST = "docflow.localhost:8443";
const PORT = 8443;
const UPSTREAM = { host: "127.0.0.1", port: 3000 };
const CERT = "/tls/tls.crt";
const KEY = "/tls/tls.key";
export const HSTS = "max-age=31536000";
// 시간 제한: 요청 하나를 다 받기까지 300초 — 뒤의 next start(Node 기본 server.requestTimeout 300초, Next가 바꾸지 않음)와 같게.
// 큰 업로드(요청 본문 최대 UPLOAD_MAX_BODY_MB 210MB, app/upload.py)는 300초 안에 끝나야 한다 = 초당 약 0.7MB 이상 (ssh 터널·LAN이면 넉넉).
// 머리 헤더는 30초(느린 헤더로 연결 붙잡기 막기), 동시 연결 256 (파드 메모리 128Mi 안)
export const LIMITS = { requestTimeout: 300_000, headersTimeout: 30_000, keepAliveTimeout: 5_000, maxConnections: 256 };
// 홉 단위 헤더는 넘기지 않는다 (RFC 9110 7.6.1)
const HOP = new Set(["connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "te", "trailer",
  "proxy-authenticate", "proxy-authorization"]);
// 클라이언트가 보낸 전달 헤더는 지운다 — 뒤(Next·docflow)가 믿을 만한 값처럼 보이지 않게
const FORWARDED = /^(x-forwarded-.*|forwarded|x-real-ip)$/;
const PLAIN = { "content-type": "text/plain; charset=utf-8", "strict-transport-security": HSTS,
  "x-content-type-options": "nosniff", "cache-control": "no-store" };

export function upstreamHeaders(headers) {
  const out = {};
  for (const [k, v] of Object.entries(headers)) {
    if (!HOP.has(k) && !FORWARDED.test(k)) out[k] = v;
  }
  out["x-forwarded-proto"] = "https";
  return out;
}

export function downstreamHeaders(headers) {
  const out = {};
  for (const [k, v] of Object.entries(headers)) {
    if (!HOP.has(k)) out[k] = v;
  }
  out["strict-transport-security"] = HSTS;
  return out;
}

export function makeHandler({ upstream = UPSTREAM, publicHost = PUBLIC_HOST } = {}) {
  return (req, res) => {
    if ((req.headers.host ?? "").toLowerCase() !== publicHost) {
      res.writeHead(421, PLAIN);
      res.end("misdirected request");
      return;
    }
    if (req.headers.upgrade) { // 웹소켓 등 — 운영 빌드에는 쓰는 곳이 없다
      res.writeHead(400, PLAIN);
      res.end("upgrade not supported");
      return;
    }
    const up = http.request({ ...upstream, method: req.method, path: req.url, headers: upstreamHeaders(req.headers) }, (ur) => {
      res.writeHead(ur.statusCode ?? 502, downstreamHeaders(ur.headers));
      // 응답 도중 끊기면 연결을 끊는다 (본문 뒤에 다른 글을 붙이지 않는다 — 잘린 응답이 온전해 보이지 않게)
      pipeline(ur, res, (err) => { if (err) res.destroy(); });
    });
    up.on("error", () => {
      if (res.headersSent) {
        res.destroy();
      } else {
        res.writeHead(502, PLAIN);
        res.end("bad gateway");
      }
    });
    // 응답을 다 보내기 전에 클라이언트가 끊으면(응답 헤더 전 포함) 뒤 연결도 닫는다
    res.on("close", () => { if (!res.writableFinished) up.destroy(); });

    pipeline(req, up, (err) => { if (err) up.destroy(); });
  };
}

export function createServer(cert, key, opts = {}) {
  const server = https.createServer({ cert, key, minVersion: "TLSv1.2" }, makeHandler(opts));
  Object.assign(server, LIMITS);
  return server;
}

if (process.argv[1]?.endsWith("tls-proxy.mjs")) { // 직접 실행할 때만 듣는다 (시험은 불러서 쓴다)
  const server = createServer(fs.readFileSync(CERT), fs.readFileSync(KEY));
  server.listen(PORT, "0.0.0.0", () => console.log(`tls-proxy: https://${PUBLIC_HOST} → http://127.0.0.1:${UPSTREAM.port}`));
  for (const sig of ["SIGTERM", "SIGINT"]) process.on(sig, () => server.close(() => process.exit(0)));
}
