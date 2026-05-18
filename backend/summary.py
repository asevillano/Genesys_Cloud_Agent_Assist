"""Conversation summary / category extraction via Azure OpenAI chat."""
from __future__ import annotations

import asyncio
import logging
from typing import List, Tuple

from azure.identity import DefaultAzureCredential, get_bearer_token_provider
from openai import AzureOpenAI

from .config import settings

log = logging.getLogger(__name__)

_client: AzureOpenAI | None = None


def _get_client() -> AzureOpenAI | None:
    global _client
    if _client is not None:
        return _client
    if not settings.aoai_endpoint:
        return None
    if settings.aoai_key:
        _client = AzureOpenAI(
            azure_endpoint=settings.aoai_endpoint,
            api_key=settings.aoai_key,
            api_version=settings.aoai_api_version,
        )
    else:
        log.info("No AZURE_OPENAI_API_KEY set — using DefaultAzureCredential for Azure OpenAI.")
        token_provider = get_bearer_token_provider(
            DefaultAzureCredential(),
            "https://cognitiveservices.azure.com/.default",
        )
        _client = AzureOpenAI(
            azure_endpoint=settings.aoai_endpoint,
            azure_ad_token_provider=token_provider,
            api_version=settings.aoai_api_version,
        )
    return _client


async def generate_summary(turns: list[dict], categories: list[str]) -> Tuple[str, List[str]]:
    """
    turns: [{"channel": "customer|agent", "text": "..."}]
    Returns (summary_text, detected_categories).
    """
    if not turns:
        return "No conversation to summarize.", []
    client = _get_client()
    if client is None:
        return "Azure OpenAI not configured.", []

    convo = "\n".join(
        f"{('Customer' if t['channel'] == 'customer' else 'Agent')}: {t['text']}"
        for t in turns
    )
    cats = ", ".join(categories) if categories else "(none)"
    prompt = (
        "Analyse the following contact-center conversation and produce:\n"
        "1. **Summary**: 3-4 concise sentences with the main topics.\n"
        f"2. **Categories**: pick only from this list: {cats}. "
        "If none apply, say 'Ninguna categoría aplicable'.\n\n"
        "Format:\n**Resumen:**\n<text>\n\n**Categorías detectadas:**\n<list>\n\n"
        f"---\nCONVERSATION:\n{convo}\n---"
    )

    def _call():
        return client.chat.completions.create(
            model=settings.aoai_summary_deployment,
            messages=[
                {"role": "system",
                 "content": "You are an expert at analysing and summarising contact-center conversations."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.3,
            max_tokens=800,
        )

    try:
        resp = await asyncio.to_thread(_call)
        text = resp.choices[0].message.content or ""
    except Exception as e:
        log.exception("summary failed")
        return f"Error: {e}", []

    detected = [c for c in categories if c.lower() in text.lower()]
    return text, detected
