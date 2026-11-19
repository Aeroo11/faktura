import { LogOut } from "lucide-react";
import { signedInUser } from "@/lib/api";
import { ROLE_LABEL } from "@/lib/users";

/** Who is signed in (from the Keycloak token) and a sign-out button. */
export async function UserMenu() {
  const user = await signedInUser();
  if (!user) return null;
  return (
    <div className="flex items-center gap-3">
      <p className="text-sm">
        <span className="font-medium" data-testid="signed-in-user">
          {user.display}
        </span>{" "}
        <span className="text-text-muted">· {ROLE_LABEL[user.role]}</span>
      </p>
      <form action="/auth/logout" method="post">
        <button
          type="submit"
          className="inline-flex items-center gap-1 rounded-control border border-border px-2 py-1 text-sm text-text-muted hover:text-text"
        >
          <LogOut size={14} aria-hidden /> Sign out
        </button>
      </form>
    </div>
  );
}
