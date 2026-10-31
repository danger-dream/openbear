# Service Unlock v0.5.4 — source and license notice

> Modified 2026-10-18 for v0.5.1: NotebookLM, ChatGPT App and JCOM removed; WOWOW, Spotify, Abema and DeepSeek response classification repaired; Hulu Japan receives one bounded HTTP/1.1 fallback but remains unresolved in live testing. The anonymous Spotify POST/strict classifier is derived from fixed RRC MediaUnlockTest_Spotify and is AGPL-3.0-only in internal/extra/spotify_signup.go; internal/geo contains its integration adapter, retaining original MIT notices. Other repair files in internal/extra retain AGPL. Exact modified Corresponding Source is included with this release; GUKO is not deployed by this script publication.

Original service-unlock v0.4.3 (7d032c4) and geocheck v0.3.0 code and their
MIT notices remain preserved. New native extra probes are translations from
lmc999/RegionRestrictionCheck b6d4a6f9a87fc6eae6d3e62d0092ececcec8e844,
Copyright the RegionRestrictionCheck contributors, licensed AGPL-3.0.

Modified 2026-10-18: selected functions translated from shell/curl to Go,
explicit HTTPS/TLS/direct HTTP client, conservative errors, safety exceptions,
fixed anonymous client data, global+one-region scheduling and unified output.
Each probe's source function, line, groups and source hash are recorded in
internal/extra/catalogue-source.json. All new files there are AGPL-3.0-only;
unified.go and internal/render/extra.go carry the same extension license.
MIT notices on pre-existing files remain notices on those original portions,
not an assertion that the combined executable is MIT-only.

Modified 2026-10-18 for v0.5.2: default automatic exit-country detection and
regional scheduling, explicit manual overrides and bounded global fallback.
No existing platform probe predicate or original network strategy was changed.

Modified 2026-10-18 for v0.5.3: one rounded report frame with internal sections
and one column header; per-group status counts and explanations removed.
Only presentation changed; probe, JSON and automatic-region behavior is preserved.

Modified 2026-10-18 for v0.5.4: narrowed Spotify signup classification.
Undocumented status311, bare120/320 and country-launch flags cannot establish
registration eligibility. Only the observed explicit legacy proxy refusal yields
a scoped No; all other unproven outcomes are Unknown. Incomplete anonymous
request fields, renderer, fonts and layout remain unchanged. No account was created.

Exact Corresponding Source, build files, fixed data, tests, license texts and
modification notices for this release are available at no charge:
https://github.com/shui1iao/service-unlock/tree/v0.5.4
https://github.com/shui1iao/service-unlock/releases/download/v0.5.4/service-unlock-v0.5.4-source.zip
The source archive is offered beside the matching executable assets on the
same release page. The build instructions are in README.md. Dependencies are
pinned in go.mod/go.sum; their source is available through the Go module proxy
and the respective upstream projects. Network deployments must continue to
provide their actual modified Corresponding Source to interacting users as
required by AGPL sections 5, 6 and 13. This script release does not deploy GUKO.

Fixed cookies supplied only anonymous client/device form and GraphQL data.
No account credentials, Neon UpdateAccount data, weak TLS, or HTTP target
was copied into the runnable extra catalogue. See README-unified-native.md.
