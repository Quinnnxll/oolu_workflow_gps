# Changelog

All notable changes to Workflow-GPS are documented here.

## Unreleased

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
