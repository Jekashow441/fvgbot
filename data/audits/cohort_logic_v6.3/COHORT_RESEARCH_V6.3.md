> CORRECTION: waterfall / first-blocker and detector-quality claims superseded; see [correction notice](CORRECTION_NOTICE.md).

# Cohort and detector research — V6.3 (read-only)

Unique setup cards: 1654; observations: 17389. Production strategy unchanged.

## Cohort waterfall

| Stage | Entered | Passed | Failed | Unknown | N/A |
|---|---:|---:|---:|---:|---:|
| Raw setup | 1654 | 1654 | 0 | 0 | 0 |
| FVG validity | 1654 | 1650 | 0 | 4 | 0 |
| Zone eligibility | 1654 | 1238 | 411 | 5 | 0 |
| Structure | 1654 | 1202 | 448 | 4 | 0 |
| HTF | 1654 | 539 | 1093 | 22 | 0 |
| Liquidity | 1654 | 27 | 1623 | 4 | 0 |
| Quality | 1654 | 836 | 814 | 4 | 0 |
| VWAP | 1608 | 1597 | 11 | 0 | 46 |
| Orderbook | 1608 | 1590 | 17 | 1 | 46 |
| Spread | 1608 | 1605 | 3 | 0 | 46 |
| Mitigation | 1621 | 569 | 1052 | 0 | 33 |
| Rejection | 1610 | 2 | 1608 | 0 | 44 |
| Entry | 18 | 18 | 0 | 0 | 1636 |
| Signal | 18 | 0 | 18 | 0 | 1636 |

Entered counts are unique setup IDs. N/A means the terminal path stopped earlier; UNKNOWN is reserved for missing/unavailable data. Each card has exactly one `first_blocker` and a separate `secondary_blockers` list. Liquidity is explicitly FALSE for most observations but is not a terminal production rejection in the current path; this is why a setup can continue after a failed liquidity check.

## First blocker by unique setup

| First blocker | Unique setups |
|---|---:|
| `fvg_fully_filled` | 671 |
| `observation_expired` | 589 |
| `fvg_ce_invalidated` | 215 |
| `no_recent_retest` | 86 |
| `htf_opposition` | 46 |
| `prolonged_zone_residence` | 26 |
| `no_bullish_rejection` | 8 |
| `entry_too_far` | 7 |
| `low_relative_volume` | 4 |
| `no_bearish_rejection` | 2 |

## Probability-ranked cause of 1654 → 18 → 0

1. Lifecycle/retest/mitigation is the dominant cohort loss: 1597 of 1654 setups (96.55%) terminate in these stages. 2. HTF/context is the first blocker for 46 setups; it is a smaller cohort cause but can overlap as a secondary blocker. 3. Only 18 setups reached technical Entry; the execution blockers then eliminate all of them, led by thin orderbook, VWAP extension and higher-4h opposition. The zero is therefore a compounded gate interaction, not a single Telegram failure.

## Reference-only classification

Current reproducible snapshot: {'all_reference_only': 3462, 'category_A': 13, 'category_B': 653, 'category_C': 2796, 'category_D': 0}. The previous latest-window run reported 3424; the live candle files refreshed between runs, so the exact earlier zone list was not persisted. This artifact locks the current 470-file snapshot and does not silently call the count drift a detector defect.
| Production rejection evidence | Count |
|---|---:|
| `atr` primary | 1370 (39.57%) |
| `body` primary | 1298 (37.49%) |
| `volume` primary | 649 (18.75%) |
| `mitigation` primary | 63 (1.82%) |
| `other` primary | 44 (1.27%) |
| `size` primary | 38 (1.1%) |

All-reason counts are in JSON because one reference-only zone can fail more than one production condition. Categories A/B/C/D are independent quality buckets; D is reserved for malformed/non-FVG geometry, not merely for failing a stricter production filter. The archived candidate-boundary study remains A=2, B=74, C=29; this run also stores 617 B-detail rows from the broader all-file reference-only pool. The differing B counts are a scope distinction (candidate-prefix cohort versus every zone in 470 files), not a claim that the 74 rows disappeared.

## Displacement, volume and body experiments

Mode counts: {'A_current': {'raw': 3541, 'quality': 109, 'trade_candidate_proxy': 66}, 'B_no_displacement': {'raw': 3541, 'quality': 177, 'trade_candidate_proxy': 119}, 'C_soft_displacement': {'raw': 3541, 'quality': 156, 'trade_candidate_proxy': 101}, 'D_atr_normalized': {'raw': 3541, 'quality': 130, 'trade_candidate_proxy': 85}}. Volume summary: {'zone_count': 3541, 'missing_or_invalid_field': 0, 'insufficient_20_bar_baseline': 2437, 'relative_volume_p50': 0.6966018331113275, 'relative_volume_p10': 0.1028435126042436, 'relative_volume_p90': 3.7025867990707417, 'symbols_with_volume': 429}. Strong moves lost specifically to the 70% body rule: 152. A missing/invalid volume value is recorded as UNKNOWN in the detector evidence; it is not converted to FAIL by this research script.

## Pairwise combinations

| Experiment | Technical candidates after | Potential proxy | Entry Approaching proxy | Confirmed actual |
|---|---:|---:|---:|---:|
| BASELINE | 18 | 10 | 0 | 0 |
| - thin_orderbook | 2 | 0 | 0 | 0 |
| - VWAP | 1 | 1 | 0 | 0 |
| - HTF hard block | 0 | 0 | 0 | 0 |
| - rejection | 0 | 0 | 0 | 0 |
| - thin - VWAP | 6 | 4 | 0 | 0 |
| - thin - rejection | 2 | 0 | 0 | 0 |
| - VWAP - rejection | 1 | 1 | 0 | 0 |
| - thin - VWAP - rejection | 6 | 4 | 0 | 0 |
| - HTF + reversal confirmation | 0 | 0 | 0 | 0 |

These are counterfactual proxies over the 18 technical-entry cohort, not profitability tests.

## Reaction and liquidity

Reaction counts: {'touch': 13, 'touch_plus_directional_rejection': 13, 'touch_without_directional_rejection': 0}; objective reaction types: {'wick_rejection': 18, 'close_beyond_ce': 25, 'consecutive_directional': 12, 'engulfing': 4, 'displacement': 8}. Expanded liquidity counts are saved as {'expanded_detector_zone_sweep': 1478, 'expanded_high_quality_zone_sweep': 362, 'expanded_sell_sweep': 1518, 'expanded_buy_sweep': 1567, 'current_detector_zone_sweep': 10}; the current production detector only uses the latest swing sweep, while the research detector adds equal/local/session levels. The `bot_quality_B_details` collection contains the current B-quality production zones; `quality_B_details` contains B-class reference-only zones. MTF research is loaded from the deep-logic artifact for 17 assets across 1m/5m/15m/1h/4h; it is descriptive and does not feed production decisions.

## Missed-opportunity and V6.4 recommendations

Causal replay examples with favorable expected-direction excursion include BLESSUSDT, MARSCOINUSDT and FARTCOINUSDT; they remain potential false positives/negatives to investigate because contemporaneous orderbook/news are unavailable. MUST FIX: persist per-stage cohort evidence, per-endpoint timing, and independent detector diffs. SHOULD FIX: relative orderbook depth, expanded liquidity pools, and separation of context score from hard invalidation. OPTIONAL: reversal 4h branch and objective rejection variants. DO NOT CHANGE without outcome evidence: anti-repaint, full-fill/CE invalidation, cost/risk checks.

Charts/replays: `data/audits/deep_logic_v6.3/setup_replays.json` and 10-chart reconstruction set. `Signal MAY FORM` remains diagnostic only.
