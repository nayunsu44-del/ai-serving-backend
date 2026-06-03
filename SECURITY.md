# Security Policy

## Supported versions

This project is developed on the `main` branch. Security fixes are applied to
`main` only; there are no separately maintained release branches.

## Reporting a vulnerability

Please do **not** open a public issue for security problems.

Report privately through GitHub's
[private vulnerability reporting](https://github.com/nayunsu44-del/ai-serving-backend/security/advisories/new)
(the **Security** tab → *Report a vulnerability*). Include:

- a description of the issue and its impact,
- steps to reproduce or a proof of concept,
- affected endpoints, configuration, or versions.

You can expect an initial acknowledgement within a few days. Please allow time
for a fix before any public disclosure.

## Scope and handling notes

This service is an authenticating API gateway, so the following are treated as
sensitive by design:

- **Service bearer tokens** (`API_KEYS`) are SHA-256 hashed at startup; only
  hashes are kept in memory.
- **Provider keys** (`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`) are used solely to
  call upstream SDKs and are never logged.
- **Audit logs** store only a hashed principal identifier, never raw JWT
  subjects, emails, or tokens.
- **PII masking** (`PII_MASKING_ENABLED`) redacts personal data before it
  reaches upstream providers; only redaction counts are logged.

When reporting, never include real API keys, tokens, or unmasked personal data
in your report — redact them first.
