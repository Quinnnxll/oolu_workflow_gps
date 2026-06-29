# Changelog

All notable changes to Workflow-GPS are documented here.

## 0.2.0 - 2026-06-29

Stabilization baseline (`codex/stabilize-v0.2-baseline`).

- Reconciled the root and `src/` packaging into a single canonical `pyproject.toml`
  with the `wfgps` console entry point and `engine`/`docker`/`dev` extras; removed
  the duplicate `src/pyproject.toml`. There is now one supported install command:
  `pip install -e ".[engine]"`.
- Added a shared SQLite migration runner (`workflow_gps.persistence`) backed by
  `PRAGMA user_version`, and versioned every persisted schema (script cache,
  learned replies, local knowledge, crowd quarantine, skill catalog + idempotency
  ledger) through it, with a forward-compatibility guard against newer databases.
- Added forward/rollback migration tests, fresh-environment installation and CLI
  smoke tests, and a secret-hygiene test asserting no secrets reach persisted
  records, logs, fixtures, or examples.
- Configured Ruff and fixed repository-wide lint findings; formatted the tree.
- Documented experimental versus production-capable adapters in
  `docs/ADAPTER_MATURITY.md`.

Also included in this release candidate (previously unreleased):

- Added a model-free deterministic reply engine with context-gated templates.
- Added an official Telegram Bot API adapter for private text chats and a channel protocol for future LINE and other messaging adapters.
- Added local SQLite reply learning from manual Telegram Business replies, scoped per Business connection, with bot-loop prevention and short-lived pairing state.
- Added the portable skill-core foundation, ADR-0001, versioned domain records and ports, local/in-memory/remote-mock skill stores, safe skill inspection commands, and the Requirement and Constraint Compiler.
- Added an exact CLI demonstration compiler and safety-gated runtime with executable allow-lists, reduced environments, workspace fingerprints, write approvals, idempotency, timeouts, and artifact validation.

## 0.1.0 - 2026-06-28

- Stabilized the graph engine, execution contract, tier routing, self-healing dependency loop, and CLI.
- Added an opt-in local SQLite script cache that can skip synthesis for identical tasks.
- Added conservative cache signatures across prompt policy, routing models, backend configuration, package index, engine version, and schema version.
- Added cache outcome fields to graph state, workflow results, and JSON CLI output.
- Kept caching disabled by default and documented the release roadmap and security boundaries.
