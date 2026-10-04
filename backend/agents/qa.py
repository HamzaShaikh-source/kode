"""QA persona: e2e + unit suites, contract-compliance, review notes."""
from agents.base import LLMAgent
from agents.prompts import QA_SYSTEM
from models import AgentRole


class QAAgent(LLMAgent):
    role = AgentRole.qa
    persona = "Test engineer: Playwright e2e, pytest contract-compliance, review notes"
    system_prompt = QA_SYSTEM
    capabilities = ["playwright", "pytest", "contract-testing"]
