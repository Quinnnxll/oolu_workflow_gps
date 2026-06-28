# ADR-0001: Portable skill core and composition roots

- Status: Accepted
- Date: 2026-06-28

## Context

Workflow-GPS must learn and replay operational skills through local desktop applications,
private manually configured APIs, cloud workers, and hybrid gateway/local-agent
deployments. Direct dependencies from domain logic to SQLite, HTTP, vendor SDKs, or UI
automation would make these deployment modes diverge and weaken testing and safety.

The system also needs a Requirement and Constraint Compiler. An underspecified request
such as "design a cup" must remain a parameter space until required values are supplied
or explicitly delegated. A demonstrated value must not silently become a universal
default.

## Decision

Create a portable `workflow_gps.skills` core containing:

- Versioned domain records for demonstrations, actions, state, constraints, skills,
  outcomes, approvals, requirements, parameter domains, and authorization.
- Protocol ports for observation, execution, probing, validation, storage, secrets,
  gateway coordination, events, and approval.
- A deterministic Requirement and Constraint Compiler that reports unresolved
  parameters, proposed questions, delegated choices, constraint blockers, and production
  readiness.
- Composition-specific adapters outside the domain model. Initial implementations are
  in-memory, local SQLite, and a serialization-boundary remote mock.

Skills store credential references only. Network records carry schema versions and
idempotency identifiers. Local execution remains under the local agent's authority.

## Alternatives considered

### Build around SQLite models first

Rejected because persistence details would leak into cloud and hybrid contracts.

### Store generated scripts as the complete skill representation

Rejected as the only representation because scripts do not make requirements,
constraints, capabilities, approvals, or recovery behavior independently inspectable.
Scripts may remain an action payload behind a typed adapter.

### Let the model fill every missing parameter

Rejected because it converts examples and statistical priors into silent engineering
requirements. Model suggestions may be presented as unbound options or selected only
under an explicit authorization grant.

## Consequences

- More interfaces and records exist before vendor integrations begin.
- Every adapter must satisfy shared contract tests.
- Schema migrations and compatibility are required from the first persisted skill.
- Local and cloud implementations can evolve independently without changing core logic.
- Production is blocked while required parameters, hard constraints, or approvals remain
  unresolved.

## Migration impact

The existing script cache and learned-reply stores remain separate. They may later expose
their behavior as skills, but this ADR does not migrate their current schemas.
