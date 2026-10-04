"""Backend persona: routers, controllers, ORM, migrations."""
from agents.base import LLMAgent
from agents.prompts import BACKEND_SYSTEM
from models import AgentRole


class BackendAgent(LLMAgent):
    role = AgentRole.backend
    persona = "API engineer: FastAPI routers, controllers, ORM models, SQL migrations"
    system_prompt = BACKEND_SYSTEM
    capabilities = ["fastapi", "pydantic", "sql-migrations", "stripe-webhooks"]
