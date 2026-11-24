# Bunny.ai context

Last reconciled 2026-11-24.

## What it is

Bunny.ai is an open-source, hardware-independent, provider-agnostic personal assistant built around ownership: identity, memories, configuration and hardware belong to the user, and the intelligence engine is interchangeable. Apache-2.0. This public repository is a documentation-only, pre-release foundation — no runnable implementation exists yet.

## Current state

- Branch `docs/project-foundation` at `cda891e` (2 commits): vision plus foundational architecture documents, every one status-labelled as proposed design.
- Documents: README, CONTRIBUTING, CODE_OF_CONDUCT, SECURITY, LICENSE, and docs/{VISION, ARCHITECTURE, AWAKENING, PERSONALITY, MEMORY, PRIVACY, ROADMAP, DECISIONS, REVIEW-2026-11-24}. All relative links resolve.
- Decision log D-001..D-022: accepted core (licence, documentation-first, no private-prototype import, gateway, local-first memory, deterministic security, budget-before-dispatch); provisional SQLite (D-007) and Pi 4 reference target (D-015); stack, voice, packaging, encryption open. Staged security review gates and community Code of Conduct adopted.
- All 13 findings from the independent review (MiMo V2.6 Flash, 2026-11-24) recorded in docs/REVIEW-2026-11-24.md are resolved.

## Non-negotiable constraints

- D-002/D-017: documentation-first; status banners stay honest; no placeholder code.
- D-003: no import of the private prototype — no personal, hardware or credential data ever enters this repository.
- D-012/D-013: permissions, spending limits and approvals are deterministic code; budget reserved before dispatch.
- D-006/D-009: local-first memory with honest provider disclosure; three-layer personality; core identity never autonomously rewritten.
- Deny-by-default tools, untrusted retrieved content, no secrets in any file, no auto-commit or push.

## Where the detail lives

| Topic | Document |
|---|---|
| Vision, principles, non-goals | docs/VISION.md |
| Components, request lifecycle, trust boundaries | docs/ARCHITECTURE.md |
| Decisions and open questions | docs/DECISIONS.md |
| Milestones and exit criteria | docs/ROADMAP.md |
| Memory, personality, first run | docs/MEMORY.md, docs/PERSONALITY.md, docs/AWAKENING.md |
| Privacy and security model | docs/PRIVACY.md, SECURITY.md |
| Contribution, conduct and licensing rules | CONTRIBUTING.md, CODE_OF_CONDUCT.md |
