# Contributing to Bunny.ai

Thank you for considering a contribution. Bunny is an ambitious project, and it is
currently at an early, documentation-led stage.

> **Status:** the experimental offline scaffold is available. Installation, packaging
> and runtime interfaces may change as Awakening develops.

## Development setup

Use [uv](https://docs.astral.sh/uv/getting-started/installation/) 0.13.8 and run these
commands from the checkout. Python 3.12 is the baseline. Use `--python 3.13` to check
compatibility in a separate environment or let uv recreate the project environment.

```sh
uv sync --locked --python 3.12
uv run --no-sync ruff format --check .
uv run --no-sync ruff check .
uv run --no-sync mypy
uv run --no-sync pytest
uv build --offline
uv run --no-sync python scripts/smoke_wheel.py
```

The build emits a wheel and source distribution in `dist/`. The smoke script expects
one Bunny.ai wheel there. It installs the wheel into a temporary environment without an
index and runs the installed entry point with Python networking blocked.

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

We are committed to providing a welcoming, inclusive and harassment-free environment. All participants are expected to follow our [Code of Conduct](CODE_OF_CONDUCT.md). Please report unacceptable behaviour privately to repository maintainers through the contact channel listed on the repository owner's profile.
