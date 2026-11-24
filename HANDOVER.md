# Handover — 2026-11-24

## Purpose

Improve the public-facing README.md for Bunny.ai so first-time visitors (non-technical users, software engineers, and prospective open-source contributors) immediately understand the project's purpose, inspiration, intended experience, agent architecture, operating costs, and roadmap while maintaining strict documentation-only pre-release boundaries.

## Changes Made

- **Public Onboarding & Tagline (`README.md`):** Clarified what Bunny is, why it exists (decoupling identity/memory from rental cloud models), who it is for, and preserved core messaging ("Your hardware, your choice of intelligence, your memories, your Bunny"). Maintained honest pre-release disclaimer.
- **Illustrative Experience:** Added "What living with Bunny could feel like" featuring two hypothetical dialogues (landscape photography hobby and dawn tide/weather assistance) demonstrating natural conversation, curiosity, persistent memory, and explicit consent boundaries. Explicitly disclaimed sentience, emotional dependency, or implemented functionality.
- **Project Origin:** Added a tasteful, authentic Founder's note explaining the inspiration behind Bunny (a childhood stuffed rabbit symbolizing enduring comfort and continuity) and connected it to the engineering vision of portable assistant identity across models and machines.
- **Logical Agent Roles:** Detailed Neon (conversational interface), Otto (routine maintenance, memory hygiene, reflection), and Scout (sandboxed research) in plain English. Clarified they share a single assistant identity, run under a single provider/process, and remain strictly governed by permission and budget boundaries. Removed redundant ASCII diagram to enhance mobile readability.
- **Operating Costs & Budget Boundaries:** Clarified that Bunny is free and open-source, cloud API tokens are billed by providers, one API key is sufficient, pre-dispatch budget reservations prevent dispatch when limits are reached within Bunny's accounting boundary, and acknowledged external factors (uncertain charges, out-of-band pricing changes).
- **Founder's Note & Scenario Grounding:** Removed unverified biographical details from the founder's note (preserving core childhood keepsake inspiration); explicitly disclaimed illustrative conversation details (places, times, tides) as fictional scenario data; reconciled provenance statement with published founder origin.
- **Roadmap Preview & Progressive Disclosure:** Added a visual Mermaid timeline and structured milestone summary derived from canonical `docs/ROADMAP.md` (completed foundation, v0.1 awakening, v0.2 personality, v0.3 voice/delegation, v0.4 assistant, v1.0 community). Reorganized document flow from high-level onboarding to technical details.

## Validation Performed

- Checked all 23 internal and relative documentation links via automated script (`docs/*.md`, `LICENSE`, `CONTRIBUTING.md`, `CODE_OF_CONDUCT.md`, `SECURITY.md`); all resolve.
- Validated Mermaid syntax across all diagrams (`flowchart TB` canonical derivative and `flowchart LR` roadmap).
- Verified `git diff --check` with 0 whitespace errors or malformed lines.
- Conducted 3-perspective first-time reader review (non-technical user, engineer, contributor) answering all 10 evaluation questions.
- Confirmed zero credentials, private infrastructure details, or unvalidated hardware claims.

## Outstanding Concerns & Notes

- Hardware targets (Raspberry Pi 4 4GB, cross-platform targets) remain unvalidated design constraints until v0.1 implementation benchmarks.
- Dedicated project security and conduct reporting email inboxes remain pending owner setup prior to v1.0 public release (fallback to GitHub reporting and maintainer profile currently documented).

## Next Intended Development Milestone

- **v0.1 Awakening:** Minimal text assistant on reference hardware, single-provider configuration, Neon conversational role, seed personality, local memory storage (provisional SQLite), and minimal pre-dispatch budget gate (D-013).
