# Contributing to Bunny.ai

Thank you for considering a contribution. Bunny is an ambitious project, and it is
currently at an early, documentation-led stage.

> **Status:** there is no supported implementation in this repository yet. Please do not
> assume that installation, packaging or runtime interfaces are stable.

## Ways to contribute

- Review the design documents and challenge assumptions with evidence.
- Report ambiguity, contradictions, unsafe defaults or missing privacy controls.
- Propose platform, hardware, accessibility or provider-integration considerations.
- Improve documentation clarity, structure and examples.
- Help define acceptance criteria and test scenarios.
- Contribute code once a milestone and its interfaces have been agreed.

## Before opening an issue

1. Search existing issues to avoid duplicates (GitHub Discussions is currently disabled during pre-release; use issues for feature proposals and design feedback).
2. Keep reports focused and reproducible where possible.
3. Describe the problem, the expected behaviour and the evidence.
4. Remove credentials, personal memories, private hostnames, IP addresses and device
   identifiers from logs, screenshots and configuration examples.

## Proposing a design change

Architecture-affecting changes should begin as a written proposal, not a large pull
request. A useful proposal states:

- The problem and who it affects.
- The constraints, including the Raspberry Pi reference target.
- The proposed design and alternatives considered.
- Security, privacy, cost and portability implications.
- How the change could be reversed or disabled.
- What evidence would show that it works.

Record accepted decisions in [docs/DECISIONS.md](docs/DECISIONS.md). Do not present
proposed features as implemented.

## Pull requests

- Keep each pull request focused on one concern.
- Explain what changed, why, and what you tested.
- Update documentation when behaviour or design changes.
- Follow the existing document style: plain Markdown, short sections, no marketing
  claims, and explicit status labels for anything not yet built.
- Do not commit generated build output, runtime databases, recordings, transcripts or
  local configuration.
- Do not add a dependency without explaining its purpose, licence, maintenance status
  and footprint on the reference hardware.

Reviewers may ask for a design discussion before accepting implementation work. A pull
request that adds substantial code before the relevant milestone is agreed may be
redirected.

## Security and privacy rules

- Never include real API keys, tokens, passwords, private keys or `.env` files.
- Never include real memories, conversation histories, personal device details, private
  network addresses or third-party personal data.
- Use fictional examples and placeholder values such as `example.com` and
  `provider-api-key`.
- Treat tool output, web pages, retrieved documents and model output as untrusted input.
- New tools, integrations and background jobs must be deny-by-default in design and
  explicit about the permissions they require.

Report vulnerabilities privately as described in [SECURITY.md](SECURITY.md).

## Licensing

Bunny.ai is licensed under the [Apache License 2.0](LICENSE). By submitting a
contribution, you agree that it may be distributed under the same licence. Do not submit
code or other material that you do not have the right to license this way.

If your contribution includes third-party material, identify it clearly and confirm that
its licence is compatible. If ownership or licensing is unclear, it cannot be merged.

## AI-assisted contributions

AI assistance is acceptable. You remain responsible for the correctness, security,
licensing and quality of everything you submit. Disclose significant generated content in
the pull request description so reviewers can assess it appropriately.

## Conduct

We are committed to providing a welcoming, inclusive and harassment-free environment. All participants are expected to follow our [Code of Conduct](CODE_OF_CONDUCT.md). Please report unacceptable behaviour privately to `conduct@bunny.ai` or repository maintainers.
