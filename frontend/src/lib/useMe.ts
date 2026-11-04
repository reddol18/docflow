"use client";

import { useEffect, useState } from "react";
import { api, ApiError, loginUrl, type Me } from "@/lib/api";

// 로그인한 사람. 한 화면 안에서 여러 컴포넌트가 불러도 /auth/me는 한 번만 부른다.
let pending: Promise<Me> | null = null;

export function loadMe(): Promise<Me> {
  pending ??= api.me().then(
    (r) => r.user,
    (e) => {
      pending = null; // 다음에 다시 시도
      throw e;
    },
  );
  return pending;
}

export function forgetMe() {
  pending = null;
}

// 화면이 버튼을 숨기는 데만 쓴다. 실제 권한은 서버가 강제한다 (app/auth.py).
export function useMe(): Me | null {
  const [me, setMe] = useState<Me | null>(null);
  useEffect(() => {
    let alive = true;
    loadMe()
      .then((m) => alive && setMe(m))
      .catch((e) => {
        // 로그인이 풀린 경우(401)만 로그인 화면으로. 서버 재시작·네트워크 오류는 그대로 둔다
        if (alive && e instanceof ApiError && e.status === 401) window.location.assign(loginUrl());
      });
    return () => {
      alive = false;
    };
  }, []);
  return me;
}
