# Handover — 2026-11-24

## Completed

- **13 Review Findings Resolved:** Addressed all 13 findings from `docs/REVIEW-2026-11-24.md` across architecture, roadmap, security, decisions, contributing, and memory documentation.
- **Code of Conduct:** Adopted `CODE_OF_CONDUCT.md` with verified reporting to repository maintainers via profile contact (dedicated inbox pending owner setup), linked from `CONTRIBUTING.md` and `README.md`.
- **Verified Reporting Channels:** Cleaned all documentation to reference only verified reporting channels (GitHub Private Vulnerability Reporting; direct maintainer contact on repository profile); marked dedicated project email inboxes as pending owner setup. Removed any unowned domain addresses.
- **Validation:** Verified all markdown relative links across the repository (all resolve). Checked committed diff for whitespace errors, sensitive data, and unsupported claims.
- **Context Updated:** Reconciled `CONTEXT.md` and updated `docs/REVIEW-2026-11-24.md` marking all findings as Resolved.

## Resolutions Summary

| ID | Sev | Focus | Resolution Summary | Key Files |
|---|---|---|---|---|
| F-01 | High | Budget gate | Added minimal pre-dispatch reservation check to v0.1 deliverables and exit criteria; documented staged schedule under D-013 | `docs/ROADMAP.md`, `docs/DECISIONS.md` |
| F-02 | Med | Permission diagram | Connected Scout→PERM, PERM→TOOLS, PERM→WEB in both diagrams; reinforced no-bypass rule | `docs/ARCHITECTURE.md`, `README.md` |
| F-03 | Low | Gateway/meter arrows | Standardized both diagrams on `GW <--> METER` (reserve/settle) and `GW --> CLOUD/LOCAL` | `docs/ARCHITECTURE.md`, `README.md` |
| F-04 | Low | Neon role in v0.1 | Explicitly designated Neon as the conversational role powering v0.1 text interaction | `docs/ROADMAP.md` |
| F-05 | Low | Model config mapping | Clarified that Bunny's conversational voice uses Neon's model by default unless overridden | `docs/ARCHITECTURE.md` |
| F-06 | Low | Security contact & discussions | Configured GitHub private reporting with fallback to maintainer profile contact; marked dedicated security inbox as pending owner setup; noted GitHub Discussions status | `SECURITY.md`, `CONTRIBUTING.md` |
| F-07 | Med | Rate limits, retries, charges | Added gateway resilience section: attempt caps, backoff+jitter, `Retry-After`, uncertain-charge reservation hold | `docs/ARCHITECTURE.md`, `docs/DECISIONS.md` |
| F-08 | Med | Pricing & spending controls | Specified versioned pricing tables, fail-closed/ceiling stale price policy, and hard caps vs soft alerts | `docs/ARCHITECTURE.md`, `docs/DECISIONS.md`, `docs/ROADMAP.md` |
| F-09 | High | Security review staging | Staged security review gates before v0.3 (tools/device actuation), before v0.4 (MCP/skills), and v1.0 | `docs/ROADMAP.md`, `SECURITY.md`, `docs/DECISIONS.md` |
| F-10 | Med | Network egress & SSRF | Added egress IP validation against private/reserved ranges, redirect hop revalidation, response caps | `SECURITY.md`, `docs/ARCHITECTURE.md`, `docs/ROADMAP.md` |
| F-11 | Low | Canonical diagram | Designated `docs/ARCHITECTURE.md` as canonical; marked `README.md` diagram as derivative | `docs/ARCHITECTURE.md`, `README.md` |
| F-12 | Low | Code of conduct | Adopted `CODE_OF_CONDUCT.md` with maintainer profile reporting (dedicated conduct reporting infrastructure pending owner setup) | `CODE_OF_CONDUCT.md`, `CONTRIBUTING.md`, `README.md`, `docs/DECISIONS.md` |
| F-13 | Med | Context engineering | Added context engineering section: prompt layer budgets, composition order, Otto summarisation, cache pricing | `docs/ARCHITECTURE.md`, `docs/MEMORY.md`, `docs/DECISIONS.md` |

## Local Commit Status

- Ready for local commit on `docs/project-foundation`. No push or merge performed.

## Remaining Owner Actions

1. Review resolved findings and updated diagrams in `docs/ARCHITECTURE.md` and `README.md`.
2. Set up dedicated project security and conduct reporting mailboxes/infrastructure prior to public release.
3. Enable GitHub Private Vulnerability Reporting in repository security settings upon publication.
4. Future architectural spikes: evaluate stack (D-018) and voice (D-019) benchmarks on Raspberry Pi 4 reference hardware.
