# Workflow-GPS roadmap

Development sequencing, architectural boundaries, adapter requirements, and safety gates
are coordinated in [DEVELOPMENT_COORDINATION.md](DEVELOPMENT_COORDINATION.md).

## V0.1.0: stable engine and optional script cache

V0.1.0 establishes the graph, execution contract, dependency-healing loop, routing, and offline-testable interfaces as the stable engine foundation. It adds an optional local SQLite cache for successful synthesized scripts. The cache is disabled by default and does not cache execution results.

## V0.2.0: stronger invalidation and result cache

V0.2.0 will extend invalidation inputs and policy controls, add cache inspection and pruning, and introduce a separately governed result cache for tasks whose inputs and outputs are safe and deterministic enough to reuse.

## V0.3.0: remote shared cache and knowledge server

V0.3.0 will support an authenticated remote service for sharing vetted scripts, dependency knowledge, and cache metadata across Workflow-GPS installations. Trust, provenance, tenancy, revocation, and auditability are prerequisites for enabling shared reuse.
