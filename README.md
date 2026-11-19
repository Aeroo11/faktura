# Faktura

[Bahasa Indonesia](README.id.md)

Accounts-payable automation: read supplier invoices, match them against the purchase order
and goods receipt (*three-way match*), and send only the problem invoices to a human.

> **Status: Phase 1 (MVP) complete.** Labelled synthetic data, extraction with targeted
> re-reading, deterministic validators, a matching engine with an end-to-end eval, a CORD
> benchmark, a review-queue API with maker-checker and a tamper-evident audit trail, and a
> UI. Not yet built: SSO, ERP integration, and deployment (phases 2–3). This README only
> describes what is in the code.

## Results at a glance

| What | Result |
|---|---|
| Anomalies caught (price, quantity, duplicate, NPWP, VAT, bank account) | **99 / 99**, Rp775.6M at risk, 100% caught |
| Clean invoices paid with a misread amount or account | **0** |
| Invoices read fully correctly, after targeted re-read | 53.7% → **73.0%** (100% on clean PDFs) |
| Field errors caught by deterministic validators vs. model self-flags | **75.5%** vs. 17% |
| CORD v2 (public benchmark), zero-shot | TED accuracy **91.9** vs. 91.3 for fine-tuned Donut |
| Extraction cost | ~USD 1 per 1,000 invoices |

Re-read numbers are on 300 invoices held out from the 40 used to tune it; CORD numbers are
on its test split, with prompts tuned on validation only. Matching numbers cover all 340.

## Synthetic dataset

```bash
uv sync
uv run faktura-synth --config configs/synth.yaml --out data/synth/v1
uv run pytest
```

The generator creates vendors, POs, goods receipts, and invoice PDFs that vary in language
(ID/EN), date format, NPWP (tax ID) format, and scan effects. Some invoices get exactly one
injected anomaly:

| Anomaly | Example |
|---|---|
| `price_mismatch` | Invoice unit price 2–15% above the PO |
| `qty_over_received` | Billed quantity exceeds what was received |
| `duplicate_invoice` | The same invoice submitted again |
| `npwp_mismatch` | NPWP differs from the vendor master |
| `vat_miscalc` | VAT computed at the wrong rate |
| `bank_account_changed` | Payee account differs from the vendor master |

### v1 and v2

v1 (`configs/synth.yaml`) uses one layout with light scanning. A 10-invoice smoke test on
v1 scored 100%, a sign that the data was too easy, not that the model was perfect. v2
(`configs/synth_v2.yaml`) is deliberately harder. Each difficulty factor is recorded
separately, so the eval can show which one makes extraction fail:

| Factor | Variants |
|---|---|
| Layout | classic, modern (coloured header, zebra rows), compact (small font, tax invoice number in the footer), borderless (no rules, totals on the left) |
| Number format | `Rp 1.234.567`, `Rp1.234.567,00`, `IDR 1,234,567` |
| Date format | `05/03/2025`, `5 Maret 2025`, `2025-03-05`, `05-Mar-2025` |
| Descriptions | 30% of items use long descriptions that wrap to two lines |
| Degradation | 0 clean PDF, 1 light scan, 2 poor scan (folds, low contrast, JPEG), 3 phone photo (perspective, uneven lighting) |
| Stamps | 40% of level 2–3 invoices get a "DITERIMA" (received) stamp over the totals |

Some stamped level-3 invoices have digits that are unreadable even to a human. For those,
the eval measures whether the model flags the field as uncertain, not whether a guess
happened to be right.

Because the anomalies are injected, the ground truth comes without manual labelling. The
test `test_labels_match_independent_oracle` re-checks every label with rules written
separately from the generator.

## Invoice extraction

The PDF page is rendered to an image (~170 DPI) and read by a vision model through the
Azure AI Foundry Responses API with structured output (Pydantic). The model only copies
what is printed. Recomputation and decisions are done by deterministic code in the
matching stage.

The model is not asked for numeric confidence scores, because self-reported numbers are
usually not calibrated. It flags hard-to-read fields instead (`uncertain_fields`), and
the eval measures how many real errors those flags catch. They catch only 17%, so the
pipeline relies on deterministic validators (format, arithmetic, vendor master), which
catch 75.5% with no false alarms on correct reads. Flagged fields get a targeted re-read
at higher resolution with zoomed tiles. Master data is never shown to the model, so a
fraudulent value cannot be "corrected" into the expected one.

```bash
cp .env.example .env   # fill in your own Azure endpoint and key
uv run faktura-eval-extract --run runs/luna-smoke --limit 10
```

The eval report covers per-field accuracy, the share of fully correct invoices, slices by
condition (scan level, ID or EN), flag quality, p50/p95 latency, and estimated cost per
1,000 invoices. Raw model output is cached per invoice, so an interrupted run resumes
without paying twice.

What did not work is logged too: OpenCV lighting correction for phone photos gave no gain
(604 vs. 604 correct fields over 72 photos). See [`docs/experiments.md`](docs/experiments.md).

## Payment decisions (three-way match)

Every invoice gets one of four decisions, and each decision carries reasons an auditor
can read:

| Decision | Meaning |
|---|---|
| `approve` | Matches the PO, goods receipt, and vendor master, so it is paid automatically |
| `review_reading` | The reading is in doubt, so a human checks it against the document |
| `business_exception` | Price, quantity, or VAT deviates from the PO, so it needs business approval |
| `fraud_hold` | Bank account or NPWP changed, or the invoice is a duplicate, so payment is held |

A bank account or NPWP that differs from the master is **always held**, even if it is
probably a misread. Changed payment details are the most common AP fraud pattern.

Results on 340 synthetic v2 invoices, using real model readings (first pass plus re-read
of flagged fields):

- **Anomalies caught: 99 of 99.** Rp775.6M at risk, 100% caught.
- **Clean invoices paid with a misread amount or account: 0.**
- **Clean invoices auto-approved:** 100% of PDFs, 98% of light scans, 64% of poor scans,
  and 17% of phone photos. The rest go to a human.

```bash
uv run faktura-eval-match --oracle            # test the rules with perfect readings
uv run faktura-eval-match --runs runs/<run>   # test with model readings
```

## Review queue and audit trail

Invoices that are not auto-approved go to a queue. Workflow rules are enforced in the API,
not in the UI:

- **Maker-checker.** AP staff (makers) correct or confirm fields, then the matching engine
  decides again. An invoice a human has touched must be approved by a different approver.
  Whoever changed an invoice cannot approve it.
- **`fraud_hold`** can only be submitted after a vendor verification is recorded (who was
  contacted, through which channel, and what they confirmed).
- **The model's original reading is never overwritten.** Human corrections are stored
  separately.
- **Tamper-evident audit trail.** Every event stores the hash of the previous one, so
  `GET /audit/verify` detects edited or deleted rows. In Postgres, a trigger rejects
  UPDATE and DELETE on the audit table.
- **Optimistic concurrency.** Actions against a stale invoice version are rejected (409).

```bash
docker compose up -d db
export DATABASE_URL=postgresql+psycopg://faktura:faktura-dev@127.0.0.1:5433/faktura
uv run alembic upgrade head
uv run faktura-seed --runs runs/luna-v2-reread-tiles-final runs/luna-v2-reread-pilot-tiles runs/luna-v2
uv run uvicorn faktura.app.api:app --port 8077   # Swagger UI at /docs
```

Login is a placeholder: identity comes from the `X-User` and `X-Role` headers, for
development only. SSO (Keycloak) is planned for phase 2.

### Review UI

![Review queue](docs/screenshots/1-queue.png)

The detail page shows the invoice image next to the reading. Flagged fields are
highlighted, and the model's original value stays visible next to the human correction:

![Invoice held because the NPWP was misread](docs/screenshots/2-held-for-npwp.png)

The flow in the screenshots: the NPWP was read with two digits swapped, so the invoice was
held. AP staff (Budi) corrects it from the document, and the matching engine decides
again. Because a human touched the invoice, payment still needs a different approver
(Sari). Every step is in the audit trail:

![Approved, with audit trail](docs/screenshots/4-approved-with-audit.png)

```bash
cd web && npm install && npm run build
FAKTURA_API_URL=http://127.0.0.1:8077 npm start      # http://127.0.0.1:3077
npm run verify                                        # typecheck + lint + unit tests
```

Next.js 16 calls FastAPI from the server, so the browser never talks to the API directly.

**End-to-end test in a real browser** (Playwright) runs the scenario above and produces
the screenshots:

```bash
uv run playwright install chromium
uv run python scripts/e2e_demo.py --out docs/screenshots   # API and web must be running
```

## External benchmark: CORD v2

For comparison with public models, extraction was also tested on
[CORD v2](https://huggingface.co/datasets/naver-clova-ix/cord-v2) (Indonesian shop and
restaurant receipts, CC BY 4.0). Scoring uses Donut's official metric code, ported with
its MIT licence, so the numbers are directly comparable.

| Model | Trained on CORD | TED accuracy | F1 |
|---|---|---|---|
| gpt-6-luna (zero-shot, prompt p2) | no | **91.9** | 82.1 |
| Donut (fine-tuned) | 800 receipts | 91.3 | **84.1** |

Prompts were tuned only on the validation split, then the test split was run once. Most of
the F1 gap comes from transcription format, for example `8,000` written as `8.000` on
dot-matrix receipts. Ignoring whitespace, case, and thousands separators, F1 is 86.9. That
is a secondary metric and not comparable to published numbers.

Caveats: zero-shot versus fine-tuned is not a like-for-like comparison, and CORD has been
public since 2019, so it may be in the model's pre-training data. Full details are in
[`docs/experiments.md`](docs/experiments.md).

```bash
uv run faktura-bench-cord --split validation --prompt p2 --run runs/cord-val-p2
```

All data is fictitious, and every page is marked "CONTOH SINTETIS" (synthetic sample).
Domain assumptions are in [`docs/assumptions.md`](docs/assumptions.md).

## License

[MIT](LICENSE).
