"""Conversation summary / category extraction via Azure OpenAI chat."""
from __future__ import annotations

import asyncio
import logging
from typing import List, Tuple

from azure.identity import AzureCliCredential, get_bearer_token_provider
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
        log.info("No AZURE_OPENAI_API_KEY set — using AzureCliCredential for Azure OpenAI.")
        token_provider = get_bearer_token_provider(
            AzureCliCredential(process_timeout=30),
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
        f"2. **Categories**: pick only from this list (keep the category names verbatim, in their original language): {cats}. "
        "If none apply, write the equivalent of 'No applicable category' in the conversation's language.\n\n"
        "IMPORTANT: Write the entire response (section headings and body) in the SAME LANGUAGE as the CONVERSATION below. "
        "Do not translate. If the conversation is in English, respond in English; if in Spanish, respond in Spanish; etc.\n\n"
        "Use this structure, translating the two headings into the conversation's language "
        "(e.g. English: 'Summary' / 'Detected categories'; Spanish: 'Resumen' / 'Categorías detectadas'):\n"
        "**<Summary heading>:**\n<text>\n\n**<Categories heading>:**\n<list>\n\n"
        f"---\nCONVERSATION:\n{convo}\n---"
    )

    def _call():
        return client.chat.completions.create(
            model=settings.aoai_summary_deployment,
            messages=[
                {"role": "system",
                 "content": (
                     "You are an expert at analysing and summarising contact-center conversations. "
                     "Always reply in the same language as the conversation you are given."
                 )},
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
