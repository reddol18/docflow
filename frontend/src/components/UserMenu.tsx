"use client";

import { useEffect, useState } from "react";
import { usePathname } from "next/navigation";
import { api, loginUrl, ROLE_LABEL, type Me } from "@/lib/api";

// 헤더 오른쪽: 로그인한 사람과 로그아웃. 로그인 안 했으면(401) 로그인 화면으로 보낸다.
export function UserMenu() {
  const path = usePathname();
  const [me, setMe] = useState<Me | null>(null);

  useEffect(() => {
    if (path === "/login") return;
    let alive = true;
    api
      .me()
      .then((r) => alive && setMe(r.user))
      .catch(() => alive && window.location.assign(loginUrl()));
    return () => {
      alive = false;
    };
  }, [path]);

  if (path === "/login" || !me) return null;
  return (
    <div className="flex items-center gap-3 text-sm">
      <span className="text-slate-700">
        {me.name}
        <span className="ml-1 rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-500">{ROLE_LABEL[me.role]}</span>
      </span>
      <button
        onClick={async () => {
          await api.logout().catch(() => {});
          // eslint-disable-next-line @next/next/no-location-assign-relative-destination -- 화면 상태를 모두 버리려고 새로 불러온다
          window.location.assign("/login");
        }}
        className="text-slate-500 hover:text-slate-900"
      >
        로그아웃
      </button>
    </div>
  );
}
