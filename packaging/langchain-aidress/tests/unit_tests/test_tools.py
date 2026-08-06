"""Standard LangChain unit tests for every Aidress tool."""

from langchain_tests.unit_tests import ToolsUnitTests

from langchain_aidress import (
    AidressCallAgentTool,
    AidressClaimBearerKeyTool,
    AidressGetAgentTool,
    AidressImportAgentTool,
    AidressListRegistryTool,
    AidressMatchAgentsTool,
    AidressRegisterAgentTool,
    AidressReviewTransactionTool,
    AidressRotateAgentKeyTool,
    AidressUpdateAgentTool,
    AidressVerifyAgentTool,
)


class TestVerifyAgentUnit(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[AidressVerifyAgentTool]:
        return AidressVerifyAgentTool

    @property
    def tool_constructor_params(self) -> dict:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {"agent_id": "agent_exa_ai"}


class TestMatchAgentsUnit(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[AidressMatchAgentsTool]:
        return AidressMatchAgentsTool

    @property
    def tool_constructor_params(self) -> dict:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {"capabilities": ["web research"]}


class TestGetAgentUnit(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[AidressGetAgentTool]:
        return AidressGetAgentTool

    @property
    def tool_constructor_params(self) -> dict:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {"agent_id": "agent_exa_ai"}


class TestListRegistryUnit(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[AidressListRegistryTool]:
        return AidressListRegistryTool

    @property
    def tool_constructor_params(self) -> dict:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {}


class TestImportAgentUnit(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[AidressImportAgentTool]:
        return AidressImportAgentTool

    @property
    def tool_constructor_params(self) -> dict:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {"domain_url": "https://example.com"}


class TestRegisterAgentUnit(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[AidressRegisterAgentTool]:
        return AidressRegisterAgentTool

    @property
    def tool_constructor_params(self) -> dict:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {"agent_id": "agent_example_01"}


class TestRotateAgentKeyUnit(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[AidressRotateAgentKeyTool]:
        return AidressRotateAgentKeyTool

    @property
    def tool_constructor_params(self) -> dict:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {"agent_id": "agent_example_01"}


class TestClaimBearerKeyUnit(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[AidressClaimBearerKeyTool]:
        return AidressClaimBearerKeyTool

    @property
    def tool_constructor_params(self) -> dict:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {"token": "claim-token-example"}


class TestUpdateAgentUnit(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[AidressUpdateAgentTool]:
        return AidressUpdateAgentTool

    @property
    def tool_constructor_params(self) -> dict:
        return {"agent_key": "aidress-agent-sk-test"}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {"agent_id": "agent_example_01", "specialty": "example"}


class TestCallAgentUnit(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[AidressCallAgentTool]:
        return AidressCallAgentTool

    @property
    def tool_constructor_params(self) -> dict:
        return {"agent_key": "aidress-agent-sk-test"}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {
            "agent_id": "agent_exa_ai",
            "payload": {"action": "search"},
            "caller_agent_id": "agent_example_01",
        }


class TestReviewTransactionUnit(ToolsUnitTests):
    @property
    def tool_constructor(self) -> type[AidressReviewTransactionTool]:
        return AidressReviewTransactionTool

    @property
    def tool_constructor_params(self) -> dict:
        return {"agent_key": "aidress-agent-sk-test"}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {
            "caller_agent_id": "agent_example_01",
            "receiver_agent_id": "agent_exa_ai",
            "success": True,
            "score": 9,
        }
