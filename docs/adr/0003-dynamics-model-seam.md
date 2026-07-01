# ADR-0003: A dynamics-model seam for latent workflow state (linear SSM / Kalman later)

- Status: Proposed
- Date: 2026-07-01

## Context

We want to know whether a **linear State Space Model** (either a deep S4/S5/Mamba
backbone, or a classical linear-Gaussian / Kalman model) will help OoLu with
multi-agent, multi-dimensional problem solving — and to leave an integration point
so a later decision does not require reworking the engine.

Two findings shape this ADR:

1. **Multi-dimensionality is not the hard part.** Linear SSMs are naturally
   high-dimensional (MIMO). The hard part of multi-agent solving is that it is
   *nonlinear, discrete, and non-stationary*: branching/conditional control
   (approvals, reserved actions), multiplicative agent coupling, and partial
   observability. A strictly linear operator cannot represent those decisions —
   which is exactly why Mamba adds input-dependent *selectivity* and breaks strict
   linearity.
2. **A linear SSM is strong as a component, weak as the whole brain.** It is a good
   long-horizon sequence encoder, a good belief/state *estimator* (Kalman/EKF) over
   a defined latent, and an exact solver for linear-quadratic / linear-Gaussian
   *sub-problems* — as long as the discrete decisions stay in a nonlinear policy
   layer.

There is one property specific to OoLu worth exploiting: an SSM's recurrent state
is **fixed-size and serializable**, unlike a Transformer KV cache. That maps
cleanly onto our versioned, serializable `RunState` and durable checkpoint/resume
(ADR-0002). So the natural role is to *carry and estimate latent workflow state*,
not to make the discrete decisions.

## Decision

Do **not** adopt a linear SSM as a reasoning core now. Instead, reserve a seam,
following the established "port + deterministic default + optional adapter"
pattern, so a linear-SSM/Kalman estimator can drop in later behind a stable
interface once verification justifies it.

- Introduce a `DynamicsModel` port (orchestrator stage adapter) with roughly:
  `init_state(brief) -> LatentState`, `step(state, observation) -> LatentState`
  (belief update), `predict(state, action) -> Prediction` (forward model the route
  optimizer / outcome monitor may optionally consult).
- Add a nullable, serializable `latent: LatentState | None` to `RunState`
  (fixed-size vector; round-trips through `model_dump_json()`), preserving
  ADR-0002's single-source-of-truth and resume guarantees.
- Ship a **no-op / identity default** so behaviour is unchanged and the engine
  stays deterministic and serializable. Discrete decisions remain in the existing
  (nonlinear) policy adapters — `LeastCostRouteOptimizer`,
  `RiskBasedHumanControl`, `StatusOutcomeMonitor`, `BoundedRetryRecovery`.
- Model-backbone experiments (e.g. a Mamba-based LLM) need **no** seam: they are
  just another endpoint behind `routing/gateway.py` and can be A/B'd directly.

### Verification before adoption (falsifiable)

Fit a one-step linear predictor `x_{t+1} ≈ A x_t + B u_t` on real trajectories
exported from the audit log + `ExecutionRecord`s, then:

- **Linearity residual test:** measure how much a small nonlinear head reduces the
  linear model's residual. Small reduction → linear suffices; large → it does not.
- **Observability/controllability:** rank/condition of the fitted Gramians (is the
  latent even estimable from what OoLu observes?).
- **Horizon-degradation curve:** success/cost vs. trajectory length.
- **Multi-agent:** per-agent linear filter + explicit coupling vs. a joint
  nonlinear model.
- **Calibration & throughput:** filter consistency (NIS/NEES) and the latency /
  constant-memory payoff that is the SSM's real advantage.

Baselines: current deterministic adapters + a Transformer-backed model via the
gateway.

## Consequences

- The integration point exists and is contract-tested with a no-op default, at
  **zero behavioural risk** today; the SSM/Kalman adapter lands only if the
  verification above justifies it.
- The linear model is confined to a *component* role (encoder / estimator / LQ
  sub-solver), matching where the theory says it is strong.
- If verification fails, the seam is a cheap, inert interface — not a rewrite.
