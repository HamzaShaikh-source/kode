"""Settings from .env (pydantic-settings). Secrets are SecretStr and never logged."""
from __future__ import annotations

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

ROLES = ("backend", "frontend", "qa")


class MissingConfigError(RuntimeError):
    """Raised (loudly) when a capability is used without the configuration it needs."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    github_token: Optional[SecretStr] = None
    github_repo: Optional[str] = None
    github_base_branch: str = "main"
    anthropic_api_key: Optional[SecretStr] = None
    openai_api_key: Optional[SecretStr] = None
    llm_model: str = "anthropic/claude-sonnet-5-5"
    author_backend: Optional[str] = None
    author_frontend: Optional[str] = None
    author_qa: Optional[str] = None
    kode_db_path: str = "./kode_state.db"
    kode_contracts_dir: str = "./contracts"
    kode_cors_origins: str = "http://localhost:5173"
    kode_frontend_dir: str = ".."                    # folder holding index.html/app.js/styles.css/fonts (served at /)
    simulated_github: bool = False
    openai_api_base: Optional[str] = None            # OpenAI-compatible gateway (e.g. https://tokenharbor.ai/v1)
    llm_max_retries: int = 4                         # total attempts for transient LLM failures
    llm_base_delay_s: float = 1.0                    # exponential backoff base (1s, 2s, 4s ...)
    llm_call_deadline_s: float = 300.0               # wall limit for ONE logical LLM call incl. retries
    agent_wall_budget_s: Optional[float] = None      # wall limit per agent task (set to 180 by --minimal)
    kode_enable_hub: bool = False                    # KODE_ENABLE_HUB=1 -> the Brain: pairing, sessions, console
    kode_hub_db: str = "./hub.db"
    kode_hub_key_file: str = "./.hub_key"
    kode_enable_bridge: bool = False                 # KODE_ENABLE_BRIDGE=1 -> terminal bridge (starts programs on this host!)
    kode_bridge_cwd: str = "./bridge_workdir"        # working directory for bridged sessions
    kode_minimal: bool = False                       # KODE_MINIMAL=1 -> one tiny file per agent (stage/free-model mode)
    simulation_mode: bool = False                    # SIMULATION_MODE=1 -> UI-only heartbeat, no engine calls

    @classmethod
    def settings_customise_sources(cls, settings_cls, init_settings, env_settings, dotenv_settings,
                                   file_secret_settings):
        # .env wins over inherited shell env, so a stale exported key can never shadow the project key.
        return (init_settings, dotenv_settings, env_settings, file_secret_settings)

    # ── derived ──
    @property
    def frontend_dir(self) -> Path:
        return Path(self.kode_frontend_dir).resolve()

    @property
    def contracts_dir(self) -> Path:
        return Path(self.kode_contracts_dir).resolve()

    @property
    def cors_origins(self) -> List[str]:
        return [o.strip() for o in self.kode_cors_origins.split(",") if o.strip()]

    def _llm_key_name(self) -> str:
        provider = self.llm_model.split("/", 1)[0].lower()
        return "OPENAI_API_KEY" if provider in ("openai", "azure") else "ANTHROPIC_API_KEY"

    def llm_key(self) -> Optional[str]:
        """The API key matching LLM_MODEL provider (None if unset)."""
        key = self.openai_api_key if self._llm_key_name() == "OPENAI_API_KEY" else self.anthropic_api_key
        return key.get_secret_value() if key and key.get_secret_value() else None

    def author_for(self, role: str) -> Tuple[str, str]:
        """Parse 'Name <email>' for a role; loud failure if absent or malformed."""
        raw = getattr(self, f"author_{role}")
        if not raw or "<" not in raw or not raw.rstrip().endswith(">"):
            raise MissingConfigError(
                f"AUTHOR_{role.upper()} is missing or malformed; expected 'Full Name <email@host>' "
                "(the human who owns this agent commits)."
            )
        name, email = raw.rsplit("<", 1)
        return name.strip(), email.rstrip().rstrip(">").strip()

    # ── capability gates (each raises MissingConfigError with an actionable message) ──
    def require_llm(self) -> str:
        key = self.llm_key()
        if not key:
            raise MissingConfigError(
                f"{self._llm_key_name()} is not set but LLM_MODEL={self.llm_model!r} needs it. "
                "K.O.D.E. never simulates agent output; set the key in .env."
            )
        return key

    def require_github(self) -> None:
        tok = self.github_token.get_secret_value() if self.github_token else ""
        missing = [n for n, v in (("GITHUB_TOKEN", tok), ("GITHUB_REPO", self.github_repo)) if not v]
        for role in ROLES:
            if not getattr(self, f"author_{role}"):
                missing.append(f"AUTHOR_{role.upper()}")
        if missing:
            raise MissingConfigError("GitHub pipeline requires: " + ", ".join(missing) + " (see .env.example).")

    def component_status(self) -> Dict[str, str]:
        """Truth table for the startup banner. Anything not REAL is labelled explicitly."""
        tok = self.github_token.get_secret_value() if self.github_token else ""
        gh_ok = bool(tok and self.github_repo)
        return {
            "LLM calls (LiteLLM)": f"REAL  model={self.llm_model}" if self.llm_key()
                                   else f"NOT CONFIGURED  -> {self._llm_key_name()} missing; /features will fail loudly",
            "GitHub branches/commits/PRs/merge": f"REAL  repo={self.github_repo}" if gh_ok
                                   else "NOT CONFIGURED  -> GITHUB_TOKEN/GITHUB_REPO missing; dispatch will fail loudly",
            "Contract freeze (sha256 lock)": "REAL",
            "Scope guard (ScopeViolation)": "REAL",
            "State store (SQLite)": "REAL",
            "SSE event stream": "REAL",
            "Approval gate (sole merge path)": "REAL",
            "Terminal bridge (PTY over WS)": "REAL  ON - loopback only, token+origin checked, allowlisted providers, scrubbed env"
                                             if self.kode_enable_bridge else "off (set KODE_ENABLE_BRIDGE=1)",
            "Brain hub (machines, consoles)": "REAL  ON - pairing codes, hashed node tokens, consent-gated control, audit log"
                                              if self.kode_enable_hub else "off (set KODE_ENABLE_HUB=1)",
            "SIMULATION_MODE (UI heartbeat)": "SIMULATED  ON - UI shows a SIMULATION badge; engine/LLM/GitHub NOT used"
                                              if self.simulation_mode else "off (real engine)",
            "SIMULATED_WORKER_SANDBOX": "SIMULATED  agents run as in-process asyncio tasks, not isolated sandboxes",
            "SIMULATED_TEST_EXECUTION": "SIMULATED  QA agent WRITES tests; K.O.D.E. does not execute them today",
            "SIMULATED_GITHUB": "SIMULATED  (flag set, but no simulator exists: dispatch refuses to run)"
                                if self.simulated_github else "off (real GitHub)",
        }


def banner(settings: Settings) -> str:
    rows = settings.component_status()
    w = max(len(k) for k in rows)
    lines = ["=" * 78, " K.O.D.E. backend - component truth table", "=" * 78]
    for k, v in rows.items():
        lines.append(f" {k.ljust(w)}  {v}")
    sim = [k for k, v in rows.items() if v.startswith("SIMULATED")]
    lines.append("-" * 78)
    lines.append(" SIMULATED components: " + (", ".join(sim) if sim else "none"))
    lines.append("=" * 78)
    return "\n".join(lines)
