"""FastAPI app + lifespan. Run: uvicorn app:app --port 8000 (from backend/)."""
from __future__ import annotations

import logging
from pathlib import Path
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Callable, Dict, Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse
from fastapi import Request
from fastapi.staticfiles import StaticFiles

from agents.backend import BackendAgent
from agents.base import LLMAgent
from agents.frontend import FrontendAgent
from agents.qa import QAAgent
from config import Settings, banner
from events import EventBus
from llm import LLMClient, build_llm
from models import AgentRole
from pipeline.github import GitHubClient, GitHubPort
from routes import router
from bridge.spike import BridgeState, router as bridge_router
from hub.gateway import Hub, load_or_create_key, router as hub_router
from hub.join import is_local_request, make_router as make_join_router
from hub.store import HubStore
from state import State

log = logging.getLogger("kode")
VERSION = "0.3.0"


@dataclass
class Services:
    settings: Settings
    state: State
    bus: EventBus
    llm: LLMClient
    github_override: Optional[GitHubPort] = None          # tests inject a recording double here
    _github: Optional[GitHubPort] = field(default=None, repr=False)

    def get_github(self) -> GitHubPort:
        if self.github_override is not None:
            return self.github_override
        if self._github is None:
            self._github = GitHubClient(self.settings)     # raises MissingConfigError if unconfigured
        return self._github

    def make_agents(self) -> Dict[AgentRole, LLMAgent]:
        cls = {AgentRole.backend: BackendAgent, AgentRole.frontend: FrontendAgent, AgentRole.qa: QAAgent}
        return {role: c(self.llm, self.settings.author_for(role.value), self.bus.emit, self.settings.agent_wall_budget_s) for role, c in cls.items()}


def create_app(settings: Optional[Settings] = None, *, llm: Optional[LLMClient] = None,
               github: Optional[GitHubPort] = None) -> FastAPI:
    settings = settings or Settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state = await State(settings.kode_db_path).init()
        bus = EventBus(state)
        if settings.kode_enable_bridge:
            app.state.bridge = BridgeState(Path(settings.kode_bridge_cwd).resolve())
        if settings.kode_enable_hub:
            hub = Hub(HubStore(Path(settings.kode_hub_db).resolve()), load_or_create_key(Path(settings.kode_hub_key_file).resolve()))
            hub.start()
            hub.orch.default_llm = build_llm(settings)
            hub.orch.default_label = settings.llm_model if settings.llm_key() else ""
            app.state.hub = hub
        app.state.services = Services(settings, state, bus, llm or build_llm(settings), github_override=github)
        print(banner(settings), flush=True)
        yield
        if getattr(app.state, "hub", None):
            await app.state.hub.stop()
            app.state.hub.store.close()
        await state.close()

    app = FastAPI(title="K.O.D.E. engine", version="0.1.0", lifespan=lifespan)
    app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"], allow_headers=["*"])
    app.include_router(router)
    app.include_router(hub_router)
    app.include_router(bridge_router)                  # endpoints 404/deny unless KODE_ENABLE_BRIDGE=1

    CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
           "connect-src 'self' ws: wss:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")

    @app.middleware("http")
    async def security_headers(request: Request, call_next):
        resp = await call_next(request)
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        p = request.url.path
        if (p in ("/", "/guide", "/classic", "/console", "/console/") or p.startswith("/join/")) and settings.kode_enable_hub:      # strict CSP on pages that talk to the Brain
            resp.headers["Content-Security-Policy"] = CSP
        return resp

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"ok": True}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        hub = getattr(app.state, "hub", None)
        return {"ready": True, "hub": bool(hub), "machines_online": sum(1 for n in hub.nodes.values() if n.online) if hub else 0, "version": VERSION}

    @app.get("/health")
    async def health():
        return {"ok": True, "simulation_mode": settings.simulation_mode,
                "mode": "SIMULATION" if settings.simulation_mode else "ENGINE", "minimal": settings.kode_minimal,
                "components": settings.component_status()}

    # Serve ONLY the explicit frontend files - never the project folder (it contains backend/.env).
    fe = settings.frontend_dir
    site_dir = fe / "console"
    if settings.kode_enable_hub and (site_dir / "landing.html").exists():          # landing + guide; the original single-page UI moves to /classic
        for route, name in (("/", "landing-v2.html" if (site_dir / "landing-v2.html").exists() else "landing.html"), ("/landing-v1", "landing.html"), ("/guide", "guide.html")):
            app.add_api_route(route, (lambda p=site_dir / name: FileResponse(p, media_type="text/html", headers={"Cache-Control": "no-store"})),
                              methods=["GET"], include_in_schema=False)
    app.add_api_route("/classic", (lambda: FileResponse(fe / "index.html", media_type="text/html", headers={"Cache-Control": "no-store"})),
                      methods=["GET"], include_in_schema=False)
    for route, name, mime in (("/", "index.html", "text/html"), ("/index.html", "index.html", "text/html"),
                              ("/app.js", "app.js", "text/javascript"), ("/styles.css", "styles.css", "text/css")):
        if (fe / name).exists():
            app.add_api_route(route, (lambda p=fe / name, m=mime: FileResponse(p, media_type=m, headers={"Cache-Control": "no-store"})),
                              methods=["GET"], include_in_schema=False)
    console_dir = fe / "console"
    if settings.kode_enable_hub and console_dir.is_dir():
        app.include_router(make_join_router(lambda: getattr(app.state, "hub", None), console_dir))
        app.mount("/console/assets", StaticFiles(directory=console_dir / "assets"), name="console-assets")

        @app.get("/console", include_in_schema=False)
        @app.get("/console/", include_in_schema=False)
        async def console_page(request: Request):
            html = (console_dir / "index.html").read_text(encoding="utf-8")
            hub = getattr(app.state, "hub", None)
            lead_ok = hub is not None and is_local_request(request)      # a tunnel/proxy on this PC also connects from 127.0.0.1: never trust that alone
            html = html.replace("__LEAD_KEY__", hub.lead_key if lead_ok else "")      # the lead key is only ever served to loopback
            return HTMLResponse(html, headers={"Cache-Control": "no-store"})
    if (settings.kode_enable_bridge or settings.kode_enable_hub) and (fe / "vendor").is_dir():
        app.mount("/vendor", StaticFiles(directory=fe / "vendor"), name="vendor")     # vendored xterm.js (MIT)
    if (fe / "fonts").is_dir():
        app.mount("/fonts", StaticFiles(directory=fe / "fonts"), name="fonts")

    return app


app = create_app()
