"""Standard LangChain integration tests for the read-only Aidress tools.

These run against the live public registry at https://api.aidress.ai, which
needs no credentials. The write tools (register, rotate, claim, update, call,
review) are deliberately not integration-tested here: they would mutate the
production registry. Their argument mapping onto the SDK is covered in
``tests/unit_tests/test_delegation.py`` with the SDK client patched out.
"""

from langchain_tests.integration_tests import ToolsIntegrationTests

from langchain_aidress import (
    AidressGetAgentTool,
    AidressListRegistryTool,
    AidressMatchAgentsTool,
    AidressVerifyAgentTool,
)


class TestVerifyAgentIntegration(ToolsIntegrationTests):
    @property
    def tool_constructor(self) -> type[AidressVerifyAgentTool]:
        return AidressVerifyAgentTool

    @property
    def tool_constructor_params(self) -> dict:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {"agent_id": "agent_exa_ai"}


class TestMatchAgentsIntegration(ToolsIntegrationTests):
    @property
    def tool_constructor(self) -> type[AidressMatchAgentsTool]:
        return AidressMatchAgentsTool

    @property
    def tool_constructor_params(self) -> dict:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {"capabilities": ["web research"]}


class TestGetAgentIntegration(ToolsIntegrationTests):
    @property
    def tool_constructor(self) -> type[AidressGetAgentTool]:
        return AidressGetAgentTool

    @property
    def tool_constructor_params(self) -> dict:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {"agent_id": "agent_exa_ai"}


class TestListRegistryIntegration(ToolsIntegrationTests):
    @property
    def tool_constructor(self) -> type[AidressListRegistryTool]:
        return AidressListRegistryTool

    @property
    def tool_constructor_params(self) -> dict:
        return {}

    @property
    def tool_invoke_params_example(self) -> dict:
        return {}
