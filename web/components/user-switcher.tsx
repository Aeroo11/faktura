"use client";

import { useRef } from "react";
import { switchUser } from "@/app/actions";
import { ROLE_LABEL, USERS } from "@/lib/users";

/** DEV ONLY identity picker: the API trusts this until SSO arrives in phase 2. */
export function UserSwitcher({ current }: { current: string }) {
  const form = useRef<HTMLFormElement>(null);
  return (
    <form ref={form} action={switchUser} className="flex items-center gap-2">
      <label htmlFor="user" className="text-xs text-text-muted">
        Signed in as <span className="rounded bg-warning-soft px-1 text-warning">demo</span>
      </label>
      <select
        id="user"
        name="user"
        defaultValue={current}
        onChange={() => form.current?.requestSubmit()}
        className="rounded-control border border-border bg-surface px-2 py-1 text-sm"
      >
        {USERS.map((u) => (
          <option key={u.name} value={u.name}>
            {u.display} · {ROLE_LABEL[u.role]}
          </option>
        ))}
      </select>
    </form>
  );
}
