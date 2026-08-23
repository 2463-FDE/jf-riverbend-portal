import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

export async function POST(
  req: NextRequest,
  { params }: { params: Promise<{ id: string; coverageId: string }> }
) {
  const { id, coverageId } = await params;
  return proxy(
    req,
    `/patients/${encodeURIComponent(id)}/coverages/${encodeURIComponent(coverageId)}/eligibility-retry`,
    { method: "POST", body: {} }
  );
}
