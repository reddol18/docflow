import { maskedImage } from "@/lib/masked";

// 흐림 빌드에서 next.config.ts가 /api/crops/* (잘라 둔 손글씨 칸·수신·제목 줄) 를 여기로 돌린다.
// 수신 줄에는 사람 이름이 들어갈 수 있어 원본을 내보내지 않는다 (https://github.com/reddol18/docflow/issues/35).
// 이름 검사는 docflow(/crops, app/main.py CROP_RX)가 하고, 여기서도 같은 꼴만 넘긴다
const NAME = /^[\w.-]+_(docno|date|recipient|title)\.png$/;

export async function GET(req: Request, { params }: { params: Promise<{ vol: string; name: string }> }) {
  const { vol, name } = await params;
  if (!NAME.test(name)) return new Response(null, { status: 400 });
  return maskedImage(req, `/crops/${encodeURIComponent(vol)}/${encodeURIComponent(name)}`, 6);
}
