// 로그인 화면의 SSO 버튼을 이 출처에서 보일지 (https://github.com/reddol18/docflow/issues/39).
// SSO는 등록한 콜백(OIDC_REDIRECT_URI)과 같은 출처에서만 된다 — 다른 출처(예: web 파드 https)에서 시작하면
// state 쿠키가 콜백 쪽에 없어 늘 실패한다. 서버가 oidc_origin을 주지 않으면(그 값을 모르는 서버) 예전처럼 버튼을 보인다.
// 시험: sso.test.mjs (node --test, 타입만 지우고 돈다 — 그래서 타입 말고는 일반 JS 문법만 쓴다)
export type SsoView = { button: boolean; elsewhere: string | null };

export function ssoView(modes: readonly string[] | undefined, oidcOrigin: string | null | undefined, here: string): SsoView {
  const on = !!modes && modes.includes("oidc");
  const elsewhere = on && oidcOrigin && oidcOrigin !== here ? oidcOrigin : null;
  return { button: on && !elsewhere, elsewhere };
}
