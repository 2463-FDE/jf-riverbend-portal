import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  return proxy(req, `/eligibility/jobs/${encodeURIComponent(id)}`);
}
