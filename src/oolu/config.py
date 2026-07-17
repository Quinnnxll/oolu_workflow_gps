"""Settings — externalized configuration and the engine factory.

Everything the engine has been defaulting in code (tier models + endpoints, resource
limits, escalation/loop thresholds, backend choice) is gathered here into one
``Settings`` object that:

  * validates against the same frozen Pydantic models the components already use
    (``RoutingConfig``, ``ResourceLimits``, ``EdgePolicy``) — so a YAML file simply
    deserializes; there is no second schema to keep in sync;
  * loads from ``config/models.yaml`` (or any path);
  * accepts a small set of environment overrides for the things that genuinely vary
    per deployment — the vLLM endpoint, the model names, the backend, the package
    index — without dragging in a settings framework.

The settings LOGIC lives in the package (clean imports); the YAML template lives at
the repo root in ``config/models.yaml`` (operator-editable data).

``build_oolu(settings)`` is the one-call factory: it selects and constructs
the backend + gateway from config and returns a ready ``OoLu``. Tests inject
their own gateway/backend to bypass the real ones.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field

from .graph.edges import EdgePolicy
from .routing.matrix import RoutingConfig, RoutingMatrix, default_routing_config
from .runtime.backend import ResourceLimits

if TYPE_CHECKING:
    from .graph.builder import OoLu

# Environment overrides — the deployment-time knobs, applied after YAML.
ENV_CONFIG_PATH = "OOLU_CONFIG"
ENV_API_BASE = "OOLU_API_BASE"
ENV_FAST_MODEL = "OOLU_FAST_MODEL"
ENV_REASONING_MODEL = "OOLU_REASONING_MODEL"
ENV_BACKEND = "OOLU_BACKEND"
ENV_PINNED_INDEX_URL = "OOLU_PINNED_INDEX_URL"
ENV_UV_CACHE_DIR = "OOLU_UV_CACHE_DIR"


class BackendSettings(BaseModel):
    """Which isolation backend to run, and its container-level knobs."""

    model_config = ConfigDict(frozen=True)

    kind: Literal["subprocess", "docker"] = Field(
        default="subprocess",
        description="'docker' = hostile isolation; 'subprocess' = dev/fallback, NO isolation.",
    )
    image: str = "oolu-sandbox:latest"
    network_name: str = "bridge"
    uv_cache_dir: str | None = None
    pinned_index_url: str | None = None
    run_as_user: str | None = None


class GraphSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    recursion_limit: int | None = Field(
        default=None, description="None => derived from the edge policy's max_recalcs."
    )


class SkillSettings(BaseModel):
    model_config = ConfigDict(frozen=True)

    registry_path: str = "~/.oolu/skills-registry.db"
    max_context_tools: int = Field(default=8, ge=1)


class Settings(BaseModel):
    """The whole engine configuration, composed from the components' own models."""

    model_config = ConfigDict(frozen=True)

    routing: RoutingConfig = Field(default_factory=default_routing_config)
    limits: ResourceLimits = Field(default_factory=ResourceLimits)
    edges: EdgePolicy = Field(default_factory=EdgePolicy)
    backend: BackendSettings = Field(default_factory=BackendSettings)
    graph: GraphSettings = Field(default_factory=GraphSettings)
    skills: SkillSettings = Field(default_factory=SkillSettings)
    request_timeout_s: float = Field(
        default=120.0, description="Gateway completion timeout."
    )

    # --- loading ------------------------------------------------------ #
    @classmethod
    def load(
        cls, path: str | Path | None = None, *, apply_env: bool = True
    ) -> "Settings":
        """Load from YAML (explicit path, then $OOLU_CONFIG, else defaults), then
        layer environment overrides on top."""
        resolved = path or os.environ.get(ENV_CONFIG_PATH)
        if resolved:
            data = yaml.safe_load(Path(resolved).read_text()) or {}
            settings = cls.model_validate(data)
        else:
            settings = cls()
        return settings.with_env_overrides() if apply_env else settings

    def with_env_overrides(self) -> "Settings":
        """Return a copy with deployment env vars applied. No-op if none are set."""
        env = os.environ
        updates: dict = {}

        api_base = env.get(ENV_API_BASE)
        fast_model = env.get(ENV_FAST_MODEL)
        reasoning_model = env.get(ENV_REASONING_MODEL)
        if api_base or fast_model or reasoning_model:
            fast = self.routing.fast.model_copy(
                update=_tier_overrides(api_base, fast_model)
            )
            reasoning = self.routing.reasoning.model_copy(
                update=_tier_overrides(api_base, reasoning_model)
            )
            updates["routing"] = self.routing.model_copy(
                update={"fast": fast, "reasoning": reasoning}
            )

        backend_changes: dict = {}
        if env.get(ENV_BACKEND):
            backend_changes["kind"] = env[ENV_BACKEND]
        if env.get(ENV_PINNED_INDEX_URL):
            backend_changes["pinned_index_url"] = env[ENV_PINNED_INDEX_URL]
        if env.get(ENV_UV_CACHE_DIR):
            backend_changes["uv_cache_dir"] = env[ENV_UV_CACHE_DIR]
        if backend_changes:
            updates["backend"] = self.backend.model_copy(update=backend_changes)

        return self.model_copy(update=updates) if updates else self


def _tier_overrides(api_base: str | None, model: str | None) -> dict:
    out: dict = {}
    if api_base:
        out["api_base"] = api_base
    if model:
        out["model"] = model
    return out


# --------------------------------------------------------------------------- #
# Engine factory.                                                              #
# --------------------------------------------------------------------------- #
def build_oolu(
    settings: Settings | None = None,
    *,
    gateway=None,
    backend=None,
    knowledge=None,
    hint_provider=None,
    script_cache=None,
) -> "OoLu":
    """Construct a ready engine from settings. Inject ``gateway``/``backend`` to
    bypass the real LiteLLM/Docker components (tests, dev, custom wiring); pass
    ``knowledge`` to wire in the crowd-intelligence layer (local/remote cache)."""
    settings = settings or Settings()

    from .graph.builder import OoLu

    if backend is None:
        backend = _build_backend(settings.backend)
    if gateway is None:
        from .routing.gateway import LiteLLMGateway  # lazy: litellm optional at import

        gateway = LiteLLMGateway(request_timeout=settings.request_timeout_s)

    return OoLu(
        gateway=gateway,
        backend=backend,
        matrix=RoutingMatrix(settings.routing),
        edge_policy=settings.edges,
        limits=settings.limits,
        pinned_index_url=settings.backend.pinned_index_url,
        knowledge=knowledge,
        hint_provider=hint_provider,
        recursion_limit=settings.graph.recursion_limit,
        script_cache=script_cache,
        backend_kind=settings.backend.kind,
        backend_image=settings.backend.image
        if settings.backend.kind == "docker"
        else None,
    )


def _build_backend(bs: BackendSettings, *, web_fetch=None, materialized_dir=None):
    # ``web_fetch`` is the host-side guarded HTTP hand the web broker
    # answers a granted sandbox through (runtime.webhand). None = no web
    # hand: granted runs still get the honest refusal from the shim.
    # ``materialized_dir`` is the mounted bundle tier (runtime.bundle): when
    # set, a bundle is materialized once and staged by symlink / read-only
    # bind-mount instead of extracted per run.
    if bs.kind == "docker":
        from .runtime.isolation import LocalDockerBackend  # lazy: docker optional

        return LocalDockerBackend(
            image=bs.image,
            network_name=bs.network_name,
            uv_cache_dir=bs.uv_cache_dir,
            default_index_url=bs.pinned_index_url,
            run_as_user=bs.run_as_user,
            web_fetch=web_fetch,
            materialized_dir=materialized_dir,
        )
    from .runtime.isolation import SubprocessBackend

    return SubprocessBackend(
        default_index_url=bs.pinned_index_url,
        web_fetch=web_fetch,
        materialized_dir=materialized_dir,
    )
