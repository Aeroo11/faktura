import type { Reading } from "./types";

export type FieldKind = "text" | "money" | "digits" | "date" | "lines";

export interface FieldDef {
  key: keyof Reading;
  label: string;
  kind: FieldKind;
}

/** Display order and input type for each extracted field. */
export const FIELDS: FieldDef[] = [
  { key: "vendor_name", label: "Vendor", kind: "text" },
  { key: "vendor_npwp", label: "NPWP", kind: "digits" },
  { key: "invoice_number", label: "Invoice number", kind: "text" },
  { key: "tax_invoice_number", label: "Tax invoice number", kind: "digits" },
  { key: "invoice_date", label: "Invoice date", kind: "date" },
  { key: "po_number", label: "PO number", kind: "text" },
  { key: "bank_name", label: "Bank", kind: "text" },
  { key: "bank_account", label: "Bank account", kind: "digits" },
  { key: "lines", label: "Line items", kind: "lines" },
  { key: "dpp", label: "DPP (tax base)", kind: "money" },
  { key: "vat", label: "VAT (PPN)", kind: "money" },
  { key: "total", label: "Total", kind: "money" },
];

export type ParseResult = { ok: true; value: unknown } | { ok: false; error: string };

/** Turn the text a person typed into the JSON value the API expects. Pure; unit-tested. */
export function parseFieldValue(kind: FieldKind, raw: string): ParseResult {
  const s = raw.trim();
  switch (kind) {
    case "money": {
      const digits = s.replace(/^rp\s*/i, "").replace(/[.\s]/g, "").replace(/,00$/, "");
      if (!/^\d+$/.test(digits)) return { ok: false, error: "Enter whole rupiah, e.g. 1.250.000" };
      return { ok: true, value: Number(digits) };
    }
    case "digits": {
      const digits = s.replace(/[\s.\-]/g, "");
      if (!/^\d+$/.test(digits)) return { ok: false, error: "Digits only" };
      return { ok: true, value: digits };
    }
    case "date":
      if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return { ok: false, error: "Use YYYY-MM-DD" };
      return { ok: true, value: s };
    case "lines":
      try {
        const v = JSON.parse(s);
        if (!Array.isArray(v)) return { ok: false, error: "Expected a JSON list of lines" };
        return { ok: true, value: v };
      } catch {
        return { ok: false, error: "Invalid JSON" };
      }
    default:
      if (!s) return { ok: false, error: "Value is empty" };
      return { ok: true, value: s };
  }
}
