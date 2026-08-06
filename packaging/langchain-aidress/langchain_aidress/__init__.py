"""Aidress agent trust and discovery tools for LangChain.

Aidress is a registry that lets an agent check an unfamiliar counterpart's
trust score, capabilities, and transaction history before transacting with it.

Thin adapters over the official ``aidress-sdk`` client.
"""

from importlib import metadata

from langchain_aidress.toolkit import AidressToolkit
from langchain_aidress.tools import (
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

try:
    __version__ = metadata.version(__package__)
except metadata.PackageNotFoundError:
    __version__ = ""
del metadata

__all__ = [
    "AidressCallAgentTool",
    "AidressClaimBearerKeyTool",
    "AidressGetAgentTool",
    "AidressImportAgentTool",
    "AidressListRegistryTool",
    "AidressMatchAgentsTool",
    "AidressRegisterAgentTool",
    "AidressReviewTransactionTool",
    "AidressRotateAgentKeyTool",
    "AidressToolkit",
    "AidressUpdateAgentTool",
    "AidressVerifyAgentTool",
    "__version__",
]
