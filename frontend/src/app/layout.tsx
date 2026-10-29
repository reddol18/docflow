import type { Metadata } from "next";
import Link from "next/link";
import "./globals.css";

export const metadata: Metadata = {
  title: "docflow — 공문서 색인",
  description: "스캔 공문서 OCR · 색인 추출 · 사람 검토 · 검색 (LangGraph)",
};

export default function RootLayout({ children }: LayoutProps<"/">) {
  return (
    <html lang="ko" className="h-full antialiased">
      <body className="min-h-full flex flex-col">
        <header className="border-b border-slate-200 bg-white">
          <div className="mx-auto flex max-w-7xl items-center gap-6 px-6 py-3">
            <Link href="/" className="text-lg font-bold tracking-tight">
              docflow <span className="text-sm font-normal text-slate-500">공문서 색인 플랫폼</span>
            </Link>
            <nav className="flex gap-4 text-sm text-slate-600">
              <Link href="/" className="hover:text-slate-900">대시보드</Link>
              <Link href="/search" className="hover:text-slate-900">검색</Link>
            </nav>
            <span className="ml-auto text-xs text-slate-400">PaddleOCR · LangGraph · Qdrant · GPT</span>
          </div>
        </header>
        <main className="mx-auto w-full max-w-7xl flex-1 px-6 py-6">{children}</main>
      </body>
    </html>
  );
}
