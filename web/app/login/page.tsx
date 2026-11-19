import type { Metadata } from "next";
import { redirect } from "next/navigation";
import { LogIn } from "lucide-react";
import { signedInUser } from "@/lib/api";
import { safeReturnTo } from "@/lib/return-to";

export const metadata: Metadata = { title: "Sign in" };

const ERRORS: Record<string, string> = {
  role: "Your account has no Faktura role, or holds both AP staff and Approver. Ask an administrator to fix it in Keycloak.",
  expired: "Your session ended. Sign in again.",
  failed: "Sign-in did not complete. Try again.",
};

/** Local development realm only (keycloak/faktura-realm.json). */
const DEMO_ACCOUNTS = [
  { user: "budi", role: "AP staff" },
  { user: "dewi", role: "AP staff" },
  { user: "sari", role: "Approver" },
  { user: "rina", role: "Approver" },
  { user: "joko", role: "both roles: refused" },
];

export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ returnTo?: string; error?: string; signed_out?: string }>;
}) {
  const params = await searchParams;
  const returnTo = safeReturnTo(params.returnTo);
  if (await signedInUser()) redirect(returnTo);
  const error = params.error ? (ERRORS[params.error] ?? ERRORS.failed) : null;
  const showDemo = process.env.FAKTURA_SHOW_DEMO_ACCOUNTS === "1";

  return (
    <div className="mx-auto max-w-md space-y-6 pt-10">
      <div className="space-y-2">
        <h1 className="text-xl font-semibold tracking-tight">Sign in to Faktura</h1>
        <p className="text-sm text-text-muted">
          Sign-in goes through the company identity provider (Keycloak). Your role there decides
          whether you check invoices or approve payments.
        </p>
      </div>
      {params.signed_out && <p className="rounded-control bg-success-soft px-3 py-2 text-sm text-success">Signed out.</p>}
      {error && <p className="rounded-control bg-danger-soft px-3 py-2 text-sm text-danger">{error}</p>}
      <a
        href={`/auth/login?returnTo=${encodeURIComponent(returnTo)}`}
        className="inline-flex w-full items-center justify-center gap-2 rounded-control bg-accent px-4 py-2 text-sm font-medium text-white hover:opacity-90"
      >
        <LogIn size={16} aria-hidden /> Sign in with Keycloak
      </a>
      {showDemo && (
        <div className="rounded-control border border-border bg-surface p-4 text-sm">
          <p className="mb-2 font-medium">Demo accounts (local realm)</p>
          <table className="w-full text-left">
            <tbody>
              {DEMO_ACCOUNTS.map((a) => (
                <tr key={a.user} className="border-t border-border first:border-0">
                  <td className="py-1 font-mono">{a.user}</td>
                  <td className="py-1 font-mono text-text-muted">{a.user}-faktura-dev</td>
                  <td className="py-1 text-text-muted">{a.role}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}
