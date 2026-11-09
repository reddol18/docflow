import sharp from "sharp";

// 흐림 빌드(NEXT_PUBLIC_MASK_IMAGES=1)에서 next.config.ts가 원본 이미지 경로(/api/head·/api/crops)를 /masked/* route로 돌린다.
// 그 route들이 docflow에서 원본을 받아 여기서 흐린 PNG만 내보낸다 — CSS 흐림은 이미지 주소를 직접 열면 우회되므로
// 실명·직인이 든 이미지(머리부, 수신 줄 등)는 서버에서 흐린다 (https://github.com/reddol18/docflow/issues/35)
const DOCFLOW_URL = process.env.DOCFLOW_URL ?? "http://127.0.0.1:8000";

// 흐림 세기: 이미지 너비의 1/120, 최소 minSigma (잘라 둔 줄처럼 작은 이미지도 글자가 안 읽히게)
export const blurSigma = (width: number, minSigma: number) => Math.max(minSigma, width / 120);

export async function maskedImage(req: Request, path: string, minSigma: number): Promise<Response> {
  // 로그인 쿠키를 그대로 넘겨 docflow가 권한을 확인하게 한다 (https://github.com/reddol18/docflow/issues/6)
  const headers: Record<string, string> = {};
  const cookie = req.headers.get("cookie");
  if (cookie) headers.cookie = cookie;
  const r = await fetch(`${DOCFLOW_URL}${path}`, { cache: "no-store", headers });
  if (!r.ok) return new Response(null, { status: r.status });
  const img = sharp(Buffer.from(await r.arrayBuffer()));
  const { width = 1000 } = await img.metadata();
  const out = await img.blur(blurSigma(width, minSigma)).png().toBuffer();
  return new Response(new Uint8Array(out), {
    headers: { "Content-Type": "image/png", "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff" },
  });
}
