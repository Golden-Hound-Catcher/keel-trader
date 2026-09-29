# Backtest: no LLM entry when 4h is neutral (2026-09-29)

Data: every OKX demo closed position since 2026-09-20 00:00 CST (`/account/positions-history`,
`realizedPnl` = pnl + fees + funding; 43 positions, all LLM primary), joined to the entry decision
(`order_sized.decision_id` → `decisions.calculus_data` trend_15m/1h/4h, ADX, confidence).
IS = 9/20 → 9/25 17:30, OOS = 9/25 17:30 → 9/29 09:30. Active days from `worker_cycle_summary`
count × 300 s (OOS had only ~1.0 active day because of the 58.5 h outage + 9/29 reboot).

Existing knob: `KEEL_LLM_4H_MODE=hard` (`keel/policy/edge_overlay.py::_htf_ok`); llm_demo runs
`soft` + F10 (4h neutral ⇒ 15m same-dir) + `KEEL_LLM_SOFT4H_BLOCK_15M_NEUTRAL=1`, so the
"4h neutral needs 15m+1h alignment" variant is **already live** — every 4h-neutral trade below had
15m=1h=bearish.

| variant | IS n / net / WR / fires·d⁻¹ | OOS n / net / WR / fires·d⁻¹ | ALL n / net / WR / fires·d⁻¹ |
|---|---|---|---|
| baseline (soft, current) | 35 / −4.18 / 29% / 6.1 | 8 / −21.88 / 12% / 8.0 | 43 / −26.06 / 26% / 6.4 |
| **A hard (4h must align)** | 24 / **+17.62** / 42% / 4.2 | 1 / **+4.65** / 100% / 1.0 | 25 / **+22.27** / 44% / 3.7 |
| B 4h-neutral needs conf ≥ 75 | 26 / +10.87 / 38% / 4.5 | 2 / −2.20 / 50% / 2.0 | 28 / +8.66 / 39% / 4.2 |
| C 4h-neutral needs ADX ≥ 30 | 30 / +3.55 / 33% / 5.2 | 4 / −2.63 / 25% / 4.0 | 34 / +0.93 / 32% / 5.1 |
| D 4h-neutral half size (theoretical) | 35 / +6.72 / 29% / 6.1 | 8 / −8.61 / 12% / 8.0 | 43 / −1.89 / 26% / 6.4 |
| E 4h-neutral needs 4h EMA9<21 & px<EMA21 lean | 24 / +17.62 / 42% / 4.2 | 4 / −9.71 / 25% / 4.0 | 28 / +7.91 / 39% / 4.2 |

* 4h-neutral trades: 18, **0 wins**, net −48.33 (all shorts; 4h was never `bearish` in the window).
* Top 8 winners (+17.83 … +4.65) are all 4h-bullish longs → none removed by A.
* Daily fires under A: 9/20 2, 9/21 11, 9/22 5, 9/23 4, **9/24 0**, 9/25 3, **9/28 0**, 9/29 0
  (days where 4h was neutral ≥97 % of cycles). Zero-fire days are regime-driven, not a bug.
* D is not implementable for BTC/ETH today: all BTC/ETH entries are 1 contract (min lot).
* Caveats: 43 trades, OOS only 8 (1 active day); 4h-neutral ≡ counter-(lagging)-4h-bull shorts in
  this sample, so A is partly a "no shorts" filter here.

Recommendation (Jo decides; not deployed): `KEEL_LLM_4H_MODE=hard`, with an alert if 0 fires for
>48 h so a long neutral regime is noticed rather than mistaken for an outage.
