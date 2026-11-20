// Shapes returned by the Faktura FastAPI service (src/faktura/app/api.py).

export type State = "approved" | "needs_action" | "awaiting_approval" | "rejected";
export type Decision = "approve" | "review_reading" | "business_exception" | "fraud_hold";
export type Role = "ap_staff" | "approver";

export interface Reason {
  code: string;
  status: Decision;
  message: string;
  fields?: string[];
}

export interface InvoiceLine {
  description: string;
  qty: number;
  unit_price: number;
  amount: number;
}

export interface Reading {
  invoice_number: string | null;
  tax_invoice_number: string | null;
  invoice_date: string | null;
  po_number: string | null;
  vendor_name: string | null;
  vendor_npwp: string | null;
  bank_name: string | null;
  bank_account: string | null;
  lines: InvoiceLine[];
  dpp: number | null;
  vat: number | null;
  total: number | null;
  uncertain_fields: string[];
}

export interface InvoiceSummary {
  id: string;
  seq: number;
  state: State;
  decision: Decision;
  vendor_id: string | null;
  vendor_name: string | null;
  invoice_number: string | null;
  total: number | null;
  auto_approved: boolean;
  version: number;
  reason_codes: string[];
}

export interface AuditEntry {
  seq: number;
  at: string;
  actor: string;
  action: string;
  payload: Record<string, unknown>;
  hash: string;
}

export interface InvoiceDetail extends InvoiceSummary {
  reasons: Reason[];
  reading: Reading;
  model_reading: Reading;
  human_verified: string[];
  makers: string[];
  /** Posting to the ERP; null until the invoice is approved. */
  erp: ErpStatus | null;
  /** Latest bank-change request for the account printed on this invoice, if any. */
  bank_change: BankChange | null;
  audit: AuditEntry[];
}

export type BankChangeStatus = "awaiting_callback" | "awaiting_approval" | "approved" | "rejected";

export interface BankAccount {
  bank_name: string;
  bank_account: string;
}

export interface Callback {
  called_by: string;
  phone: string;
  spoke_with: string;
  outcome: "confirmed" | "denied";
  note: string;
  at: string;
}

export interface BankChange {
  id: number;
  vendor_id: string;
  status: BankChangeStatus;
  old: BankAccount;
  new: BankAccount;
  source_invoice_id: string | null;
  channel: string;
  requested_by: string;
  callback: Callback | null;
  decided_by: string | null;
  decision_note: string | null;
  erp_status: "pending" | "synced" | "needs_attention" | null;
  erp_error: string | null;
  created_at: string;
  updated_at: string;
  version: number;
}

export interface BankChangeDetail extends BankChange {
  vendor: { name: string | null; phone_on_file: string | null };
  invoices: InvoiceSummary[];
  audit: AuditEntry[];
}

export interface ErpStatus {
  status: "pending" | "posted" | "needs_attention";
  bill: string | null;
  attempts: number;
  error: string | null;
}

export interface SummaryGroup {
  state: State;
  decision: Decision;
  count: number;
  total: number;
}

export interface ChainStatus {
  ok: boolean;
  events: number;
  first_broken_seq: number | null;
}
