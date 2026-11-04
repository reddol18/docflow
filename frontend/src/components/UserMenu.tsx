"use client";

import { usePathname } from "next/navigation";
import { api, ROLE_LABEL } from "@/lib/api";
import { forgetMe, useMe } from "@/lib/useMe";

// 헤더 오른쪽: 로그인한 사람과 로그아웃. 로그인이 풀렸으면(401만) 로그인 화면으로 보낸다 (useMe).
export function UserMenu() {
  const path = usePathname();
  if (path === "/login") return null;
  return <Menu path={path} />;
}

function Menu({ path }: { path: string }) {
  const me = useMe(path);
  if (!me) return null;
  return (
    <div className="flex items-center gap-3 text-sm">
      <span className="text-slate-700">
        {me.name}
        <span className="ml-1 rounded bg-slate-100 px-1.5 py-0.5 text-[11px] text-slate-500">{ROLE_LABEL[me.role]}</span>
      </span>
      <button
        onClick={async () => {
          await api.logout().catch(() => {});
          forgetMe();
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
