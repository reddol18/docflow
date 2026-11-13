"use client";

import { Suspense, useEffect, useState } from "react";
import { useSearchParams } from "next/navigation";
import { api, oidcLoginUrl, type AuthConfig } from "@/lib/api";
import { ssoView } from "@/lib/sso";

const ERRORS: Record<string, string> = {
  oidc: "SSO 로그인이 취소되었거나 실패했습니다.",
  role: "이 계정에는 docflow 권한이 없습니다. 관리자에게 문의하세요.",
};

// 같은 사이트의 상대 경로만 (서버의 auth.safe_next와 같은 규칙)
const CONTROL = /[\x00-\x1f]/;
const safeNext = (n: string | null) =>
  n && n.length <= 2048 && n.startsWith("/") && !n.startsWith("//") && !n.includes("\\") && !CONTROL.test(n) ? n : "/";

function LoginForm() {
  const q = useSearchParams();
  const next = safeNext(q.get("next"));
  const [cfg, setCfg] = useState<AuthConfig | null>(null);
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState<string | null>(ERRORS[q.get("error") ?? ""] ?? null);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    api
      .authConfig()
      .then((c) => {
        setCfg(c);
        if (c.off) window.location.assign(next); // 개발용 인증 끔
      })
      .catch(() => setError("서버에 연결할 수 없습니다."));
  }, [next]);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(username, password);
      window.location.assign(next);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
      setBusy(false);
    }
  };

  const local = cfg?.modes.includes("local");
  // SSO 버튼은 등록한 콜백과 같은 출처에서만 (lib/sso.ts, https://github.com/reddol18/docflow/issues/39).
  // cfg는 브라우저에서만 채워지므로 그때만 window를 읽는다
  const { button: oidc, elsewhere: oidcElsewhere } = ssoView(cfg?.modes, cfg?.oidc_origin, cfg ? window.location.origin : "");
  return (
    <div className="mx-auto mt-16 w-full max-w-sm rounded-xl border border-slate-200 bg-white p-6 shadow-sm">
      <h1 className="mb-5 text-lg font-semibold">로그인</h1>
      {error && <div className="mb-4 rounded-lg bg-rose-50 px-3 py-2 text-sm text-rose-700">{error}</div>}
      {oidc && (
        <a
          href={oidcLoginUrl(next)}
          className="block w-full rounded-md bg-slate-800 px-3 py-2 text-center text-sm font-medium text-white hover:bg-slate-700"
        >
          {cfg?.oidc_label ?? "SSO"}로 로그인
        </a>
      )}
      {oidc && local && <div className="my-4 text-center text-xs text-slate-400">또는</div>}
      {oidcElsewhere && (
        // 로컬 로그인이 없으면 이 안내가 화면의 전부 — 빈 화면 대신 눈에 띄게, SSO가 되는 주소로 가는 링크
        <div
          className={
            local
              ? "mb-4 text-center text-xs text-slate-400"
              : "rounded-lg bg-amber-50 px-3 py-3 text-center text-sm text-amber-800"
          }
        >
          SSO 로그인은{" "}
          <a href={`${oidcElsewhere}/login`} className="underline">
            {oidcElsewhere}
          </a>
          에서만 됩니다.
        </div>
      )}
      {local && (
        <form onSubmit={submit} className="space-y-3">
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            placeholder="아이디"
            autoComplete="username"
            autoFocus
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
          />
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            placeholder="비밀번호"
            autoComplete="current-password"
            className="w-full rounded-md border border-slate-300 px-3 py-2 text-sm"
          />
          <button
            type="submit"
            disabled={busy || !username || !password}
            className="w-full rounded-md bg-blue-600 px-3 py-2 text-sm font-medium text-white hover:bg-blue-500 disabled:opacity-50"
          >
            {busy ? "확인 중…" : "로그인"}
          </button>
        </form>
      )}
    </div>
  );
}

export default function LoginPage() {
  return (
    <Suspense>
      <LoginForm />
    </Suspense>
  );
}
