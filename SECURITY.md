# Security policy

Bunny is intended to hold personal memory and to act on a user's behalf. Security and
privacy are therefore treated as product requirements, not as later hardening work.

> **Status:** no software has been released from this repository. There are no supported
> versions yet. This policy describes reporting and the intended security model.

## Reporting a vulnerability

Do **not** open a public issue for a suspected vulnerability.

Use GitHub's private vulnerability reporting for this repository:

1. Open the repository's **Security** tab.
2. Choose **Report a vulnerability**.
3. Provide a clear description, affected component or document, reproduction steps and
   potential impact.

If private reporting is unavailable, contact the maintainers through the repository's
listed security contact rather than disclosing details publicly. Please allow reasonable
time for triage and a coordinated fix before public disclosure.

Do not include real API keys, tokens, private keys, personal memories or private network
details in a report. Use redacted or synthetic examples.

## Scope

In scope includes, once code exists:

- Unexpected disclosure of credentials, memories, transcripts or personal context.
- Permission bypasses, prompt-injection-to-action paths, and unsafe tool execution.
- Budget or spending-control bypasses.
- Unsafe default network exposure or authentication weaknesses.
- Memory deletion or export behaviour that contradicts the documented model.
- Supply-chain or dependency issues in shipped components.

Out of scope for this repository while it is documentation-only:

- Vulnerabilities in third-party providers, SDKs or models themselves. Report those
  upstream; tell us if Bunny's design makes the impact worse.
- Social engineering of the project maintainers.
- Issues that require modifying a user's device or account outside Bunny.

## Intended security model

The full design is in [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) and
[docs/PRIVACY.md](docs/PRIVACY.md). The core principles are:

- **Deny by default.** Tools, integrations and file or device access are off unless
  explicitly enabled.
- **Least privilege.** Each agent and adapter receives only the permissions and secrets
  it needs.
- **Consequential actions need approval.** Sending, deleting, purchasing, changing
  settings or controlling devices requires explicit, scoped confirmation.
- **Model output is not authority.** A model cannot grant itself permissions, change its
  own security policy or approve a tool call.
- **Untrusted data stays untrusted.** Web content, documents, tool output and retrieved
  messages cannot become instructions or permissions.
- **Credential isolation.** API keys and tokens are never stored as ordinary memories,
  prompts, logs or documentation. Secrets should live in dedicated secret storage or
  environment variables with restricted file permissions.
- **No default public listeners.** Local services bind to loopback or an explicitly
  authenticated interface. The local network is not treated as a trusted caller.
- **Auditable actions.** Security-relevant events are logged with redacted metadata,
  bounded retention and user-visible controls.
- **Honest degradation.** When a provider, tool or permission is unavailable, Bunny
  reports that state rather than silently substituting a weaker or riskier path.

## Prompt injection and tool output

Bunny assumes that retrieved content can be hostile. The security boundary must be
deterministic code, not prompt wording. Tool results and external content should be
delimitated, length-bounded, provenance-tagged and never interpreted as policy.
High-impact tools should require out-of-band approval whose scope cannot be widened by a
model or a retrieved document.

## Secrets and personal data

- Never commit `.env` files, credentials, private keys or service-account files.
- Never put secrets in memory records, prompts, issue reports, screenshots or logs.
- Memory exports and backups are potentially sensitive; protect and encrypt them
  according to the privacy design.
- Deleting a memory is not secure erasure. Previous provider requests, backups and
  filesystem remnants may persist; this is documented honestly rather than overstated.

## Dependencies and supply chain

Every added dependency should be justified against the reference hardware, licence,
maintenance status and attack surface. Prefer the standard library and small, auditable
components where practical. A dependency is not trusted merely because it is popular.

## Disclosure

Once releases exist, this policy will list supported versions and expected response
targets. Until then, all reports are handled on a best-effort basis by the maintainers.
