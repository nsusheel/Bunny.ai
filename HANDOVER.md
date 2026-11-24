# Handover · 2026-11-24

- PR #2 was verified merged into main at `3f22696`. The architecture visualisation branch was created from that synchronised base with a clean working tree.
- The README now keeps the documentation-only status, ownership, privacy, hardware, costs and v0.1 preview visible. Five disclosures hold examples, origin, logical roles, operating details and later milestone summaries. Engineers and contributors can go directly to canonical docs.
- Seven Mermaid views cover product, roadmap, components, logical roles, a billable text turn, local identity and memory, and privacy flows. Conventions are in [Architecture](docs/ARCHITECTURE.md#diagram-conventions).
- The redesigned views preserve deterministic permission checks and pre-dispatch reservations. The specifications leave the integration wiring for billable non-model actions and the exact streaming and persistence ordering open.
- Local validation used Mermaid 11.17.2 and headless Chrome with temporary dependencies. The preview covered 1120 px and 390 px in both themes, with all five README disclosures exercised. Visible words fell from 2,057 to 403 and total words from 2,057 to 1,199, excluding Mermaid source and diagram labels. The denser canonical diagrams need zoom on mobile.
- Next milestone remains [v0.1 Awakening](docs/ROADMAP.md#v01---awakening). The reference hardware is unvalidated. Dedicated security and conduct inboxes remain pending for public release.
