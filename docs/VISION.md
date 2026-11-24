# Vision

**An open-source personal AI that grows with you.**

## The problem

Personal AI is becoming powerful and convenient, but it is usually rented. Identity,
memory and behaviour live inside a vendor's account. A model is deprecated, a price
changes, a policy shifts, or a service is withdrawn, and the assistant the user has been
building a relationship with changes underneath them.

At the same time, running a genuinely personal assistant has been treated as requiring
either a large subscription or a large GPU. Many people already own adequate hardware:
a Raspberry Pi, an old laptop, a small home server.

Bunny exists to separate three things that are usually bundled together:

1. **The assistant** - its identity, values, personality and memory.
2. **The intelligence engine** - the language model used at any given moment.
3. **The hardware** - the machine that runs the assistant.

None of the three should permanently own the others.

## The idea

Bunny is a personal AI that starts with a small, curious seed personality and grows into
an individual through ordinary conversation. It learns what is useful to know about its
user, remembers it locally, and remains recognisably itself when the model, provider or
machine changes.

The ambition is a Jarvis-like assistant in the practical sense: always available, useful
without ceremony, aware of context, and able to coordinate research, routine tasks and
conversation. The ambition is not to imitate a fictional character, but to make a
personal assistant that is genuinely owned by the person it serves.

## Positioning

> **Your hardware. Your choice of intelligence. Your memories. Your Bunny.**

Bunny is not a thin chat window with a personality prompt. It is a framework for a
persistent personal assistant:

- The identity is structured state, not a single system prompt.
- Curiosity is a subsystem that decides when a question is worth asking.
- Memory is an inspectable local store with provenance and consent.
- Agents are logical roles with explicit permissions, not unbounded autonomous processes.
- Model access goes through one gateway that accounts for capability, usage and cost.
- Security boundaries are independent of personality and cannot be rewritten by the
  model.

## Founding principles

These are design requirements. They are not claims that every principle is already
implemented.

| Principle | Meaning |
|---|---|
| Open source | The core is community-readable, modifiable and extensible under a clear licence. |
| Hardware independent | No single device is required. The Raspberry Pi 4 with 4 GB RAM is the initial reference target, subject to testing. |
| No mandatory GPU | Hosted inference and small local models must both be viable. |
| Cross-platform | Linux, Windows and macOS are intended targets; headless operation must be possible. |
| Single-key start | One supported API key should be enough for the core experience. |
| Provider choice | Users choose the intelligence engine. Multiple providers are optional. |
| Optional local inference | Local models are supported where hardware permits, never required. |
| No mandatory subscription | There is no required Bunny cloud plan. |
| Local-first memory | Memory and identity are stored locally, with transparent disclosure of what leaves the device. |
| Spending transparency | API usage and estimated costs are visible, with configurable limits. |
| Modularity | Tools, agents, skills and integrations can be added or removed independently. |
| Immediate usefulness | Bunny should be a capable assistant before it has learned anything about the user. |

## Ownership model

The user owns:

- **The assistant's identity state**, including personality configuration and memory.
- **The data**, including conversations, memories, usage records and exports.
- **The configuration**, including providers, models, permissions and budgets.
- **The hardware**, and therefore where data is stored and when it is deleted.

Bunny owns nothing. There is no account requirement, no remote identity service and no
vendor-controlled memory. If the project later offers optional hosted services, they must
remain optional and replaceable.

## What success looks like

A successful Bunny:

- Is useful in the first ten minutes, before it knows the user.
- Asks a good follow-up question occasionally, not constantly.
- Remembers a preference once and does not need to be told again.
- Clearly distinguishes what it was told from what it inferred.
- Survives a restart, a software update, a machine migration, a model change and a
  provider change without losing who it is.
- Costs what the user expects, and stops rather than overspends.
- Can be inspected, corrected, exported and deleted.
- Can run headless on modest hardware without a mandatory cloud dependency beyond the
  chosen model provider.

## Non-goals

Bunny is not intended to be:

- A hosted consumer service with a mandatory subscription.
- A benchmark for a single model or vendor.
- An autonomous agent that expands its own permissions or acts without approval.
- A replacement for the user's judgement on consequential decisions.
- A reason to buy a GPU, a dedicated server or a proprietary hub.
- A surveillance device. Always-on voice, if enabled, should process wake words locally
  and must not upload ambient audio continuously.
- A finished product. The project values honest progress over impressive-sounding claims.

## Relationship to the model

The model is an interchangeable engine. Bunny should be able to use a hosted frontier
model for conversation, a cheaper model for routine work, a local model for privacy-
sensitive tasks, or a single model for everything.

Capabilities differ. Some models stream, some support structured output, some support
tool calling, some have small context windows. Bunny's architecture must negotiate and
degrade gracefully rather than promise identical behaviour everywhere.

Fine-tuning is not the mechanism for identity. Bunny's continuity comes from structured
personality state, local memory and context retrieval, which makes it portable across
providers.

## A community project

Bunny is intended to be extended by its community: new providers, new tools, new voices,
new hardware targets and new ideas about what a personal assistant should be. The
architecture should make safe extension possible without requiring every contributor to
understand the entire system.

See [ARCHITECTURE.md](ARCHITECTURE.md), [PERSONALITY.md](PERSONALITY.md),
[MEMORY.md](MEMORY.md) and [ROADMAP.md](ROADMAP.md) for the concrete design.
