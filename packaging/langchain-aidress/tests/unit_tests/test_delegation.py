"""Delegation tests: verify each tool calls the SDK correctly.

These cover the write tools without mutating the production registry, by
patching the SDK client rather than mocking HTTP. The SDK owns transport,
auth, retries, and timeouts; this package's job is to map tool arguments onto
the right SDK method, and that is what is asserted here.
"""

from unittest.mock import patch

import pytest
from pydantic import ValidationError

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
    AidressToolkit,
    AidressUpdateAgentTool,
    AidressVerifyAgentTool,
)

CLIENT = "langchain_aidress.tools.AidressClient"


class TestDelegation:
    """Each tool forwards to the matching SDK method with the right arguments."""

    def test_verify_calls_sdk_verify(self) -> None:
        with patch(CLIENT) as client:
            AidressVerifyAgentTool().invoke({"agent_id": "agent_exa_ai"})
        client.return_value.verify.assert_called_once_with("agent_exa_ai")

    def test_match_forwards_every_filter_in_sdk_order(self) -> None:
        with patch(CLIENT) as client:
            AidressMatchAgentsTool().invoke(
                {
                    "capabilities": ["web research"],
                    "settlement_rail": "x402",
                    "org_name": "Acme Corp",
                    "message_protocol": "mcp",
                }
            )
        client.return_value.match.assert_called_once_with(
            ["web research"], "x402", "Acme Corp", "mcp"
        )

    def test_match_defaults_unset_filters_to_none(self) -> None:
        with patch(CLIENT) as client:
            AidressMatchAgentsTool().invoke({"capabilities": ["x"]})
        client.return_value.match.assert_called_once_with(["x"], None, None, None)

    def test_match_accepts_a_non_capability_filter_alone(self) -> None:
        # /match requires at least one filter, but not specifically a capability.
        with patch(CLIENT) as client:
            AidressMatchAgentsTool().invoke({"org_name": "Acme Corp"})
        client.return_value.match.assert_called_once_with(None, None, "Acme Corp", None)

    def test_get_agent_calls_sdk_get_agent(self) -> None:
        with patch(CLIENT) as client:
            AidressGetAgentTool().invoke({"agent_id": "a"})
        client.return_value.get_agent.assert_called_once_with("a")

    def test_list_registry_calls_sdk_registry(self) -> None:
        with patch(CLIENT) as client:
            AidressListRegistryTool().invoke({})
        client.return_value.registry.assert_called_once_with()

    def test_import_agent_calls_sdk_import_agent(self) -> None:
        with patch(CLIENT) as client:
            AidressImportAgentTool().invoke({"domain_url": "https://example.com"})
        client.return_value.import_agent.assert_called_once_with("https://example.com")

    def test_register_omits_unset_fields(self) -> None:
        with patch(CLIENT) as client:
            AidressRegisterAgentTool().invoke({"agent_id": "a", "org_name": "Acme"})
        kwargs = client.return_value.register.call_args.kwargs
        assert kwargs["agent_id"] == "a"
        assert kwargs["org_name"] == "Acme"
        assert "endpoint_url" not in kwargs

    def test_rotate_calls_sdk_rotate(self) -> None:
        with patch(CLIENT) as client:
            AidressRotateAgentKeyTool().invoke({"agent_id": "a"})
        client.return_value.rotate.assert_called_once_with("a")

    def test_claim_calls_sdk_claim(self) -> None:
        with patch(CLIENT) as client:
            AidressClaimBearerKeyTool().invoke({"token": "tok-1"})
        client.return_value.claim.assert_called_once_with("tok-1")

    def test_claim_passes_a_whole_link_through_untouched(self) -> None:
        # The SDK parses "token=" out of a full URL itself; this package must not
        # pre-chew it, or a token containing "token=" would be corrupted twice.
        link = "https://api.aidress.ai/rotate?token=tok-1"
        with patch(CLIENT) as client:
            AidressClaimBearerKeyTool().invoke({"token": link})
        client.return_value.claim.assert_called_once_with(link)

    def test_update_omits_unset_fields(self) -> None:
        with patch(CLIENT) as client:
            AidressUpdateAgentTool(agent_key="k").invoke(
                {"agent_id": "a", "specialty": "freight"}
            )
        assert client.return_value.update.call_args.kwargs == {
            "agent_id": "a",
            "specialty": "freight",
        }

    def test_call_forwards_positionally_in_sdk_order(self) -> None:
        with patch(CLIENT) as client:
            AidressCallAgentTool(agent_key="k").invoke(
                {
                    "agent_id": "receiver",
                    "payload": {"action": "book"},
                    "caller_agent_id": "caller",
                    "x_payment": "pay-1",
                }
            )
        client.return_value.call.assert_called_once_with(
            "receiver", {"action": "book"}, "caller", "pay-1", None, None, None
        )

    def test_review_forwards_in_sdk_order(self) -> None:
        with patch(CLIENT) as client:
            AidressReviewTransactionTool(agent_key="k").invoke(
                {
                    "success": True,
                    "score": 9,
                    "caller_agent_id": "a",
                    "receiver_agent_id": "b",
                }
            )
        client.return_value.review.assert_called_once_with(True, 9, None, "a", "b")


class TestClientConfiguration:
    """Tool configuration is passed through to the SDK client."""

    def test_defaults(self) -> None:
        with patch(CLIENT) as client:
            AidressVerifyAgentTool().invoke({"agent_id": "a"})
        assert client.call_args.kwargs == {
            "base_url": "https://api.aidress.ai",
            "agent_key": None,
            "timeout": 30.0,
            "retry_budget": 10.0,
        }

    def test_custom_config_is_forwarded(self) -> None:
        with patch(CLIENT) as client:
            AidressVerifyAgentTool(
                base_url="https://staging.example.com",
                agent_key="sk-1",
                timeout=5.0,
                retry_budget=2.0,
            ).invoke({"agent_id": "a"})
        assert client.call_args.kwargs == {
            "base_url": "https://staging.example.com",
            "agent_key": "sk-1",
            "timeout": 5.0,
            "retry_budget": 2.0,
        }

    def test_agent_key_read_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AIDRESS_AGENT_KEY", "sk-env")
        with patch(CLIENT) as client:
            AidressVerifyAgentTool().invoke({"agent_id": "a"})
        assert client.call_args.kwargs["agent_key"] == "sk-env"

    def test_base_url_read_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AIDRESS_BASE_URL", "https://staging.example.com")
        with patch(CLIENT) as client:
            AidressVerifyAgentTool().invoke({"agent_id": "a"})
        assert client.call_args.kwargs["base_url"] == "https://staging.example.com"


class TestValidation:
    def test_review_score_must_be_between_1_and_10(self) -> None:
        with pytest.raises(ValidationError):
            AidressReviewTransactionTool(agent_key="k").invoke(
                {"success": True, "score": 11}
            )

    def test_verify_requires_agent_id(self) -> None:
        with pytest.raises(ValidationError):
            AidressVerifyAgentTool().invoke({})


class TestToolkit:
    def test_open_tools_without_credentials(self) -> None:
        names = {t.name for t in AidressToolkit().get_tools()}
        assert names == {
            "aidress_verify_agent",
            "aidress_match_agents",
            "aidress_get_agent",
            "aidress_list_registry",
            "aidress_import_agent",
            "aidress_register_agent",
            "aidress_rotate_agent_key",
            "aidress_claim_bearer_key",
        }

    def test_keyless_set_can_bootstrap_a_credential(self) -> None:
        # register → claim is the only path to a key, so both must be reachable
        # without one, or the toolkit cannot get an agent off the ground.
        names = {t.name for t in AidressToolkit().get_tools()}
        assert {"aidress_register_agent", "aidress_claim_bearer_key"} <= names

    def test_agent_key_unlocks_transacting_tools(self) -> None:
        names = {t.name for t in AidressToolkit(agent_key="k").get_tools()}
        assert {
            "aidress_call_agent",
            "aidress_review_transaction",
            "aidress_update_agent",
        } <= names

    def test_include_all_returns_every_tool(self) -> None:
        assert len(AidressToolkit(include_all=True).get_tools()) == 11

    def test_config_propagates_to_tools(self) -> None:
        tools = AidressToolkit(
            base_url="https://staging.example.com", agent_key="k", timeout=5.0
        ).get_tools()
        assert all(t.base_url == "https://staging.example.com" for t in tools)
        assert all(t.timeout == 5.0 for t in tools)
