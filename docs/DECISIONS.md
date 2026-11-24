# Decision log

This file records architectural decisions and the questions that remain open. It is
intended to explain *why* the project is shaped this way, not to claim that anything has
been implemented.

## Status legend

| Status | Meaning |
|---|---|
| **Accepted** | A current project decision. |
| **Provisional** | A working preference that may change with evidence. |
| **Open** | Not decided; requires research, measurement or owner input. |
| **Superseded** | Replaced by a later decision, retained for traceability. |

## Decisions

### D-001 - Public licence

**Status:** Accepted

Bunny.ai is licensed under the Apache License 2.0. It is permissive, includes an explicit
patent grant, is familiar to contributors and is compatible with a wide range of
downstream use. The full text is in [LICENSE](../LICENSE).

### D-002 - Documentation-first public foundation

**Status:** Accepted

The public repository starts with design documentation rather than imported code. A
credible foundation is better than inventing placeholder implementation or publishing
unsanitised prototype code. Code will be added when a milestone is ready, tested and
licensed.

### D-003 - No import of the private prototype

**Status:** Accepted

An earlier private prototype informed the design but is not part of this repository.
It is excluded because it is coupled to personal hardware and infrastructure, contains
personal context, has no selected public licence, and has not completed a public security
and sanitisation review. No source code, history or personal configuration was copied.

### D-004 - Provider-agnostic model gateway

**Status:** Accepted

All model access goes through a provider-neutral gateway. Providers are interchangeable
engines; Bunny's identity must not depend on any one vendor. Candidate integrations are
not claims of support.

### D-005 - One provider is enough to start

**Status:** Accepted

The core experience must work with a single supported API key. Multiple providers,
per-agent models and local inference are optional enhancements.

### D-006 - Local-first memory and identity

**Status:** Accepted

Durable memory, identity, configuration and usage records live on the user's hardware.
Selected context may be sent to a hosted model, and this must be disclosed honestly.

### D-007 - SQLite as the initial storage candidate

**Status:** Provisional

SQLite is the preferred starting point because it is embedded, transactional,
cross-platform and appropriate for the reference hardware. It is not a permanent mandate.
Full-text search should be tried before embeddings, and a dedicated vector database is not
introduced without measured need.

### D-008 - No mandatory infrastructure

**Status:** Accepted

Bunny does not require Docker, Redis, Kubernetes or a dedicated vector database. These may
be supported as optional deployment choices later if evidence justifies their operational
cost.

### D-009 - Three-layer personality

**Status:** Accepted

Core identity, evolving personality and user understanding are separate layers. Only the
evolving layer may change through normal use. The core is not autonomously rewritten, and
user understanding distinguishes explicit from inferred information.

### D-010 - No fine-tuning for identity

**Status:** Accepted

Bunny's continuity comes from structured personality state, persistent memory and context
retrieval. Fine-tuning is not required and would harm portability across providers.

### D-011 - Logical agents, not mandatory processes

**Status:** Accepted

Neon (conversation), Otto (routine operations) and Scout (research) are logical roles. They
may share a model, provider and process. Separate models or processes are advanced
options, not prerequisites. The memory service is infrastructure, not an agent.

### D-012 - Deterministic security boundaries

**Status:** Accepted

Permissions, spending limits and approval checks are enforced in code, not by prompt
wording. Model output cannot grant permissions, widen its own scope or approve a
consequential action.

### D-013 - Budget before dispatch

**Status:** Accepted

Every billable request, including background work, must be reserved against a budget before
it is sent. Uncertain charges are recorded rather than silently released. A provider
fallback must never hide a spending or configuration change.

Enforcement schedule and operational rules:
- **Sequencing:** D-013 takes effect in minimal form in v0.1 (pre-dispatch check against
  configured limits and versioned pricing), expanded in v0.2 (daily/monthly limits, soft
  alerts, and reflection budgets), and v0.3 (multi-agent delegation reservations).
- **Pricing:** Pricing is sourced from versioned tables. Missing or expired pricing fails
  closed or uses an explicit user-approved upper ceiling; requests never proceed under a
  silent zero-cost assumption.
- **Resilience and uncertain charges:** Bounded retries use exponential backoff, jitter,
  and honour `Retry-After`. If a request times out after dispatch, the reservation remains
  held as an uncertain charge until reconciled, avoiding double-spending on retries.
- **Cache-awareness:** Prompt cache read rates are distinguished from uncached tokens in
  usage metering.

### D-014 - Curiosity is a subsystem

**Status:** Accepted

Curiosity is a dedicated subsystem with its own policy, not merely a personality prompt. It
must be configurable, respect quiet hours, avoid repetition, never block a task and never
manufacture engagement or unnecessary API calls.

### D-015 - Hardware reference target

**Status:** Provisional

The Raspberry Pi 4 with 4 GB RAM is the initial reference target, subject to real testing.
Repurposed laptops, desktops and servers are equally valid. No GPU is required. The target
is a design constraint, not a claim of validated performance.

### D-016 - Text before voice

**Status:** Accepted

Text interaction is the first milestone. Voice is optional and layered on later, with local
wake-word processing preferred and no continuous ambient upload.

### D-017 - Documentation-only launch

**Status:** Accepted

The initial public state is a documentation-only foundation. It may be launched publicly as
such, provided the status is clear and no planned feature is presented as available.

### D-018 - Technology stack

**Status:** Open (with a provisional Python baseline)

No final implementation language has been selected. Python is the provisional baseline
because the private prototype demonstrated a text-only path in Python, it is accessible to
contributors, and it runs on the reference hardware. This is not a commitment.

Trade-offs considered:

| Option | Strengths | Weaknesses |
|---|---|---|
| Python | Strong AI SDK ecosystem, fast iteration, readable, good Pi support. | Higher memory use, packaging and distribution friction, weaker static guarantees. |
| TypeScript | Strong tooling, good UI and web integration, large contributor pool. | Runtime and packaging overhead on small devices; AI SDK parity varies. |
| Rust | Excellent performance, memory safety and single-binary deployment. | Steep learning curve, slower iteration, smaller AI SDK ecosystem. |
| Go | Simple deployment, good concurrency and cross-compilation. | Less mature AI tooling; more verbose for rapid prototyping. |

The decision should be revisited before v0.1 implementation, using measured evidence for
memory, startup, packaging and local voice support.

### D-019 - Voice stack

**Status:** Open

Local wake-word detection is preferred. Speech-to-text and text-to-speech may be local or
hosted, but voice must remain optional and should not require a second cloud key by
default. Candidate technologies require evaluation on the reference hardware.

### D-020 - Packaging and installation

**Status:** Open

The installation experience should be reproducible on a clean machine and should not
require containers. Whether that means a Python package, a virtual environment, a native
binary, a service unit or a combination is undecided.

### D-021 - Memory encryption and export format

**Status:** Open

Encrypted identity and memory export/import are a longer-term goal. The format, key
storage, recovery model and migration semantics require a dedicated design and threat
model. Until implemented, documentation must not describe storage as encrypted.

### D-022 - Proactive interaction

**Status:** Accepted

Proactive questions and unsolicited conversation are disabled or conservative by default
and must be configurable. The project does not optimise for engagement.

## Open questions

- Final implementation language and repository layout.
- Gateway interface shape and capability negotiation model.
- Personality schema and whether numeric traits are useful.
- Memory schema, retention defaults and deletion propagation tests.
- Voice stack, microphone hardware assumptions and privacy indicators.
- Extension format for tools, skills and MCP servers.
- Cross-platform packaging and service supervision.
- Security review timing: Settled — staged review gates precede v0.3 (tools/delegation),
  v0.4 (MCP/skills/egress), and v1.0 (final comprehensive audit).
- Governance and code of conduct: Settled — community Code of Conduct adopted in
  [CODE_OF_CONDUCT.md](../CODE_OF_CONDUCT.md). Maintainership model remains open.
- Context engineering fine-tuning: token budget allocations per model class (see
  [ARCHITECTURE.md](ARCHITECTURE.md)).
- Whether any optional hosted service is ever appropriate.

## Changing a decision

Propose the change with the problem, alternatives, evidence and impact on security,
privacy, cost and portability. Update this file when a decision changes, and mark the old
entry as superseded rather than deleting it. Do not present a provisional or open item as
settled in other documentation.
