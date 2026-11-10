// tls-proxy.mjs 시험 (https://github.com/reddol18/docflow/issues/48) — node --test. TLS 층은 node 것이라 처리기는 http로 띄워 본다.
import assert from "node:assert/strict";
import http from "node:http";
import net from "node:net";
import { after, before, test } from "node:test";
import { HSTS, LIMITS, PUBLIC_HOST, createServer, makeHandler } from "./tls-proxy.mjs";

let upstream, proxy, upPort, port;
let slowClosed = false;
const seen = [];

before(async () => {
  upstream = http.createServer((req, res) => {
    seen.push(req.headers);
    if (req.url === "/slow") { // 응답을 늦게 — 그 사이 클라이언트가 끊는다
      req.socket.on("close", () => { slowClosed = true; });
      return;
    }
    if (req.url === "/cut") { // 헤더를 보내고 본문 중간에 끊긴 뒤(Next가 죽음)
      res.writeHead(200, { "content-type": "text/plain" });
      res.write("part");
      setTimeout(() => res.socket.destroy(), 20);
      return;
    }
    res.writeHead(200, { "content-type": "text/plain", "set-cookie": "__Host-docflow_session=t; Path=/; Secure",
      connection: "keep-alive", "keep-alive": "timeout=99", "x-up": "1" });
    res.end(`ok ${req.method} ${req.url}`);
  });
  await new Promise((r) => upstream.listen(0, "127.0.0.1", r));
  upPort = upstream.address().port;
  proxy = http.createServer(makeHandler({ upstream: { host: "127.0.0.1", port: upPort } }));
  await new Promise((r) => proxy.listen(0, "127.0.0.1", r));
  port = proxy.address().port;
});
after(() => { // 실패해 남은 연결이 있어도 시험이 끝나게
  proxy.closeAllConnections(); upstream.closeAllConnections();
  proxy.close(); upstream.close();
});

function get(path, headers = {}) {
  return new Promise((resolve, reject) => {
    const r = http.request({ host: "127.0.0.1", port, path, headers: { host: PUBLIC_HOST, ...headers } }, (res) => {
      let body = "";
      res.setEncoding("utf8");
      res.on("data", (c) => { body += c; });
      res.on("end", () => resolve({ status: res.statusCode, headers: res.headers, body, complete: res.complete }));
      res.on("error", () => resolve({ status: res.statusCode, headers: res.headers, body, complete: false }));
      res.on("aborted", () => resolve({ status: res.statusCode, headers: res.headers, body, complete: false }));
    });
    r.on("error", reject);
    r.end();
  });
}

test("HSTS·응답 헤더 그대로, hop 헤더는 빼고 넘긴다", async () => {
  const r = await get("/login");
  assert.equal(r.status, 200);
  assert.equal(r.headers["strict-transport-security"], HSTS);
  assert.deepEqual(r.headers["set-cookie"], ["__Host-docflow_session=t; Path=/; Secure"]);
  assert.equal(r.headers["x-up"], "1");
  assert.notEqual(r.headers["keep-alive"], "timeout=99"); // 뒤의 hop 헤더가 아니라 이 서버 자신의 값
});

test("클라이언트가 보낸 X-Forwarded-*·Forwarded·X-Real-IP·hop 헤더는 지운다", async () => {
  seen.length = 0;
  await get("/x", { "x-forwarded-for": "1.2.3.4", "x-forwarded-host": "evil.test", forwarded: "for=1.2.3.4",
    "x-real-ip": "1.2.3.4", "proxy-authorization": "Basic eA==", te: "trailers", cookie: "a=b" });
  const h = seen[0];
  for (const k of ["x-forwarded-for", "x-forwarded-host", "forwarded", "x-real-ip", "proxy-authorization", "te"]) {
    assert.equal(h[k], undefined, k);
  }
  assert.equal(h["x-forwarded-proto"], "https");
  assert.equal(h.cookie, "a=b");
  assert.equal(h.host, PUBLIC_HOST); // Next가 보는 Host는 원래 이름
});

test("다른 Host는 421 (뒤로 넘기지 않음)", async () => {
  seen.length = 0;
  for (const host of ["localhost:8443", "10.42.0.9:8443", "docflow.localhost", "evil.test"]) {
    const r = await get("/login", { host });
    assert.equal(r.status, 421, host);
    assert.equal(r.headers["strict-transport-security"], HSTS);
  }
  assert.equal(seen.length, 0);
  assert.equal((await get("/login", { host: "DOCFLOW.localhost:8443" })).status, 200); // 대소문자는 같은 이름
});

test("Upgrade(웹소켓·HMR)는 받지 않는다", async () => {
  seen.length = 0;
  const r = await get("/_next/webpack-hmr", { connection: "Upgrade", upgrade: "websocket", "sec-websocket-version": "13",
    "sec-websocket-key": "dGhlIHNhbXBsZSBub25jZQ==" });
  assert.equal(r.status, 400);
  assert.equal(seen.length, 0);
});

test("뒤가 죽어 있으면 502, 응답 도중 끊기면 연결을 끊는다(글을 덧붙이지 않음)", async () => {
  const cut = await get("/cut");
  assert.equal(cut.complete, false);
  assert.ok(!cut.body.includes("bad gateway"));
  const dead = http.createServer(makeHandler({ upstream: { host: "127.0.0.1", port: await freePort() } }));
  await new Promise((r) => dead.listen(0, "127.0.0.1", r));
  const p = port;
  port = dead.address().port;
  try {
    const r = await get("/login");
    assert.equal(r.status, 502);
    assert.equal(r.headers["strict-transport-security"], HSTS);
  } finally { port = p; dead.close(); }
});

test("응답 전에 클라이언트가 끊으면 뒤(upstream) 연결도 닫는다", async () => {
  slowClosed = false;
  const r = http.request({ host: "127.0.0.1", port, path: "/slow", headers: { host: PUBLIC_HOST } });
  r.on("error", () => {});
  r.end();
  await new Promise((ok) => setTimeout(ok, 50)); // 뒤가 요청을 받을 때까지
  r.destroy();
  for (let i = 0; i < 50 && !slowClosed; i++) await new Promise((ok) => setTimeout(ok, 20));
  assert.equal(slowClosed, true);
});

test("서버 시간 제한·연결 수", () => {
  const s = createServer(undefined, undefined);
  for (const [k, v] of Object.entries(LIMITS)) assert.equal(s[k], v, k);
  assert.ok(LIMITS.requestTimeout >= 300_000 && LIMITS.headersTimeout <= 60_000);
});

function freePort() {
  return new Promise((resolve) => {
    const s = net.createServer();
    s.listen(0, "127.0.0.1", () => { const p = s.address().port; s.close(() => resolve(p)); });
  });
}
