# OTC Variation-Margin Report

- **Spot**: 0.9000
- **r**: 0.0000
- **Valuation date**: 2026-06-04
- **Holdings**: 750,000 FIL
- **Flat IV**: 80.00% (legs without their own iv)
- **Move sizes ($)**: 0.05, 0.1, 0.2

## Positions

| CP | Side | Type | Strike | Expiry | DTE | IV | Qty | Per-unit Px | MTM $ (+asset/-liab) | Per-unit d | Position d | Verdict | Cover |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| OTC | buy | P | 0.50 | 31JUL26 | 57 | 80% | 1,250,000 | 0.0026 | +3,224 | -0.022 | -27,289 | PAYS (asset) | asset (credit) |
| OTC | sell | P | 2.50 | 31JUL26 | 57 | 80% | 500,000 | 1.6001 | -800,039 | -0.999 | +499,471 | CALLS on DOWN | cash (CSP) |
| OTC | sell | C | 0.50 | 31JUL26 | 57 | 80% | 750,000 | 0.4026 | -301,935 | +0.978 | -733,627 | CALLS on UP | covered (pledge) |
| OTC | buy | C | 2.50 | 31JUL26 | 57 | 80% | 500,000 | 0.0001 | +39 | +0.001 | +529 | PAYS (asset) | asset (credit) |

### VM swing per move ($, signed for a spot RISE; negative = I post)

| # | Leg | +0.05 | +0.1 | +0.2 |
|---|---|---|---|---|
| 1 | buy P 0.50 | -1,364 | -2,729 | -5,458 |
| 2 | sell P 2.50 | +24,974 | +49,947 | +99,894 |
| 3 | sell C 0.50 | -36,681 | -73,363 | -146,725 |
| 4 | buy C 2.50 | +26 | +53 | +106 |

## Portfolio rollup

- **Net delta**: -260,915.411 FIL
- **Dollar delta**: -234,824 (net delta x spot)
- **Net MTM**: -1,098,710 (+ asset / - liability)
- **Standing short-leg liability**: 1,101,973 gross, -1,098,710 net of long assets

### VM sensitivity

| Move ($) | Netted swing (UP, one set) | Gross POST on UP | Gross POST on DOWN |
|---|---|---|---|
| 0.05 | -13,046 | 38,046 | 25,000 |
| 0.1 | -26,092 | 76,092 | 50,000 |
| 0.2 | -52,183 | 152,183 | 100,000 |

- **Cash collateral required**: 1,250,000 (sum of cash-margin short legs)
- **Underlying pledged**: 750,000 FIL (~675,000)

> **TWO-SIDED MARGIN WARNING** — some legs call on UP and others on DOWN. You can be called in both directions; a single directional hedge will not neutralise both ends.
