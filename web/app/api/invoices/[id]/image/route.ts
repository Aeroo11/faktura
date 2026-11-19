import { apiFetch } from "@/lib/api";

/** Proxy the rendered invoice page, so the browser never calls FastAPI directly. */
export async function GET(_req: Request, { params }: { params: Promise<{ id: string }> }) {
  const { id } = await params;
  const res = await apiFetch(`/invoices/${encodeURIComponent(id)}/image`);
  if (!res.ok) return new Response("not found", { status: res.status });
  return new Response(res.body, {
    headers: { "Content-Type": "image/png", "Cache-Control": "private, max-age=300" },
  });
}
