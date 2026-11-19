/**
 * Where to send the user after sign-in. Only a path on this site: anything else
 * (another host, `//evil.example`, `/\evil.example`) falls back to the queue, so the
 * login flow cannot be used as an open redirect.
 */
export function safeReturnTo(value: string | null | undefined): string {
  if (!value || !value.startsWith("/") || value.startsWith("//") || value.includes("\\")) return "/";
  if (value.startsWith("/auth/") || value.startsWith("/login")) return "/";
  return value;
}
