import type { Role } from "./types";

/**
 * Demo identities. DEV ONLY: the API trusts X-User / X-Role headers until OIDC
 * (Keycloak) lands in phase 2. Switching user here is how the demo shows that the
 * person who corrects an invoice cannot also approve it.
 */
export interface DemoUser {
  name: string;
  display: string;
  role: Role;
}

export const USERS: DemoUser[] = [
  { name: "budi", display: "Budi", role: "ap_staff" },
  { name: "dewi", display: "Dewi", role: "ap_staff" },
  { name: "sari", display: "Sari", role: "approver" },
  { name: "rina", display: "Rina", role: "approver" },
];

export const ROLE_LABEL: Record<Role, string> = {
  ap_staff: "AP staff",
  approver: "Approver",
};

export const USER_COOKIE = "faktura_user";

export function findUser(name: string | undefined): DemoUser {
  return USERS.find((u) => u.name === name) ?? USERS[0];
}
