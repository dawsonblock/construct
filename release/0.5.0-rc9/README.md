# Release 0.5.0-rc9

Focused on:
1. Proving the exact packaged artifact (Qualified Source Payload = Packaged Payload = Verified Final ZIP)
2. Eliminating the last supersession race (Atomic + Serialized)

No new runtime features.

## Baseline

- Tag: 0.5.0-rc8-audit-baseline
- SHA: 972b5bfeb75c38d614ea1d9b484631b0da37b078
- Version: 0.5.0-rc8.dev0

## Changes allowed

Only release-blocking fixes:
- Supersession atomicity (FOR UPDATE, state machine, audit)
- Release pipeline (dist/, qualify-release, packaged verification)
- Version normalization to 0.5.0-rc9.dev0
- Test hardening (concurrency, regression, classification)

