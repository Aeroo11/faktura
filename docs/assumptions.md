# Assumptions

Domain assumptions the code depends on. Each one should be checked against the
current regulation or a real AP team before the numbers are presented as realistic.

| Assumption | Where | Status |
|---|---|---|
| Effective VAT is 11% for ordinary goods (nominal 12% on a reduced tax base of 11/12 DPP) | `configs/synth.yaml` `vat_bps` | Verify against the current PMK |
| VAT is floored to whole rupiah | `schemas.compute_vat` | Verify the rounding rule |
| NPWP is 16 digits | `schemas.Vendor.npwp` | Verify |
| Tax invoice numbers are 17 random digits | `synth.generate` | Synthetic format only, not the official one |
| An invoice bills only what was received, at the PO price | `synth.generate.make_clean_invoice` | Common three-way-match policy; real policies add tolerances |
| Duplicate = same vendor and same invoice number | `tests/test_synth.py` oracle | Real AP also uses fuzzy matching; planned for phase 2 |

All vendor names, NPWPs, and bank accounts are random and fictional. Every rendered
page carries a "CONTOH SINTETIS" banner.
