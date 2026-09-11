# Phase 1 entity map

This document defines the Home Assistant entity model for Phase 1. It is derived from
the [observed CodexBar v0.55.1 contract](observed-contract.md). The bridge consumes only the
bare, cached `GET /usage` response from a loopback-bound `codexbar serve` process.

## Device boundary

Publish one Home Assistant device for the telemetry bridge on the collector host. All
provider/window entities belong to that device. Provider and window identifiers form the
stable part of each entity's MQTT discovery `unique_id`; display labels are presentation
only and may change without creating a new entity.

The entity set must be generated from each response. Do not assume that every provider has
session and weekly windows, and do not hardcode the observed Antigravity bucket list.

## Dynamic window enumeration

For every healthy provider row:

1. Read every member of `usage.extraRateWindows[]` and use its `id` as the stable bucket
   key. The human-facing `title` is only the label.
2. When no detailed extra-window set represents the provider's quota buckets, read each
   non-null named window under `usage` (`primary`, `secondary`, and any future
   window-shaped member supported by the normaliser). Do not duplicate `primary` or
   `secondary` aliases when the detailed extra-window set already represents them.
3. Keep the selected source bucket key in the entity identity. Do not rename a `primary` window to
   `session`, or a `secondary` window to `weekly`, based only on `windowMinutes`.
4. Emit a remaining-percent entity and a reset-timestamp entity for each bucket.

This distinction is necessary because the observed Antigravity `primary` and `secondary`
windows are both 10,080 minutes, and its actual quota detail is in dynamically named
`extraRateWindows[]` buckets such as `antigravity-quota-summary-gemini-5h`.

## Entities per quota bucket

| Entity | State | Home Assistant metadata | Attributes |
|---|---|---|---|
| Remaining quota | `100 - usedPercent` | `device_class: battery`; `unit_of_measurement: "%"`; `state_class: measurement` | Provider, source bucket key, `windowMinutes`, `dataConfidence`, and the pace fields described below when present |
| Reset time | `resetsAt` | `device_class: timestamp` | Provider, source bucket key, `windowMinutes`; `resetDescription` may be included as explanatory text |

Preserve the numeric precision supplied by CodexBar unless Home Assistant requires a
presentation rounding rule. Clamp only to defend against invalid upstream values; do not
silently convert a missing `usedPercent` into zero.

The timestamp state is the ISO 8601 value from `resetsAt`. A bucket without `resetsAt`
must not publish a fabricated timestamp.

## Pace attributes

For named buckets, match `pace.<bucket-key>` to `usage.<bucket-key>`. When a pace block is
present, attach these fields to the remaining-quota entity:

- `stage`
- `deltaPercent`
- `expectedUsedPercent`
- `willLastToReset`
- `etaSeconds`
- `summary`

Fields are independently optional. Omit missing fields rather than supplying false,
zero, or empty defaults. The observed Antigravity row has no pace block, which is a valid
provider shape and must not make its quota entities unavailable. No pace-to-extra-window
mapping is defined by the observed contract, so do not infer one.

## Provider confidence

Expose the row's `usage.dataConfidence` on every remaining-quota entity for that provider.
Consumers must be able to distinguish observed `exact` data from Claude's observed
`percentOnly` data. A missing value remains unknown; do not promote it to `exact`.

The fixture captured on 2026-08-27 records Claude as `percentOnly`. Antigravity is healthy
through `source=cli` after the credential remediation described in the
[operator runbook](operator-runbook.md); its lack of pace remains expected.

## Codex reset credits

When `usage.codexResetCredits` is present, publish a provider-level numeric entity from
`availableCount`. It is not a quota-window entity and must not be duplicated once per
bucket. The observed Codex fixture contains one available reset credit.

Useful non-secret attributes may include the upstream `updatedAt`. Do not publish OAuth
material or any credential-store content as entity state or attributes.

## Provider errors and partial snapshots

Treat an `error` object on one provider row as a row-level failure. Continue normalising
and publishing every healthy row even if the HTTP response is non-200 or another provider
failed. Do not clear healthy entities because one provider failed.

For a failed provider row, retain the last known quota state rather than replacing it with
zero or an invented unavailable state. Error diagnostics may be logged, but must be
sanitised and must never include tokens.

## MQTT retention and availability

Retain both Home Assistant discovery messages and state messages. This allows the last
known reading to survive a collector restart. After a broker restart, state must be
restored either from the broker's persisted retained store or from the collector's full
retained republish on reconnect.

The bridge owns only the exact discovery, state, and attributes topics recorded in its
private retained inventory manifest. That manifest is scoped by the configured MQTT topic
prefix and collector host identity; it is not a Home Assistant discovery entity. After a
complete successful source response with no provider errors, the bridge publishes the
complete desired topic map, tombstones obsolete manifest entries with an empty retained
QoS 1 payload, and writes the replacement manifest last. A failed publish leaves the
previous manifest authoritative so reconciliation can be retried safely.

Partial and error snapshots do not perform deletion or advance the manifest. Healthy rows
in such a snapshot may refresh their existing values, but a failed or omitted provider's
last-known topics remain retained. The manifest governs topics published after this
lifecycle was introduced; historical pre-manifest topics require an explicit operator
cleanup rather than broad broker-wide adoption.

Use a generous `expire_after` or omit it. Do not derive entity availability from
CodexBar's `staleAfterSeconds`: it is 900 under `serve`, describes the upstream snapshot,
and is not the Home Assistant entity lifetime. A stale retained value is intentionally
preferred to an unavailable ambient display in Phase 1.

Phase 1 is complete only when the retained state round-trip has been tested with real
authenticated broker credentials and the entities survive both collector and broker
restarts. TCP reachability to port 1883 alone does not satisfy this requirement.
