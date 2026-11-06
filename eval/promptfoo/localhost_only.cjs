// promptfoo view(0.125.0)는 듣는 주소를 고를 수 없어 모든 인터페이스(::)에 열고, cors()로 모든 출처를 허용하며 Host를 보지 않는다
// → 결과 DB(실제 문서 글자)가 LAN·다른 웹페이지(DNS 리바인딩 포함)에 보인다. run.ps1 -View가 node --require로 먼저 읽힌다.
//  1) 주소 없이 포트만 준 listen을 127.0.0.1로 묶는다
//  2) HTTP 요청·웹소켓 업그레이드(socket.io)마다 Host가 127.0.0.1:<포트>/localhost:<포트>가 아니거나,
//     Origin이 있는데 같은 출처(http://127.0.0.1:<포트>, http://localhost:<포트>)가 아니면 403
//  3) 응답의 Access-Control-Allow-Origin(cors()가 붙임)을 지운다 — 다른 출처 페이지가 응답을 읽지 못하게
const net = require("net");
const http = require("http");

const listen = net.Server.prototype.listen;
net.Server.prototype.listen = function (...a) {
  if (typeof a[0] === "number" && (a.length === 1 || typeof a[1] === "function")) a.splice(1, 0, "127.0.0.1");
  return listen.apply(this, a);
};

function allowed(req) {
  const port = req.socket && req.socket.localPort;
  const hosts = [`127.0.0.1:${port}`, `localhost:${port}`];
  const host = String(req.headers.host || "").toLowerCase();
  if (!hosts.includes(host)) return false;
  const origin = req.headers.origin;
  return origin === undefined || hosts.some((h) => String(origin).toLowerCase() === `http://${h}`);
}

function stripCors(res) {
  const isAcao = (k) => String(k).toLowerCase() === "access-control-allow-origin";
  const setHeader = res.setHeader;
  res.setHeader = function (k, v) {
    return isAcao(k) ? this : setHeader.call(this, k, v);
  };
  const writeHead = res.writeHead;
  res.writeHead = function (...a) {
    const h = a.find((x, i) => i > 0 && x && typeof x === "object" && !Array.isArray(x));
    if (h) for (const k of Object.keys(h)) if (isAcao(k)) delete h[k];
    return writeHead.apply(this, a);
  };
}

const emit = http.Server.prototype.emit;
http.Server.prototype.emit = function (ev, req, resOrSocket, ...rest) {
  if (ev === "request") {
    if (!allowed(req)) {
      resOrSocket.writeHead(403, { "Content-Type": "text/plain; charset=utf-8" });
      resOrSocket.end("forbidden: 127.0.0.1/localhost only\n");
      return true;
    }
    stripCors(resOrSocket);
  } else if (ev === "upgrade" && !allowed(req)) {
    resOrSocket.end("HTTP/1.1 403 Forbidden\r\nConnection: close\r\n\r\n");
    return true;
  }
  return emit.call(this, ev, req, resOrSocket, ...rest);
};
