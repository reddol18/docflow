import { maskedImage } from "@/lib/masked";

// 흐림 빌드에서 next.config.ts가 /api/head/* 를 여기로 돌린다. 실명·직인이 읽히지 않게 서버에서 흐린다.
export async function GET(req: Request, { params }: { params: Promise<{ vol: string; page: string }> }) {
  const { vol, page } = await params;
  return maskedImage(req, `/head/${encodeURIComponent(vol)}/${encodeURIComponent(page)}`, 3);
}
