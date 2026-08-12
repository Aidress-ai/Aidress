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
    AidressGenerateKeypairTool,
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


class TestGenerateKeypair:
    """The keypair tool is local-only and must not raise out of the agent loop."""

    KEYGEN = "langchain_aidress.tools.generate_keypair"

    def test_returns_public_key_and_path(self) -> None:
        with patch(self.KEYGEN, return_value="pub-b64") as keygen:
            out = AidressGenerateKeypairTool().invoke({"agent_id": "agent_x"})
        keygen.assert_called_once_with("agent_x")
        assert out["public_key"] == "pub-b64"
        assert out["agent_id"] == "agent_x"
        assert out["keypair_path"].endswith("/.aidress/keys/agent_x.json")

    def test_never_touches_the_network(self) -> None:
        # No SDK client should be constructed: generating a keypair is local, and
        # building a client would imply credentials this tool must not need.
        with patch(CLIENT) as client, patch(self.KEYGEN, return_value="pub"):
            AidressGenerateKeypairTool().invoke({"agent_id": "agent_x"})
        client.assert_not_called()

    def test_existing_keypair_is_an_error_dict_not_an_exception(self) -> None:
        # An escaping FileExistsError aborts the whole agent run; "you already have
        # one" is a normal branch and must come back as data the model can act on.
        with patch(self.KEYGEN, side_effect=FileExistsError("already exists at ...")):
            out = AidressGenerateKeypairTool().invoke({"agent_id": "agent_x"})
        assert out["already_exists"] is True
        assert "error" in out

    def test_missing_cryptography_is_an_error_dict(self) -> None:
        with patch(self.KEYGEN, side_effect=ImportError("needs cryptography")):
            out = AidressGenerateKeypairTool().invoke({"agent_id": "agent_x"})
        assert "cryptography" in out["error"]


class TestClientConfiguration:
    """Tool configuration is passed through to the SDK client."""

    def test_defaults(self) -> None:
        with patch(CLIENT) as client:
            AidressVerifyAgentTool().invoke({"agent_id": "a"})
        assert client.call_args.kwargs == {
            "base_url": "https://api.aidress.ai",
            "agent_key": None,
            "keypair_path": None,
            "timeout": 30.0,
            "retry_budget": 10.0,
        }

    def test_custom_config_is_forwarded(self) -> None:
        with patch(CLIENT) as client:
            AidressVerifyAgentTool(
                base_url="https://staging.example.com",
                agent_key="sk-1",
                keypair_path="/tmp/kp.json",
                timeout=5.0,
                retry_budget=2.0,
            ).invoke({"agent_id": "a"})
        assert client.call_args.kwargs == {
            "base_url": "https://staging.example.com",
            "agent_key": "sk-1",
            "keypair_path": "/tmp/kp.json",
            "timeout": 5.0,
            "retry_budget": 2.0,
        }

    def test_keypair_path_read_from_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AIDRESS_KEYPAIR_PATH", "/tmp/env-kp.json")
        with patch(CLIENT) as client:
            AidressVerifyAgentTool().invoke({"agent_id": "a"})
        assert client.call_args.kwargs["keypair_path"] == "/tmp/env-kp.json"

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

    def test_review_requires_both_party_ids(self) -> None:
        # Nothing carries the ids over from aidress_call_agent — each tool builds its
        # own client, so the SDK's handle cache is always empty here. Omitting them
        # used to produce a guaranteed 422 from the server instead of a local error.
        with pytest.raises(ValidationError):
            AidressReviewTransactionTool(agent_key="k").invoke(
                {"success": True, "score": 9, "transaction_id": "txn-1"}
            )


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
            "aidress_generate_keypair",
            "aidress_rotate_agent_key",
            "aidress_claim_bearer_key",
        }

    def test_keyless_set_can_bootstrap_a_credential(self) -> None:
        # Both routes to a first key must be reachable without one, or the toolkit
        # cannot get an agent off the ground: register → claim (needs an inbox), and
        # generate_keypair → register → signed rotate (needs nothing).
        names = {t.name for t in AidressToolkit().get_tools()}
        assert {"aidress_register_agent", "aidress_claim_bearer_key"} <= names
        assert {
            "aidress_generate_keypair",
            "aidress_register_agent",
            "aidress_rotate_agent_key",
        } <= names

    def test_agent_key_unlocks_transacting_tools(self) -> None:
        names = {t.name for t in AidressToolkit(agent_key="k").get_tools()}
        assert {
            "aidress_call_agent",
            "aidress_review_transaction",
            "aidress_update_agent",
        } <= names

    def test_include_all_returns_every_tool(self) -> None:
        assert len(AidressToolkit(include_all=True).get_tools()) == 12

    def test_config_propagates_to_tools(self) -> None:
        tools = AidressToolkit(
            base_url="https://staging.example.com", agent_key="k", timeout=5.0
        ).get_tools()
        assert all(t.base_url == "https://staging.example.com" for t in tools)
        assert all(t.timeout == 5.0 for t in tools)
