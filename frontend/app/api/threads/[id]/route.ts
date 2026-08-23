import { NextRequest } from "next/server";
import { proxy } from "@/app/lib/gateway";

// One thread and its full message history. Reading it also advances the
// caller's own read position server-side — see records-service's get_thread.
export async function GET(
  req: NextRequest,
  { params }: { params: Promise<{ id: string }> }
) {
  const { id } = await params;
  return proxy(req, `/threads/${encodeURIComponent(id)}`);
}
