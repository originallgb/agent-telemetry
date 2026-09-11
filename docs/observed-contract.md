# Observed CodexBar contract — v0.55.1, macOS, 2026-08-27

Empirical. Every claim below came from running the binary, not from docs.

## Enabled providers (config: ~/.config/codexbar/config.json, 69 entries, 3 enabled)
- `codex`    — source resolves to `oauth`, dataConfidence `exact`
- `claude`   — source `cli` (explicitly pinned in config), dataConfidence `percentOnly`
- `antigravity` — token account (Google OAuth material stored in config), source `cli`

## THE CENTRAL FINDING: pace lives in `usage`, not `dashboard`
`dashboard-v1` window objects contain ONLY: kind, label, remainingPercent, usedPercent, resetAt.

`usage --format json` additionally contains, per provider, a `pace` block:
  stage, deltaPercent, expectedUsedPercent, willLastToReset, etaSeconds, summary
plus `usage.*.windowMinutes`, `dataConfidence`, `codexResetCredits`, `extraRateWindows`.

The brief mandates preserving pace/expectedUsedPercent/willLastToReset AND using `dashboard`
as the cron source. Those two requirements are incompatible. `usage` wins.

Live example (claude, session window):
  stage=farAhead deltaPercent=21 expectedUsedPercent=51 willLastToReset=false etaSeconds=3595
  summary="21% in deficit | Expected 51% used | Projected empty in 1h"
That sentence IS the primary use case. It does not exist in dashboard-v1.

## Cache lanes under `codexbar serve` (probed on 127.0.0.1:8899, --refresh-interval 300)
- `/usage` repeated immediately  -> identical updatedAt. CACHED.
- `/dashboard/v1/snapshot` repeated -> identical generatedAt. CACHED.
- `/usage?provider=codex` AFTER a snapshot -> NEW upstream fetch (21:39:44 -> 21:40:10).

=> `/usage` and `/dashboard/v1/snapshot` are SEPARATE cache lanes. Polling both doubles the
   upstream rate. Pick one. `/usage` is strictly richer at identical upstream cost.
- `/usage` DOES include the pace block over HTTP. This is what makes serve viable as the
  single upstream-touching process.

## staleAfterSeconds differs by mode (corrects the brief)
- one-shot `codexbar dashboard`: staleAfterSeconds=180, host.refreshIntervalSeconds=0
- under `codexbar serve`:        staleAfterSeconds=900, host.refreshIntervalSeconds=300
Under serve, 900 > the 300s poll, so it is NOT self-stale. Do not map it to HA `expire_after`
regardless; 180 in one-shot mode would flap every entity to unavailable between polls.

## Cost is already coverage-modelled — do not invent a coverage field
`/cost` (local session-log scan, NO upstream fetch, cached unless --refresh) returns per provider:
  provenance ("listPriceEstimate"), coverage {estimated, priced, unpriced, unmetered},
  historyCoverageIsEstablished, source, projects[], daily[] with modelBreakdowns,
  last30DaysCostUSD, sessionCostUSD, sessionTokens, historyDays, currencyCode
Observed: codex $301.55/30d (coverage priced=9 unpriced=1), claude $118.12/30d (priced=4).
Propagate CodexBar's own coverage/provenance rather than inventing a source-host field;
add host attribution ON TOP of it.

## Rate-limit hazard found
`--provider all` means all 69 providers, NOT the 3 enabled. It exits 1 and attempts fetches
against every configured provider. Never use it in a poll loop. `dashboard` honours enabled
providers only; per-provider `usage` calls are the safe equivalent.

## Guard is too thin for the decision path
`guard --json` returns only: decision, exitCode, provider, window, remainingPercent,
minimumRemainingPercent, unavailableReason. No pace, no eta.
`--window` accepts only session|weekly, so it CANNOT address antigravity's four
`extraRateWindows` (gemini-5h, gemini-weekly, 3p-5h, 3p-weekly).
=> Phase 2 MCP must read `/usage`, using guard only for the binary gate.

## Provider-shaped gaps
- antigravity has NO pace block at all, and its real quota lives in `extraRateWindows[]`
  with dynamic ids (antigravity-quota-summary-*). Its primary/secondary are BOTH weekly
  windows (windowMinutes 10080) — there is no session window to guard on.
- claude has no identity.accountEmail and dataConfidence=percentOnly under source=cli.
- codex exposes `codexResetCredits.availableCount=1` — a free full rate-limit reset,
  planning-relevant and absent from dashboard-v1.

## hooks
`hooks list` -> {"enabled": false, "events": []}. Nothing configured. Rules live in the
shared config file and must be authored before `hooks watch` does anything.
