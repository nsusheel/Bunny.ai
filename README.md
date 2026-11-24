# Bunny.ai

**An open-source personal AI that grows with you.**

> **Pre-release · Documentation only.** There is no runnable Bunny to install today. The diagrams and capabilities on this page describe the proposed design.

Bunny is a personal assistant designed to run on hardware you control. Its identity, memories and settings live on your device. You can change the AI model or move to another machine and carry that state with you.

**Your hardware, your choice of intelligence, your memories, your Bunny.**

[Architecture](docs/ARCHITECTURE.md) · [Roadmap](docs/ROADMAP.md) · [Contributing](CONTRIBUTING.md) · [Apache 2.0 licence](LICENSE)

## How it fits together

```mermaid
flowchart TB
    accTitle: Bunny on hardware you control
    accDescr: You interact with one assistant. Bunny keeps identity and memory on your device, uses your chosen local or hosted model, and accesses optional tools or research through permissions.
    You([You])
    Bunny["Bunny · one assistant<br/>On hardware you control"]
    State[("Identity and<br/>local memory")]
    Model["Model choice<br/>Local or hosted"]
    Cap["Optional tools<br/>and research"]
    You <--> Bunny
    Bunny <--> State
    Bunny <--> Model
    Bunny -.->|"Permission"| Cap

    classDef core fill:#ede9fe,stroke:#7554ba,color:#241341
    classDef state fill:#d9f5ef,stroke:#268171,color:#103a34
    classDef model fill:#e0efff,stroke:#3975b6,color:#142e52
    classDef neutral fill:#eef1f5,stroke:#67768a,color:#222d3d
    class Bunny core
    class State state
    class Model model
    class You,Cap neutral
```

- **A familiar assistant across changes.** A stable identity and adaptable conversational style are kept separately from what Bunny learns about you.
- **Curiosity that respects your attention.** Bunny is designed to ask occasional, relevant questions and let you choose what it remembers.
- **Useful with one model.** One supported API key is enough for the planned hosted setup. Local inference is optional, and features depend on the model you choose.

## Privacy, hardware and cost

- **Privacy.** Durable memory and identity stay on your device. Hosted models receive selected request context, which may include relevant memories, under the provider’s policies. You’ll be able to inspect, correct, export or delete local memories. Local deletion cannot retract data already sent.
- **Hardware.** A Raspberry Pi 4 with 4 GB RAM is the initial, unvalidated target. The planned hosted setup requires no dedicated GPU. Local inference needs suitable hardware and memory.
- **Cost and permissions.** Bunny is Apache-2.0 licensed with no required Bunny subscription. Hosted APIs can charge for usage. Bunny’s design reserves budget before billable dispatch and checks permissions before tool actions. Provider pricing changes, uncertain charges and usage outside Bunny can affect your bill.

## Where development stands

The documentation foundation is complete. **Next is v0.1 Awakening**, a minimal text assistant with one provider, a seed personality, restart-persistent local memory and a pre-dispatch budget check.

```mermaid
flowchart TB
    accTitle: Bunny development roadmap
    accDescr: The documentation foundation is complete. Version 0.1 Awakening is next. Growing Personality, Voice and Delegation, Personal Assistant and Community-Ready are future milestones.
    Done["COMPLETE · Documentation foundation"]
    Next["NEXT · v0.1 Awakening"]
    Future["PLANNED<br/>v0.2 Growing Personality<br/>v0.3 Voice and Delegation<br/>v0.4 Personal Assistant<br/>v1.0 Community-Ready"]
    Done --> Next --> Future
    classDef state fill:#d9f5ef,stroke:#268171,color:#103a34
    classDef gate fill:#fff0cf,stroke:#9d701d,color:#462d0a
    classDef neutral fill:#eef1f5,stroke:#67768a,color:#222d3d
    class Done state
    class Next gate
    class Future neutral
```

The [roadmap](docs/ROADMAP.md) defines each milestone’s scope and exit criteria. Milestones have no fixed delivery dates.

<details>
<summary>What later milestones include</summary>

- **v0.2 Growing Personality** adds inspectable and reversible personality evolution, memory management, bounded reflection and spending caps.
- **v0.3 Voice and Delegation** adds the optional voice pipeline, scoped delegation and tool permissions, subject to a security review.
- **v0.4 Personal Assistant** adds MCP integrations, skills, scheduled tasks, reminders and an optional dashboard, subject to network security gates.
- **v1.0 Community-Ready** targets tested packaging, installation documentation, verified extension interfaces and a security audit.

</details>

## Explore Bunny

<details>
<summary>Two examples of how Bunny could help</summary>

> *The following scenarios are illustrative hypothetical interactions demonstrating
> future intended behaviour, not currently implemented software. Bunny is not conscious,
> sentient or emotionally dependent on its user; it is an attentive software tool designed
> to be helpful, transparent and respectful of your attention. All named locations, travel
> estimates, trail conditions and tide predictions in these examples are fictional illustrative
> data, not verified real-world information.*

**Learning your world through natural conversation**

**First week:**
> **User** · I've been thinking about getting into landscape photography on weekends.
>
> **Bunny** · That sounds rewarding! The morning light can completely change a familiar place. Are you looking at dedicated cameras, or starting with your phone to see how you like it?
>
> **User** · Just my phone and a small tripod for now.
>
> **Bunny** · That's plenty to learn composition and framing. Let me know if you want some simple guidance on golden-hour planning or rule-of-thirds techniques whenever you head out.

**Two weeks later:**
> **User** · I have a free Saturday morning coming up. Any ideas for a quiet hike within an hour's drive?
>
> **Bunny** · Blackwood Ridge is about 45 minutes west. It has an open eastern overlook that catches early morning light—and it's usually quiet before 9 AM, which might suit your landscape photos. Would you like the trail details and sunrise time for Saturday?
>
> **User** · That's spot on. Yes, please.

Bunny observed an interest, remembered it locally without intrusive questionnaires, and
surfaced a helpful suggestion when relevant—while leaving the decision entirely to the user.

**Proactive assistance with clear consent**

> **User** · Can you find out if high tide at Blackwood Creek coincides with sunrise this Saturday?
>
> **Bunny** · I checked the tide charts for Saturday: high tide is at 6:18 AM, and sunrise is at 6:05 AM. The water levels will be high right at dawn. Would you like me to prepare a brief packing checklist for wet conditions, or set a reminder for Friday evening?
>
> **User** · Just the reminder for 8 PM Friday, thanks.
>
> **Bunny** · Reminder set for 8:00 PM Friday: "Check gear and weather for Blackwood Creek sunrise."

Bunny answered the research question, suggested logical next steps, and acted only on what the
user explicitly approved.

</details>

<details>
<summary>Founder’s note and project origin</summary>

> Bunny is named after a childhood stuffed rabbit associated with comfort, familiarity
> and continuity.
>
> In computing, hardware is routinely replaced and operating platforms evolve. With AI,
> however, user memories and assistant relationships are frequently anchored inside proprietary
> cloud accounts where continuity can end whenever a vendor alters terms or discontinues a service.
>
> Bunny began from the desire to create an assistant with that same enduring continuity: a
> personal companion whose identity, memories, and configuration belong entirely to the user,
> persisting across changing machines and interchangeable intelligence models.

Bunny's goal is to bring that sense of reliable, permanent ownership to personal AI: an assistant
that is curious from day one, grows alongside you, and remains recognisably itself across models,
providers, and machines.

Read the complete [Vision statement](docs/VISION.md).

This repository is the public home of Bunny.ai. An earlier private prototype informed some of the
conceptual design, but no prototype source code has been imported. Aside from the high-level origin
shared in the founder's note, this repository contains no personal data: no private user memories,
personal configurations, credentials, or proprietary infrastructure details have been included.
All design documents are written from scratch for the open-source community.

</details>

<details>
<summary>Meet Neon, Otto and Scout</summary>

Bunny has one user-facing identity. The proposed architecture divides the work into three logical roles that can share one process and one configured model.

- **Neon** handles conversation, coordinates delegation and presents Bunny’s final reply.
- **Otto** organises memories and coordinates bounded background work and reflection.
- **Scout** researches and synthesises information through approved, restricted sources.

The [logical role diagram](docs/ARCHITECTURE.md#logical-agents) shows shared services, the model gateway and permission boundaries.

</details>

<details>
<summary>Hardware, costs and privacy in more detail</summary>

The Raspberry Pi reference target is headless 64-bit Linux. Repurposed PCs, mini PCs and home servers across Linux, macOS and Windows are planned environments. Optional local inference trades API charges for hardware, RAM and electricity. For optional voice, the design favours local wake-word detection and rules out continuous ambient audio uploads.

Hosted usage is paid to the provider and depends on its availability, pricing and terms. Bunny’s accounting holds uncertain charges against the configured budget until reconciliation or review. Missing or stale pricing leads to a refusal or an explicit user-approved upper bound. See [gateway accounting](docs/ARCHITECTURE.md#pricing-configuration-and-spending-controls) and [privacy controls and deletion limits](docs/PRIVACY.md#user-controls).

</details>

## Documentation and contributing

- **Start with the design.** [Vision](docs/VISION.md), [Architecture](docs/ARCHITECTURE.md) and [Decisions](docs/DECISIONS.md).
- **Explore how Bunny learns.** [First run](docs/AWAKENING.md), [Personality](docs/PERSONALITY.md), [Memory](docs/MEMORY.md) and [Privacy](docs/PRIVACY.md).
- **Help shape the project.** Design feedback is welcome, including on accessibility, platform support, privacy and security. Read [Contributing](CONTRIBUTING.md) and the [Code of Conduct](CODE_OF_CONDUCT.md). Use the [security policy](SECURITY.md) for private vulnerability reporting.
