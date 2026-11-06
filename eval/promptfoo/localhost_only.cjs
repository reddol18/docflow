// promptfoo view(0.125.0)는 듣는 주소를 고를 수 없어 모든 인터페이스(::)에 연다 → 결과 DB(실제 문서 글자)가 LAN에 보인다.
// run.ps1 -View가 node --require로 먼저 읽혀, 주소 없이 포트만 준 listen을 127.0.0.1로 묶는다.
const net = require("net");
const listen = net.Server.prototype.listen;
net.Server.prototype.listen = function (...a) {
  if (typeof a[0] === "number" && (a.length === 1 || typeof a[1] === "function")) a.splice(1, 0, "127.0.0.1");
  return listen.apply(this, a);
};
