# Changelog

All notable changes to this project are documented here.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

## [0.1.0] - 2026-06-03

First tagged release. An authenticating, OpenAI-compatible chat gateway in
front of OpenAI and Anthropic.

### Added

- **OpenAI-compatible chat API** — `POST /v1/chat/completions` (streaming and
  non-streaming) and `GET /v1/models`, with strict request validation and
  OpenAI-style error bodies.
- **Multi-provider routing** — OpenAI and Anthropic backends selected by model
  ID; routable models configurable via `OPENAI_MODELS` / `ANTHROPIC_MODELS`.
- **Authentication** — service API keys (SHA-256 hashed, constant-time compare)
  and optional OIDC/JWT bearer auth, with scope-based authorization.
- **Rate limiting** — per-key token bucket (in-memory or Redis) plus a
  pre-auth per-IP throttle with spoof-resistant client-IP resolution.
- **Audit logging** — per-request audit rows with hashed principal, JSONL
  fallback on DB failure, and `super_admin` replay via `POST /admin/audit/replay`.
- **PII masking** — redacts Korean RRNs, Luhn-valid cards, phones, and emails
  before requests reach upstream providers; only redaction counts are logged.
- **Content policy** — forbidden-pattern scanning with `block` / `log_only` /
  `disabled` modes and policy-event persistence.
- **Streaming safeguards** — per-stream duration deadline, per-key concurrency
  limiting, and robust mid-stream error and audit handling.
- **Admin API** — `/admin/usage`, `/admin/audit`, `/admin/orgs`, `/admin/keys`
  with organization scoping and `super_admin` gating.
- **Request hardening** — body-size limit, message/length caps, ReDoS guards,
  and output-token limits.
- **Packaging** — Dockerfile and docker-compose (Redis + Postgres) on Python 3.13.
- **Project tooling** — GitHub Actions CI (pytest, docker build, pip-audit),
  Dependabot, MIT license, and `SECURITY.md` / `CONTRIBUTING.md`.

[Unreleased]: https://github.com/nayunsu44-del/ai-serving-backend/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/nayunsu44-del/ai-serving-backend/releases/tag/v0.1.0
