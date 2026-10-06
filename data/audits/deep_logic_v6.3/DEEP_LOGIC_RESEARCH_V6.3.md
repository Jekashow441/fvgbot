# Deep trading-logic research — V6.3 (read-only)

18 unique technical candidates / 24 observations. No production changes.

## Decision tree and counterfactuals

| Filter | Current blocked | Continue if only this blocker removed |
|---|---:|---:|
| `thin_orderbook` | 15 | 2 |
| `extended_from_vwap` | 10 | 1 |
| `higher_4h_opposition` | 9 | 0 |
| `wide_spread` | 2 | 0 |
| `fvg_fully_filled` | 2 | 0 |
| `orderbook_unavailable` | 1 | 0 |
| `higher_4h_unavailable_or_stale` | 1 | 0 |
| `correlated_btc_opposition` | 1 | 0 |

### Filter classification

| Filter | Candidates blocked | Share of 18 | Current class | Evidence / interpretation |
|---|---:|---:|---|---|
| thin_orderbook | 15 | 83.33% | HARD execution gate | Absolute 25k USDT entry-side depth; only 2 continue when removed alone, but cross-asset normalization is absent |
| extended_from_vwap | 10 | 55.56% | HARD technical gate | Only 1 continues when removed alone; useful context signal, but no standalone outcome proof |
| opposite 4h | 9 | 50.00% | HARD context gate | Zero continue alone because all affected setups have other blockers; reversal evidence missing |
| no rejection | 0 in this candidate set | n/a | HARD confirmation | Night-wide observation blocker; current rule requires directional close and CE relation |
| FVG filled | 2 | 11.11% | HARD invalidation | Lifecycle rule prevents re-entry after full wick/CE invalidation |
| spread | 2 | 11.11% | HARD cost gate | Critical only above configured 15 bps; both also had other blockers |

### 18 candidate trees (P=PASS, F=FAIL, U=UNKNOWN)

| Asset/setup | FVG | Quality | Structure | Liquidity | HTF | VWAP | Book | Spread | Mitigation | Rejection | Entry | Signal |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| AXSUSDT SHORT | P | P | P | U | F | P | F | P | P | P | P | F |
| BASEDUSDT SHORT | P | P | P | U | P | P | F | P | F | P | P | F |
| BLESSUSDT SHORT | P | P | P | U | P | F | F | P | P | P | P | F |
| CFGUSDT LONG | P | P | P | U | P | P | F | F | P | P | P | F |
| CRCLUSDT SHORT | P | P | P | U | P | F | P | P | P | P | P | F |
| EIGENUSDT SHORT | P | P | P | U | F | F | F | P | P | P | P | F |
| EPICUSDT SHORT | P | P | P | U | F | P | F | P | P | P | P | F |
| FARTCOINUSDT SHORT | P | P | P | U | P | F | F | P | P | P | P | F |
| MARSCOINUSDT SHORT | P | P | P | U | P | F | F | P | P | P | P | F |
| MORPHOUSDT SHORT | P | P | P | U | P | P | F | P | P | P | P | F |
| NOTUSDT SHORT | P | P | P | U | F | P | F | P | P | P | P | F |
| ORDIUSDT SHORT | P | P | P | U | F | P | F | P | P | P | P | F |
| ORDIUSDT SHORT | P | P | P | U | F | F | F | P | P | P | P | F |
| POPCATUSDT SHORT | P | P | P | U | P | F | F | P | P | P | P | F |
| ROBOUSDT SHORT | P | P | P | U | P | F | F | F | P | P | P | F |
| TRUMPUSDT SHORT | P | P | P | U | P | F | P | P | P | P | P | F |
| ZAMAUSDT SHORT | P | P | P | U | P | F | F | P | P | P | P | F |
| ZKCUSDT SHORT | P | P | P | U | F | P | F | P | F | P | P | F |

The 18 trees are also machine-readable in JSON. `liquidity` is UNKNOWN when the archived payload contains no sweep object; it was not treated as a failure gate.

## Findings

Pure classic OHLC reference comparison on 470 saved 5m files: both=105, bot-only=0, reference-only=3424. The reference-only set is the missed-FVG candidate pool, but it includes intentionally stricter bot rules (body, ATR, volume, size and mitigation), so it is not proof of a bug.
Causal prefix comparison for the 18 candidate boundaries: both=15, bot-only=0, reference-only=120; no candles after each analysis_bar were used.
Quality matrix (research definition, not production score): {'B': 74, 'C': 29, 'A': 2}.
Saved 5m data quality: {'files': 470, 'rows': 11223933, 'duplicate_rows': 0, 'out_of_order_files': 0}.
MTF map for the 17 unique candidate assets has data on all requested 1m/5m/15m/1h/4h files; counts are reference OHLC zones and are descriptive, not production signals.

## thin_orderbook

Current implementation sums USDT depth inside a 20.0 bps band and rejects if entry-side depth is below 25,000 USDT when the band is complete. The threshold is absolute and is not normalized to price, recent volume, ATR or market cap. Current context samples show large cross-asset variation; this is the clearest candidate for a future relative-depth experiment, but no production change was made.

## VWAP, 4h and rejection

Single-filter arithmetic says removing only VWAP would allow 1 of 18 to continue; removing only thin orderbook would allow 2; removing only higher-4h opposition, spread, full-fill or data-unavailable gates allows 0 because each affected setup has another blocker. A preferred-but-not-mandatory 4h mode therefore changes none of these 18 to Confirmed without another change.
The rejection gate is objective but narrow: directional candle body and close relative to CE. Wick rejection, engulfing, multi-candle displacement and MSS are not separate acceptance paths.

## Score/filter duplication audit

HTF alignment is counted in the heuristic score (+18 for aligned trend and -25 for opposite directional HTF) and is also enforced as a hard rejection later; this is double use of the same directional information. FVG size contributes a score bonus while FVG validity/mitigation is separately hard-gated, which is intentional but can make a high score look stronger than the final gate set. VWAP, orderbook, spread and liquidity are not score components in `_score_signal`; they are later context/execution gates or informational factors. Displacement and relative volume participate in FVG/entry validation and quality, but are not independently added as score points. No evidence of a third penalty for these fields was found in the current path.

## Setup replay and missed-opportunity evidence

Causal T-20→decision replays were generated for all 18 unique candidates. The following 12 bars are stored separately as descriptive outcome evidence only; they were not used to create signals or relabel rejections. Examples with favorable excursion in the expected direction include BLESSUSDT SHORT (close -6.88%, aligned excursion 14.87%), MARSCOINUSDT SHORT (-3.67%, 5.87%) and FARTCOINUSDT SHORT (-1.59%, 4.83%). These are potential false-negative candidates, not proof that the blocked execution would have been fillable or profitable because historical orderbook/news snapshots are unavailable.
Debug charts were rendered for AXSUSDT, MORPHOUSDT and TRUMPUSDT with candles, FVG/CE, swings and decision marker.

## Gaps and V6.4 plan (not implemented)

1. Add per-endpoint monotonic timings and p50/p95/p99. 2. Build a relative orderbook metric using entry-side depth / recent traded volume with a minimum data-quality guard. 3. Run a causal independent-detector diff on every historical prefix. 4. Add equal-high/low and session liquidity pools. 5. Store Raw/Valid/Quality/TradeCandidate/Confirmed as explicit events. 6. Keep VWAP and 4h as research branches until outcome data separates precision from recall. 7. Add setup replay and a debug chart endpoint.
