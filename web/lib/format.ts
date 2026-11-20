import type { BankChangeStatus, Decision, State } from "./types";

const IDR = new Intl.NumberFormat("id-ID", { maximumFractionDigits: 0 });

/** "Rp 1.234.567". Null renders as an em dash, never as "Rp 0". */
export function rupiah(n: number | null | undefined): string {
  return n == null ? "—" : `Rp ${IDR.format(n)}`;
}

/**
 * Compact form for stat tiles: "Rp 2,3 miliar", "Rp 692 juta".
 * Units are spelled out on purpose: the Indonesian "M" means miliar (10^9), but an
 * English reader takes "M" as million, a 1,000x misreading on a finance screen.
 */
export function rupiahCompact(n: number): string {
  const fmt = (v: number) =>
    new Intl.NumberFormat("id-ID", { maximumFractionDigits: v >= 100 ? 0 : 1 }).format(v);
  if (n >= 1e9) return `Rp ${fmt(n / 1e9)} miliar`;
  if (n >= 1e6) return `Rp ${fmt(n / 1e6)} juta`;
  return rupiah(n);
}

export const DECISION_LABEL: Record<Decision, string> = {
  approve: "Approve",
  review_reading: "Check reading",
  business_exception: "Business exception",
  fraud_hold: "Fraud hold",
};

export const STATE_LABEL: Record<State, string> = {
  needs_action: "Needs action",
  awaiting_approval: "Awaiting approval",
  approved: "Approved",
  rejected: "Rejected",
};

export const BANK_CHANGE_STATUS_LABEL: Record<BankChangeStatus, string> = {
  awaiting_callback: "Awaiting call-back",
  awaiting_approval: "Awaiting approval",
  approved: "Approved",
  rejected: "Rejected",
};

/** Bank account numbers in groups of four, easier to compare digit by digit. */
export function groupDigits(s: string): string {
  return s.replace(/(\d{4})(?=\d)/g, "$1 ");
}

/** Human wording for reason codes from the matching engine and validators. */
export const REASON_LABEL: Record<string, string> = {
  npwp_length: "NPWP has the wrong length",
  tax_invoice_length: "Tax invoice number has the wrong length",
  missing: "Field not found on the page",
  date_unparseable: "Date could not be read",
  no_lines: "No line items",
  line_amount: "Line qty × price ≠ amount",
  dpp_sum: "Lines do not add up to DPP",
  total_sum: "DPP + VAT ≠ total",
  po_unknown: "PO not found",
  vendor_unknown: "Vendor not identified",
  vendor_name: "Vendor name differs from master",
  invoice_number_shape: "Invoice number unlike this vendor's",
  date_before_po: "Invoice dated before its PO",
  lines_vs_po: "Lines not on the PO",
  price_vs_po: "Unit price differs from PO",
  qty_vs_receipt: "Billed more than received",
  qty_cumulative: "Earlier invoices + this exceed receipt",
  vat_rate: "VAT does not match the rate",
  npwp_master: "NPWP differs from vendor master",
  bank_master: "Bank account differs from vendor master",
  bank_name_master: "Bank differs from vendor master",
  probable_misread: "Looks like a misread: check digits on the page",
  duplicate_number: "Duplicate: same invoice number",
  duplicate_tax_invoice: "Duplicate: same tax invoice number",
  duplicate_po_total: "Duplicate: same PO and total",
  duplicate_amounts: "Duplicate: same amounts",
  model_uncertain: "Model marked fields as hard to read",
};

export function reasonLabel(code: string): string {
  return REASON_LABEL[code] ?? code.replaceAll("_", " ");
}

export function formatTimestamp(iso: string): string {
  return new Intl.DateTimeFormat("en-GB", {
    dateStyle: "medium",
    timeStyle: "medium",
    timeZone: "Asia/Jakarta",
  }).format(new Date(iso));
}
