/** Join class names, dropping falsy entries (same helper as TokenWatch). */
export function cn(...parts: Array<string | false | null | undefined>): string {
  return parts.filter(Boolean).join(" ");
}
