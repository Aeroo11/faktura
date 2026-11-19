"use client";

import { Card } from "@/components/ui/card";

export default function ErrorPage({ error, reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <Card className="mx-auto max-w-lg text-sm">
      <h1 className="mb-2 font-semibold">The review queue could not load</h1>
      <p className="mb-4 text-text-muted">
        Is the Faktura API running? Start it with{" "}
        <code className="font-mono">uv run uvicorn faktura.app.api:app --port 8077</code>.
      </p>
      <p className="mb-4 font-mono text-xs text-text-subtle">{error.digest ?? error.message}</p>
      <button onClick={reset} className="rounded-control bg-accent px-3 py-1.5 text-white">
        Try again
      </button>
    </Card>
  );
}
