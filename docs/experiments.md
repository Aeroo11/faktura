# Experiment log

Newest first. Every number comes from a run directory under `runs/` (git-ignored); the
command to reproduce it is listed with each entry.

## 2026-11-20 · What the first traces showed: decision latency

Tracing (`docs/observability.md`) on the demo stack: Postgres, Odoo master, 340 invoices.
The first trace of a field correction (`POST /invoices/{id}/corrections`) took 588 ms,
with 6 Odoo calls (~290 ms) inside the decision: the 30 s master-data cache had expired,
so the user's request paid for the refresh. Fix: refresh the cache in the background every
15 s (`CachedMaster.start_background_refresh`); the staleness bound is unchanged.

The next trace had no Odoo calls but still took 724 ms, 690 ms of it in the decision, and
only 22 ms of that in SQL. cProfile: `build_ledger` replayed the billing of **every**
earlier invoice, fuzzy-matching each line to its PO (`difflib`), 95% of the time, growing
linearly with AP's history. Only the invoice's own PO matters for cumulative billing, so
the replay is now scoped to it (duplicate detection still sees every invoice; that part
is cheap).

| | Before | After |
|---|---|---|
| Decisions that differ, all 340 invoices, full vs scoped replay | | **0** |
| Mean time per decision (340 invoices, files master) | 454 ms | **25 ms** |
| Correction request, end to end (trace) | 724 ms | **75 ms** |
| Odoo calls inside a request | 6 when the cache expired | **0** |
| `faktura-seed`, 340 invoices, Odoo master (same 165/62/58/55 outcome) | 177.8 s | **18.4 s** |

Reproduce: `scripts/ledger_equivalence.py` re-decides every invoice in the demo database
both ways; regression tests are `test_ledger_replays_billing_only_for_the_requested_po`
and `test_cumulative_billing_still_counts_earlier_invoices_on_the_same_po`. A side
effect: `faktura-seed` decides all 340 invoices much faster.

## 2026-11-19 · Learning from corrections

`faktura-eval-learn --runs runs/luna-v2-reread-tiles-final runs/luna-v2-reread-pilot-tiles runs/luna-v2`
(no API calls). Full write-up: `docs/learning.md`.

A misread model (counts of aligned edit types from identifier corrections) triages an
NPWP/bank mismatch that looks like a known misread as `probable_misread` (review_reading)
instead of `fraud_hold`. Replay in arrival order with a simulated clerk, fitted only on
earlier corrections (88 identifier corrections, 72 within 3 edits):

| | No learning | Learned |
|---|---|---|
| fraud_hold | 90 | 70 |
| clean in fraud_hold | 39 | 20 |
| precision | 0.567 | 0.714 |
| changed NPWP/bank still held | 25/25 | 25/25 |
| auto-approved | 165 | 165 |

Learning curve (fit on first-half corrections, measure second half; baseline 16 false
alarms): 0 → 16, 10 → 13, 25 → 9, 42 → 7, with 12/12 payee changes held throughout.
Adversarial, one digit changed to the most common misread: 235/235 triaged (the fraud
label is bypassed), 235/235 held after the page check, 0 paid.

**Loophole found and closed during the live check.** A triaged invoice showed the normal
"Submit for approval" form, which needs only a justification, while a fraud hold also
needs a vendor verification. A clerk could have skipped the page check and sent a
forged payee to an approver. Submit is now refused while `probable_misread` is present
(regression test in `tests/test_app.py`).

**Re-read prompt with learned hints: negative, not adopted.**
`faktura-eval-reread --base runs/luna-v2 --tiles --exclude runs/luna-v2-reread-pilot-tiles [--misread-model runs/learn-dev/misread-model.json]`.
Hints were fitted on the 40 dev invoices only (`faktura-eval-learn --fit-on`). The first
attempt ran both configurations at the same time and hit rate limits (29 and 10 failed
calls), so the failed calls were retried one run after the other before comparing.

| Held-out (n=296 paired) | Fully correct | Fixed / broken |
|---|---|---|
| Original tiles run (2026-11-18) | 73.0% | 140 / 7 |
| Same config, rerun | 72.0% | 138 / 10 |
| + learned hints | 66.9% | 60 / 1 |

With hints the second read changed 70 of 369 requested fields (237 without). The likely
cause is more nulls, which make the first reading stick; this is not verified because the
raw second-read output is not stored. Run-to-run noise on this set is about 1 point.

Side finding (rerun): on inv-00323 (phone photo, `vat_miscalc`) the re-read returned
the VAT that should have been charged instead of the miscalculated VAT printed on the
invoice, so the VAT field flag was lost. The invoice was still stopped, because DPP +
VAT no longer matched the total, and no anomalous invoice was approved with these
readings. Arithmetic cross-checks are a second line of defence against the model
"correcting" a document.

## 2026-11-18 · Three-way match: end-to-end decisions

`faktura-eval-match --oracle` and
`faktura-eval-match --runs runs/luna-v2-reread-tiles-final runs/luna-v2-reread-pilot-tiles runs/luna-v2`

`matching.py` maps validator issues to four decisions (approve, review_reading,
business_exception, fraud_hold). The most severe decision applies. It adds a ledger for
duplicates and cumulative billing. A bank account or NPWP that differs from the vendor
master is always held, because changed payment details are the classic AP fraud.

**Oracle (perfect reading), all 340 invoices:** every invoice is routed as expected. All
241 clean invoices are approved, and all 99 anomalies get their expected decision.
Rp 775,571,346 at risk, 100% caught. This checks the rules, independent of reading.

**Real readings (first pass + tiled re-read):**

| Invoice kind | n | Not auto-approved | Expected decision |
|---|---|---|---|
| bank_account_changed | 11 | 100% | 100% |
| npwp_mismatch | 14 | 100% | 100% |
| duplicate_invoice | 20 | 100% | 100% |
| price_mismatch | 15 | 100% | 80% (rest held: a misread NPWP/bank on the same invoice) |
| qty_over_received | 23 | 100% | 95.7% |
| vat_miscalc | 16 | 100% | 87.5% |
| clean | 241 | 31.5% | 68.5% approved straight through |

- Money at risk Rp 775,571,346: **100% caught**.
- Clean invoices paid with a misread amount or bank account: **0**.
- Clean invoices approved straight through, by level: 0 clean 70/70, 1 mild scan 45/46,
  2 poor scan 39/61, 3 photo 11/64. First pass only: 121/241; with the re-read: 165/241.
- All 39 clean invoices on fraud_hold are there because a misread NPWP or bank account
  looks like changed payment details (NPWP 15, bank 10, both 14). This is the cost of
  the "never auto-pay changed details" policy. It is paid only on poor scans and photos.

**Duplicate rule added after a miss (disclosed).** The first version approved one
duplicate (Rp 11,727,483). Its original was a phone photo whose invoice, tax, and PO
numbers were all misread, so the correctly read resubmission matched nothing in the
ledger. Duplicate detection now also matches on vendor (id, name, or NPWP) + total +
(qty, unit price) per line, because money is read far more reliably than identifiers.
After the change, all 20 duplicates are held and no clean invoice gained a new hold.
The rule was designed on the same data it is evaluated on, since no fresh model readings
exist without new API calls.

## 2026-11-18 · Phone-photo preprocessing (negative result, not adopted)

`faktura-eval-extract --preprocess --reuse runs/luna-v2 ...`, module
`extract/preprocess.py`.

Idea: fix level-3 photos before the model sees them. Planned steps were perspective warp,
lighting correction (a quadratic surface fitted to paper pixels only, so white-on-navy
header text survives), grayscale, and contrast stretch.

Photo detection: the paper's lighting ratio separates cleanly on v2 (levels 0–2 at or
below 1.18, level 3 at or above 1.49; threshold 1.3, chosen after looking at all 340
invoices). The result was 86/86 photos detected and 0/254 other pages touched; those are
returned byte-for-byte unchanged. A "paper does not fill the frame" criterion was
rejected because it flagged 26/101 clean PDFs: a full-width header band splits the white
area.

Perspective warp was **disabled after a visual check**. Uneven lighting makes the dark
half of the paper look like background, so the detected outline was wrong and the warp
cropped real content: vendor name, NPWP, invoice number, bank account. Running it
through the API would have cost money to make results worse.

Lighting correction kept all content and removed the colour cast, but did not change
accuracy:

| Phone photos | Fields correct, no preprocessing | Fields correct, preprocessing |
|---|---|---|
| Dev, first pass (n=14) | 118 | 118 |
| Dev, after tiled re-read (n=14) | 132 | 131 |
| Held-out, first pass (n=72, 864 fields) | 604 | 604 |

Conclusion: lighting and colour are not what limits the model on these photos.
Resolution is. Level 3 downsamples to 40–55%, blurs, and JPEG-compresses at quality
25–40, and that information cannot be recovered afterwards. Preprocessing stays off by
default. The practical answer for phone photos is a capture-quality gate (detect a
too-low effective resolution and ask for a retake or a scan), not more image processing.

## 2026-11-18 · Targeted second read of validator-flagged fields

`faktura-eval-reread --base runs/luna-v2 --out <run> [--tiles] [--exclude <dev run>]`

Fields flagged by the validators are read again: page rendered at 300 DPI, image detail
`high`, reasoning `medium`, a prompt asking for only those fields. FORMAT/INCONSISTENT
reasons are shown to the model; MASTER reasons never are, so a correctly read fraudulent
value is not nudged towards the master record.

Upper bound (every flagged field fixed): 52.1% -> 79.7% of invoices fully correct.

Dev set: 40 random invoices (seed 0), used to choose the configuration.

| Config (dev, n=40) | Fully correct | Fields fixed | Fields broken | Anomaly flags kept | USD / 1k invoices |
|---|---|---|---|---|---|
| First pass only | 45.0% | | | | 0.60 |
| Re-read, full page | 52.5% | 13 | 2 | 7/7 | 0.86 |
| Re-read, page + 6 zoomed tiles | 57.5% | 19 | 0 | 7/7 | 1.11 |

Tiles help because the model downscales a full page; a crop of one sixth of the page keeps
roughly twice the pixels per character.

**Final, on the 300 invoices not used for dev** (tiles; 4 re-read calls failed and kept
their first reading, so 296 are compared):

| Level | n | First pass | After re-read |
|---|---|---|---|
| All | 296 | 53.7% | **73.0%** |
| 0 clean | 90 | 90.0% | **100%** |
| 1 mild scan | 57 | 84.2% | **98.2%** |
| 2 poor scan | 80 | 35.0% | **71.2%** |
| 3 phone photo | 69 | 2.9% | 18.8% |

140 fields fixed, 7 broken. Most fixes: invoice number (64, mostly `0` read as `O`),
NPWP (28), bank account (18). 173 of 300 invoices needed a re-read. Cost USD 1.02 per
1,000 invoices (list price), latency p50 10.5 s, p95 44.9 s.

**Validator bug found here.** One anomaly flag seemed lost after the re-read. It was a
VAT under a "DITERIMA" stamp on a phone photo, read as `null` in both passes. The
validators did not treat a missing DPP or VAT as an issue, so the invoice would have
passed without review. DPP and VAT were added to the missing-field check, with a
regression test. After the fix, 72/72 anomaly fields are still flagged after the re-read,
and validator recall on the first pass rises from 74.6% to 75.5%.

Phone photos remain the weak spot (18.8%). Next lever there is image preprocessing
(perspective correction, contrast), not more prompting.

## 2026-11-18 · CORD v2: prompt tuning on validation, final test run

Prompt p2 was written from the p1 error categories on the **validation** split. `detail:
high` on the image input was tried as a preprocessing option. All three configurations
were scored on validation; the test split was then run once with the chosen one.

| Config | Split | TED | F1 (official) | F1 lenient* |
|---|---|---|---|---|
| p1 | validation | 92.2 | 86.4 | 90.7 |
| p2 | validation | 92.5 | 87.0 | 92.0 |
| p2 + detail high | validation | 93.3 | 86.5 | 91.8 |
| p1 | test | 91.3 | 80.9 | 85.9 |
| **p2 (final)** | **test** | **91.9** | **82.1** | 86.9 |
| Donut, fine-tuned (published) | test | 91.3 | 84.1 | n/a |

\* Lenient F1 ignores spaces, case, and `,` vs `.`. It is a secondary metric for
"would AP read the same value", and is **not comparable** with published CORD numbers.

Choice: p2 with default detail. It has the best validation F1, and `detail: high` did not
reduce the thousand-separator errors it was meant to fix, so its TED gain is within what
looks like run-to-run noise (all differences are under one point, single runs).

What p2 fixed: fewer structural errors (wrong value/extra fields 89 -> 67, missed fields
76 -> 62, validation). What it did not fix: thousand separators on dot-matrix receipts.
Inspecting one such receipt shows a comma that differs from a dot by a single ink dot;
the model reads it as the Indonesian-style dot. Prompting does not change this, so it is
treated as a perception limit, not a prompt problem.

Result: zero-shot gpt-6-luna matches/exceeds fine-tuned Donut on TED accuracy (91.9 vs
91.3) and trails it on exact-match F1 (82.1 vs 84.1). Most of the F1 gap is exact-copy
formatting; with formatting ignored, F1 is 86.9.

Cost: about 3,000 input and 550 output tokens per receipt; latency p50 about 7 s.

## 2026-11-18 · CORD v2 test split, prompt p1 (untuned baseline)

`faktura-bench-cord --split test --prompt p1 --run runs/cord-luna-test-p1`

External benchmark: 100 Indonesian receipts, scored with Donut's own evaluator
(`bench/donut_metric.py`, ported with its MIT notice) against `gt_parse`.

| Model | Training on CORD | TED accuracy | Field F1 |
|---|---|---|---|
| gpt-6-luna, prompt p1 | none (zero-shot) | 91.3 | 80.9 |
| Donut | fine-tuned on 800 receipts | 91.3 | 84.1 |

Donut numbers are from the README of github.com/clovaai/donut
(donut-base-finetuned-cord-v2, score 91.3; example test output TED 0.9129, F1 0.8406).

Protocol: p1 was written before looking at any CORD sample. Five test receipts were then
inspected to debug the pipeline (disclosed here). Further prompt work happens on the
validation split only; the test split is run once more with the final prompt.

Error pattern on those five receipts: the model "cleans up" text the metric wants copied
verbatim. It fixes printed typos (`BLACKCOFFE` -> `BLACK COFFEE`), normalises spacing
(`( L )` -> `(L)`), drops symbols (`@11000` -> `11000`), and swaps thousand separators.

Caveats: zero-shot vs fine-tuned is not a like-for-like comparison, and CORD has been
public since 2019, so it may be in the model's pre-training data.

## 2026-11-18 · Deterministic validators on the v2 run 2 outputs

`faktura-eval-validate --data data/synth/v2 --run runs/luna-v2` (no API calls; reuses
the cached extractions).

Share of field-level extraction errors that get sent to a human:

| Slice | Field errors | Model self-flags | Validators | Either |
|---|---|---|---|---|
| All | 417 | 17.0% | 74.6% (75.5% after the DPP/VAT fix below) | 81.8% |
| Level 0 clean | 10 | 0.0% | 100% | 100% |
| Level 1 mild scan | 13 | 0.0% | 92.3% | 92.3% |
| Level 2 poor scan | 84 | 2.4% | 83.3% | 85.7% |
| Level 3 phone photo | 310 | 22.3% | 70.6% | 79.7% |

At the invoice level, 161 of 163 invoices with any misread raise at least one flag. The two
that pass unflagged differ from the truth only in the wording of a long item description;
quantities, prices, and amounts are exact, so neither affects payment.

On invoices that were read correctly and carry no anomaly, the validators raised **zero**
flags; the 5 flags on such invoices all came from the model's self-flags. 97.5% of clean,
correctly read invoices pass straight through.

Validators used: identifier length, date parseability, line and total arithmetic, vendor
master (name, NPWP, bank), the vendor's historical invoice-number shape (catches `0` read
as `O`), VAT rate, PO price, and goods-receipt quantity. Tests require that a perfect
reading of a clean invoice raises nothing and that a perfect reading of an anomalous
invoice flags exactly the fields of that anomaly.

## 2026-11-18 · v2 run 2 (current baseline)

`faktura-eval-extract --data data/synth/v2 --run runs/luna-v2 --workers 3`
Model `gpt-6-luna`, reasoning `low`. 340 invoices, 0 failed calls.

| Field | Level 0 clean | Level 1 mild scan | Level 2 poor scan | Level 3 phone photo |
|---|---|---|---|---|
| Invoice fully correct | 90.1% | 81.5% | 35.2% | 2.3% |
| dpp / vat / total | 100% | 100% | ≥98.9% | 88–91% |
| vendor_npwp | 97.0% | 100% | 85.2% | 45.3% |
| bank_account | 100% | 100% | 88.6% | 58.1% |
| invoice_number | 95.0% | 81.5% | 55.7% | 19.8% |

Findings:

- **Money is robust; long identifiers are not.** DPP, VAT, and total stay at 100% through
  poor scans. Errors concentrate in 16–17 digit strings (NPWP, tax invoice number, bank
  account): transposed digits, doubled digits, or a zero read as the letter O.
- **The model's own uncertainty flags catch only 17% of its errors** (74% of the flags are
  real errors, but most errors are not flagged). Self-reported uncertainty cannot be the
  gate for human review.
- **Level 3 (phone photo) is not usable without review**: 2.3% of invoices fully correct.
- Layout, language, and number format matter much less than degradation level.
- Cost about USD 0.60 per 1,000 invoices at list price; latency p50 5.8 s, p95 10.6 s.

Implication for the matching stage: an NPWP misread looks exactly like an
`npwp_mismatch` anomaly. Deterministic validators (identifier length, arithmetic
consistency, vendor master lookup) must separate "probably misread" from "probably
fraud" before an invoice is flagged.

## 2026-11-18 · v2 run 1 (discarded: renderer bugs)

`faktura-eval-extract --data data/synth/v2 --run runs/luna-v2-renderbug --workers 6`

Invoice exact match 44.1%, but the level-0 (clean PDF) slice had errors a clean PDF
should not produce. Inspecting them found two bugs in **our renderer**, not in the model:

1. The `borderless` layout printed the NPWP as 16 bare digits with no "NPWP" label. The
   model returned `null` instead of guessing, which is the behaviour we want. This bug
   accounted for 14 of the level-0 NPWP errors.
2. Bold text was faked with a 1px stroke, which closes the gap in "5" so it reads as "6"
   (`14.543.872` read as `14.643.872`). Real invoices use a true bold face. Digits are no
   longer stroked.

15 calls also failed with HTTP 429 (rate limit). By design they are scored as wrong, not
dropped. Client retries were raised to 8 and workers lowered to 3.

Errors kept as genuine, because they also happen on real documents: `V002` read as `VO02`
(zero vs letter O), a digit inserted into a bank account, and a leading zero added to a
tax invoice number.

**Lesson:** a synthetic benchmark needs its own QA. Reading the errors on the easiest slice
first is what exposed both bugs.

## 2026-11-18 · v1 smoke

`faktura-eval-extract --run runs/luna-smoke --limit 10`

10/10 invoices fully correct. This was read as "the data is too easy", which led to v2.
