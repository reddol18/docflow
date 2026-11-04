import sharp from "sharp";

// proxy.ts가 외부 공개 서버에서 /api/head/* 를 여기로 돌린다. 실명·직인이 읽히지 않게 서버에서 흐린다.
const DOCFLOW_URL = process.env.DOCFLOW_URL ?? "http://127.0.0.1:8000";

export async function GET(req: Request, { params }: { params: Promise<{ vol: string; page: string }> }) {
  const { vol, page } = await params;
  // 로그인 쿠키를 그대로 넘겨 docflow가 권한을 확인하게 한다 (https://github.com/reddol18/docflow/issues/6)
  const headers: Record<string, string> = {};
  const cookie = req.headers.get("cookie");
  if (cookie) headers.cookie = cookie;
  const r = await fetch(`${DOCFLOW_URL}/head/${encodeURIComponent(vol)}/${encodeURIComponent(page)}`, {
    cache: "no-store",
    headers,
  });
  if (!r.ok) return new Response(null, { status: r.status });
  const img = sharp(Buffer.from(await r.arrayBuffer()));
  const { width = 1000 } = await img.metadata();
  const out = await img.blur(Math.max(3, width / 120)).png().toBuffer();
  return new Response(new Uint8Array(out), {
    headers: { "Content-Type": "image/png", "Cache-Control": "no-store" },
  });
}
