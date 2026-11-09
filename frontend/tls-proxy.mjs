// web 파드의 TLS 종단 (https://github.com/reddol18/docflow/issues/48).
// 맥 colima k3s에는 인그레스 컨트롤러(traefik)가 꺼져 있어(2026-11-10 확인) 새 이미지를 들이지 않고 프런트 이미지의 node로 받는다.
//   https://<이름>:8443 (TLS 1.2 이상, 인증서는 Secret tls-web) → http://127.0.0.1:3000 (같은 파드의 next start)
// 모든 응답에 HSTS를 붙인다 — docflow 응답(/api/*)도 Next를 거쳐 여기로 온다.
// 이름이 docflow.localhost라 HSTS가 localhost(개발 서버 3000)에는 걸리지 않는다.
// 하지 않는 것: HTTP/2, 웹소켓(Upgrade는 받지 않음 — 운영 빌드에는 HMR이 없다), 인증서 자동 재적재(바꾸면 파드를 다시 띄운다).
import fs from "node:fs";
import http from "node:http";
import https from "node:https";

const PORT = Number(process.env.TLS_PORT ?? 8443);
const UPSTREAM = { host: "127.0.0.1", port: Number(process.env.UPSTREAM_PORT ?? 3000) };
export const HSTS = "max-age=31536000";
// 홉 단위 헤더는 넘기지 않는다 (RFC 9110 7.6.1)
const HOP = new Set(["connection", "keep-alive", "proxy-connection", "transfer-encoding", "upgrade", "te", "trailer",
  "proxy-authenticate", "proxy-authorization"]);
// 클라이언트가 보낸 전달 헤더는 지운다 — 뒤(Next·docflow)가 믿을 만한 값처럼 보이지 않게
const FORWARDED = /^(x-forwarded-.*|forwarded|x-real-ip)$/;

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

export function handler(req, res) {
  const up = http.request({ ...UPSTREAM, method: req.method, path: req.url, headers: upstreamHeaders(req.headers) }, (ur) => {
    res.writeHead(ur.statusCode ?? 502, downstreamHeaders(ur.headers));
    ur.pipe(res);
  });
  up.on("error", () => {
    if (!res.headersSent) {
      res.writeHead(502, { "content-type": "text/plain; charset=utf-8", "strict-transport-security": HSTS,
        "x-content-type-options": "nosniff", "cache-control": "no-store" });
    }
    res.end("bad gateway");
  });
  res.on("close", () => up.destroy());
  req.pipe(up);
}

export function createServer(cert, key) {
  const server = https.createServer({ cert, key, minVersion: "TLSv1.2" }, handler);
  server.requestTimeout = 15 * 60 * 1000; // 큰 업로드(요청 본문 210MB까지, app/upload.py)가 느린 터널로 올 때
  return server;
}

if (process.argv[1]?.endsWith("tls-proxy.mjs")) { // 직접 실행할 때만 듣는다 (시험은 불러서 쓴다)
  const server = createServer(fs.readFileSync(process.env.TLS_CERT ?? "/tls/tls.crt"),
    fs.readFileSync(process.env.TLS_KEY ?? "/tls/tls.key"));
  server.listen(PORT, "0.0.0.0", () => console.log(`tls-proxy: https :${PORT} → http://127.0.0.1:${UPSTREAM.port}`));
  for (const sig of ["SIGTERM", "SIGINT"]) process.on(sig, () => server.close(() => process.exit(0)));
}
