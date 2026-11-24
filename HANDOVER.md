# Handover · 2026-11-24

- PR #3 merged into main at `9ca4175`. The visual refinement starts from that commit on `docs/visual-design-refinement`.
- The README keeps the documentation-only status, privacy, hardware and cost caveats visible. Default-visible prose is 403 words and all five disclosures remain. [The product illustration](docs/assets/bunny-overview.svg) is editable SVG with accessible text and a light and dark palette. The rabbit motif is exploratory.
- Mermaid covers the roadmap, component map, billable text turn, logical roles, identity layers and privacy flows. [Diagram conventions](docs/ARCHITECTURE.md#diagram-conventions) document the division between visual labels and adjacent explanations.
- Local validation used headless Chrome and Mermaid 11.17.2 at 1120 px and 390 px in both themes. The two README compositions and the baseline were compared locally. The component map improved from 1,354 px to 560 px native width. Dense technical labels may still benefit from zoom on phones.
- The specifications leave the integration wiring for billable non-model actions and the relative order of streaming, response delivery and persistence open.
- Next remains [v0.1 Awakening](docs/ROADMAP.md#v01---awakening). The reference hardware is unvalidated. Dedicated security and conduct inboxes are pending.
