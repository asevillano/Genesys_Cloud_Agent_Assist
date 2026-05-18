"""
Cosmos DB persistence.

Container: partition key /conversationId.
Documents:
  - Per turn:        {id, conversationId, type:"turn", channel, text, ts}
  - Per suggestion:  {id, conversationId, type:"suggestion", text, ts, forTurnId}
  - Per conversation:{id, conversationId, type:"conversation",
                      startedAt, endedAt, agentId, summary, categories}
If COSMOS_ENDPOINT/KEY are missing, all calls become no-ops.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from .config import settings

log = logging.getLogger(__name__)

_client = None
_container = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def is_ready() -> bool:
    """True iff the Cosmos container was successfully initialised."""
    return _container is not None


def init() -> None:
    """Initialise the Cosmos client/container (sync, called once at startup)."""
    global _client, _container
    if not settings.cosmos_endpoint:
        log.info("Cosmos DB disabled (no endpoint).")
        return
    try:
        from azure.cosmos import CosmosClient, PartitionKey

        if settings.cosmos_key:
            _client = CosmosClient(settings.cosmos_endpoint, credential=settings.cosmos_key)
        else:
            from azure.identity import DefaultAzureCredential
            log.info("No COSMOS_KEY set — using DefaultAzureCredential for Cosmos DB.")
            _client = CosmosClient(settings.cosmos_endpoint, credential=DefaultAzureCredential())
        db = _client.create_database_if_not_exists(id=settings.cosmos_database)
        _container = db.create_container_if_not_exists(
            id=settings.cosmos_container,
            partition_key=PartitionKey(path="/conversationId"),
        )
        log.info("Cosmos DB ready: %s/%s", settings.cosmos_database, settings.cosmos_container)
    except Exception as e:
        log.warning("Cosmos init failed, persistence disabled: %s", e)
        _container = None


async def _upsert(doc: dict[str, Any]) -> None:
    if _container is None:
        return
    try:
        await asyncio.to_thread(_container.upsert_item, doc)
    except Exception as e:
        log.warning("Cosmos upsert failed: %s", e)


async def save_turn(conversation_id: str, channel: str, text: str) -> str:
    turn_id = str(uuid.uuid4())
    await _upsert({
        "id": turn_id,
        "conversationId": conversation_id,
        "type": "turn",
        "channel": channel,
        "text": text,
        "ts": _now(),
    })
    return turn_id


async def save_suggestion(conversation_id: str, text: str, for_turn_id: str | None) -> None:
    await _upsert({
        "id": str(uuid.uuid4()),
        "conversationId": conversation_id,
        "type": "suggestion",
        "text": text,
        "forTurnId": for_turn_id,
        "ts": _now(),
    })


async def save_conversation_started(conversation_id: str, agent_name: str) -> None:
    await _upsert({
        "id": f"conv-{conversation_id}",
        "conversationId": conversation_id,
        "type": "conversation",
        "agentName": agent_name,
        "startedAt": _now(),
        "endedAt": None,
        "summary": None,
        "categories": [],
    })


async def save_conversation_summary(conversation_id: str, summary: str,
                                    categories: list[str]) -> None:
    if _container is None:
        return
    try:
        item = await asyncio.to_thread(
            _container.read_item, f"conv-{conversation_id}", conversation_id
        )
    except Exception:
        item = {
            "id": f"conv-{conversation_id}",
            "conversationId": conversation_id,
            "type": "conversation",
            "startedAt": _now(),
        }
    item["endedAt"] = _now()
    item["summary"] = summary
    item["categories"] = categories
    await _upsert(item)


async def get_conversation(conversation_id: str) -> dict:
    if _container is None:
        return {"conversationId": conversation_id, "turns": [], "suggestions": [],
                "summary": None, "categories": []}
    def _query():
        items = list(_container.query_items(
            query="SELECT * FROM c WHERE c.conversationId=@id",
            parameters=[{"name": "@id", "value": conversation_id}],
            partition_key=conversation_id,
        ))
        return items
    try:
        items = await asyncio.to_thread(_query)
    except Exception as e:
        log.warning("Cosmos query failed: %s", e)
        items = []
    turns = sorted([i for i in items if i.get("type") == "turn"], key=lambda x: x["ts"])
    suggs = sorted([i for i in items if i.get("type") == "suggestion"], key=lambda x: x["ts"])
    conv = next((i for i in items if i.get("type") == "conversation"), {})
    return {
        "conversationId": conversation_id,
        "turns": turns,
        "suggestions": suggs,
        "summary": conv.get("summary"),
        "categories": conv.get("categories", []),
        "startedAt": conv.get("startedAt"),
        "endedAt": conv.get("endedAt"),
        "agentName": conv.get("agentName"),
    }
