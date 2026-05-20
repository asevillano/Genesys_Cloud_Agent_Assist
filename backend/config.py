"""Centralized configuration loaded from environment / .env."""
from __future__ import annotations

import os
from dataclasses import dataclass
from typing import List, Optional

from dotenv import load_dotenv

load_dotenv(override=True)


def _csv(value: Optional[str]) -> List[str]:
    if not value:
        return []
    return [x.strip() for x in value.split(",") if x.strip()]


@dataclass(frozen=True)
class Settings:
    # Azure OpenAI
    aoai_endpoint: str = os.getenv("AZURE_OPENAI_ENDPOINT", "")
    aoai_key: str = os.getenv("AZURE_OPENAI_API_KEY", "")
    aoai_api_version: str = os.getenv("AZURE_OPENAI_API_VERSION", "2024-10-01-preview")
    aoai_transcribe_deployment: str = os.getenv(
        "AZURE_OPENAI_TRANSCRIBE_DEPLOYMENT", "gpt-4o-mini-transcribe"
    )
    aoai_summary_deployment: str = os.getenv(
        "AZURE_OPENAI_SUMMARY_DEPLOYMENT", "gpt-4.1-mini"
    )

    # Foundry (Agents v2 — agents are identified by name, not GUID)
    project_endpoint: str = os.getenv("PROJECT_ENDPOINT", "")
    agent_name: str = os.getenv("AGENT_NAME", os.getenv("AGENT_ID", ""))
    allowed_agent_names: tuple = tuple(
        _csv(os.getenv("ALLOWED_AGENT_NAMES", os.getenv("ALLOWED_AGENT_IDS")))
    )

    # Cosmos
    cosmos_endpoint: str = os.getenv("COSMOS_ENDPOINT", "")
    cosmos_key: str = os.getenv("COSMOS_KEY", "")
    cosmos_database: str = os.getenv("COSMOS_DATABASE", "agentassist")
    cosmos_container: str = os.getenv("COSMOS_CONTAINER", "conversations")

    # Runtime
    stt_language: str = os.getenv("STT_LANGUAGE", "es")
    categories: tuple = tuple(_csv(os.getenv("CONVERSATION_CATEGORIES", "Invoices,Products")))
    port: int = int(os.getenv("PORT", "8000"))
    audiohook_api_key: str = os.getenv("AUDIOHOOK_API_KEY", "")

    @property
    def cosmos_enabled(self) -> bool:
        return bool(self.cosmos_endpoint)


settings = Settings()


def get_credential():
    """Return the Azure credential appropriate for the current environment.

    In Azure (Container Apps / VMs / etc.) the user-assigned managed identity's
    client id is injected via the ``AZURE_CLIENT_ID`` environment variable —
    use ``ManagedIdentityCredential`` directly to avoid the slow probing chain.
    Locally fall back to ``AzureCliCredential`` (``az login``) which is fast
    and predictable on developer machines.
    """
    client_id = os.getenv("AZURE_CLIENT_ID")
    if client_id:
        from azure.identity import ManagedIdentityCredential
        return ManagedIdentityCredential(client_id=client_id)
    from azure.identity import AzureCliCredential
    return AzureCliCredential(process_timeout=30)
