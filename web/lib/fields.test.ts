import { describe, expect, it } from "vitest";
import { parseFieldValue } from "./fields";
import { rupiah, rupiahCompact } from "./format";

describe("parseFieldValue", () => {
  it("parses Indonesian money formats to whole rupiah", () => {
    expect(parseFieldValue("money", "Rp 1.250.000")).toEqual({ ok: true, value: 1250000 });
    expect(parseFieldValue("money", "1.250.000,00")).toEqual({ ok: true, value: 1250000 });
    expect(parseFieldValue("money", "1250000")).toEqual({ ok: true, value: 1250000 });
  });

  it("rejects money it cannot read safely", () => {
    expect(parseFieldValue("money", "1,25 juta").ok).toBe(false);
    expect(parseFieldValue("money", "").ok).toBe(false);
  });

  it("keeps identifiers as digit strings, so leading zeros survive", () => {
    expect(parseFieldValue("digits", "0123 4567")).toEqual({ ok: true, value: "01234567" });
    expect(parseFieldValue("digits", "12O4").ok).toBe(false);
  });

  it("requires ISO dates and JSON line lists", () => {
    expect(parseFieldValue("date", "2025-03-05").ok).toBe(true);
    expect(parseFieldValue("date", "05/03/2025").ok).toBe(false);
    expect(parseFieldValue("lines", '[{"description":"x","qty":1,"unit_price":2,"amount":2}]').ok).toBe(true);
    expect(parseFieldValue("lines", '{"a":1}').ok).toBe(false);
  });
});

describe("rupiah", () => {
  it("formats with Indonesian separators and never invents zero", () => {
    expect(rupiah(1234567)).toBe("Rp 1.234.567");
    expect(rupiah(null)).toBe("—");
    // Spelled-out units: "M" would read as million to an English reader.
    expect(rupiahCompact(2_262_040_029)).toBe("Rp 2,3 miliar");
    expect(rupiahCompact(692_489_928)).toBe("Rp 692 juta");
  });
});
