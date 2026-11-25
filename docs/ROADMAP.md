# Roadmap

> **Status:** proposed milestones. The experimental offline scaffold begins Awakening.
> No milestone below is complete, and no release dates are promised.

The roadmap sequences work so that identity, memory and safety exist before voice,
autonomy and device control. Each milestone should produce something genuinely usable,
not a demonstration that merely looks complete.

## Milestone overview

| Milestone | Theme | Primary outcome |
|---|---|---|
| **v0.1** | Awakening | A minimal text assistant with a seed personality, Neon conversation role, persistent local identity, basic memory, minimal budget gate, and one provider. |
| **v0.2** | Growing personality | Structured personality evolution, memory management, reflection, and hard/soft spending controls. |
| **v0.3** | Voice and delegation | Wake-word interaction, optional speech processing, Neon/Otto/Scout delegation with permissions, and delegation/tool security gate. |
| **v0.4** | Personal assistant | MCP integration, skills, scheduled tasks, notifications, optional dashboard, and egress/SSRF security gate. |
| **v1.0** | Community-ready | Documented installation, cross-platform validation, stable extension interfaces, and final comprehensive security review. |

## v0.1 - Awakening

Goal: prove the smallest honest version of Bunny.

Deliverables:

- Minimal working assistant on the reference hardware.
- Single-provider configuration with one API key.
- Seed personality loaded from structured state.
- Text interaction through a local interface powered by the Neon role as the primary conversational voice.
- Persistent local identity and conversation state.
- Basic memory retrieval with explicit consent.
- Natural, non-intrusive curiosity.
- Restart persistence.
- Visible usage and estimated cost with versioned pricing configuration.
- Minimal pre-dispatch reservation check against configurable spending limits (D-013 minimal enforcement).
- Offline demo or check mode that makes no paid request.

Exit criteria:

- A fresh install can be configured with one key.
- Conversation runs reliably on the Neon role.
- The first awakening is natural and can be skipped.
- Restarting does not repeat the awakening or lose identity.
- The user can inspect and delete what was remembered.
- A pre-dispatch budget reservation check prevents billable dispatch if spending limit is exceeded or pricing is missing.
- No dependency on a GPU, Docker, Redis or a vector database.
- Tests cover restart, memory consent, budget refusal and provider failure.

## v0.2 - Growing personality

Goal: make growth safe and inspectable.

Deliverables:

- Structured personality evolution for Layer 2.
- Clear separation from the immutable core in Layer 1.
- User controls to inspect, edit, reset and disable evolution.
- Memory management: browse, correct, delete, export.
- Background reflection in batches with a manual-only mode.
- Daily and monthly spending controls with visible records, soft warning thresholds and hard caps.
- User-configurable curiosity and quiet hours.
- Deletion propagation to summaries and retrieval indexes where implemented.

Exit criteria:

- Personality changes are visible, reversible and off by default where uncertain.
- Reflection cannot run without a budget and cannot grant permissions.
- Deletion behaves as documented, including its limits.
- The user can operate with curiosity and reflection disabled.

## v0.3 - Voice and delegation

Goal: add interaction modes and coordinated agents without weakening boundaries.

Deliverables:

- Local wake-word detection and voice activity detection.
- Optional local or hosted speech-to-text and text-to-speech.
- Microphone controls, listening indication and configurable retention.
- Neon, Otto and Scout as logical agents with a shared provider option.
- Explicit delegation envelopes, deadlines and cancellation.
- Tool permissions with user approval for consequential actions.
- Threat-model and security review gate prior to enabling device control or consequential tool actuation.
- Budget reservations that cover the final conversational response.

Exit criteria:

- Voice remains optional and text works without it.
- No ambient audio is uploaded continuously.
- Workers cannot exceed the permissions or budget of the parent request.
- A model cannot approve its own tool call.
- A delegated answer is not presented as Bunny unless the configuration says so.
- Tool and agent delegations satisfy the v0.3 security review gate.

## v0.4 - Personal assistant

Goal: make Bunny useful in daily routines.

Deliverables:

- MCP integration with explicit tool allowlists.
- Skills as installable, reviewable extensions.
- Scheduled tasks and reminders.
- Notifications through configurable channels.
- Optional local dashboard.
- Optional device and smart-home integrations through a permission layer.
- Security review gate for MCP sandboxing, external skills, and network egress controls.

Exit criteria:

- Scheduled work is bounded, cancellable and budgeted.
- External content cannot create schedules or grant permissions.
- Outbound requests from tools, MCP servers, and Scout research satisfy network egress validation and SSRF controls.
- Integrations pass the v0.4 security review gate.
- Every integration can be disabled independently.
- The core assistant still works when optional integrations fail.

## v1.0 - Community-ready

Goal: a project that others can install, extend and trust.

Deliverables:

- Documented installation for supported platforms.
- Cross-platform testing on Linux, Windows and macOS.
- Tested Raspberry Pi 4 4 GB behaviour and resource measurements.
- Stable extension interfaces for providers, tools and skills.
- A completed security review and threat-model update.
- Clear contribution, release and support processes.
- Migration and backup documentation.

Exit criteria:

- The documented installation works from a clean environment.
- The reference hardware meets agreed responsiveness and memory targets.
- Known security limitations are documented and accepted.
- Upgrade and rollback paths preserve identity and memory.
- The community can add a provider or tool without editing core code.

## First working prototype acceptance criteria

The initial prototype should eventually demonstrate all of the following. These are
acceptance criteria, not completed tests.

1. Installation on suitable reference hardware.
2. Configuration using one supported API key.
3. A successful first awakening.
4. Normal text conversation.
5. Natural, non-intrusive curiosity.
6. Local persistent memory.
7. Restart without losing identity or memory.
8. User-controlled memory deletion.
9. Provider switching without losing persistent identity, where supported.
10. API usage measurement.
11. Operation without unnecessary background requests.

## Sequencing notes

- Text before voice; identity and memory before autonomy.
- One provider before many; one agent boundary before delegation.
- Local storage before optional semantic retrieval.
- Budget enforcement before any hosted background job.
- Security and threat-model reviews precede each expansion of agent capability (prior to v0.3 device actions and v0.4 MCP/skills), not deferred until v1.0.
- Security boundaries before tools that can change the world.
- Measurements before optimisation claims.

## Non-promises

This roadmap does not promise:

- Release dates or a fixed number of releases.
- That every feature will be implemented.
- That all providers will behave identically.
- That the Raspberry Pi 4 will meet a specific latency target before testing.
- That voice will work without a network or a second API key in every configuration.
- That multi-agent delegation will be available in the first release.

Priorities may change as evidence arrives. Significant changes should be recorded in
[DECISIONS.md](DECISIONS.md).
