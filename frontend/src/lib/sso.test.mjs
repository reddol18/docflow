// node --test src/lib/sso.test.mjs — 로그인 화면 SSO 버튼 판단 (https://github.com/reddol18/docflow/issues/39)
import assert from "node:assert/strict";
import { test } from "node:test";
import { ssoView } from "./sso.ts";

const DEV = "http://localhost:3000";
const WEB = "https://docflow.localhost:8443";

test("콜백과 같은 출처면 버튼", () => {
  assert.deepEqual(ssoView(["local", "oidc"], DEV, DEV), { button: true, elsewhere: null });
});

test("다른 출처면 버튼 대신 SSO 출처 안내 (로컬 로그인이 없어도 빈 화면이 아니게 elsewhere를 준다)", () => {
  assert.deepEqual(ssoView(["local", "oidc"], DEV, WEB), { button: false, elsewhere: DEV });
  assert.deepEqual(ssoView(["oidc"], DEV, WEB), { button: false, elsewhere: DEV });
});

test("oidc_origin을 모르면 예전처럼 버튼", () => {
  assert.deepEqual(ssoView(["oidc"], null, WEB), { button: true, elsewhere: null });
  assert.deepEqual(ssoView(["local", "oidc"], undefined, WEB), { button: true, elsewhere: null });
});

test("oidc가 꺼져 있으면 아무것도", () => {
  assert.deepEqual(ssoView(["local"], DEV, WEB), { button: false, elsewhere: null });
  assert.deepEqual(ssoView(undefined, DEV, WEB), { button: false, elsewhere: null });
});
