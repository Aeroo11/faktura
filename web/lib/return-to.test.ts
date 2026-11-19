import { describe, expect, it } from "vitest";
import { safeReturnTo } from "./return-to";

describe("safeReturnTo", () => {
  it("keeps paths on this site", () => {
    expect(safeReturnTo("/invoices/inv-00002?tab=audit")).toBe("/invoices/inv-00002?tab=audit");
  });

  it.each([null, "", "https://evil.example/", "//evil.example", "/\\evil.example", "/auth/logout", "/login"])(
    "falls back to the queue for %s",
    (v) => {
      expect(safeReturnTo(v)).toBe("/");
    },
  );
});
