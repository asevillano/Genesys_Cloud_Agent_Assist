"""
FastAPI application.

Exposed surface:
  GET  /                          → simulator UI (Genesys agent desktop sim)
  GET  /agent-assist              → Agent Assist iframe UI
  GET  /static/*                  → static assets
  GET  /api/agents                → list available Foundry agents
  GET  /api/config                → minimal config (language, default agent...)
  POST /api/wrapup/{conv_id}      → generate summary
  GET  /api/conversations/{id}    → read from Cosmos

  WS   /ws/audiohook              → AudioHook v2 endpoint (simulator client)
  WS   /ws/assist/{conv_id}       → Agent Assist UI subscription
"""
from __future__ import annotations

import asyncio
import json
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

from fastapi import (
    FastAPI, HTTPException, Header, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from . import audiohook, cosmos_store, session_manager
from .config import settings
from .foundry_agent import FoundryAgentClient
from .stt_realtime import warm_up as stt_warm_up

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)

_log_fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s | %(message)s")
_console = logging.StreamHandler()
_console.setFormatter(_log_fmt)
_file = RotatingFileHandler(
    LOG_DIR / "app.log", maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
)
_file.setFormatter(_log_fmt)
logging.basicConfig(level=logging.INFO, handlers=[_console, _file])
log = logging.getLogger("app")

app = FastAPI(title="Genesys Cloud Agent Assist (Simulator)")
app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")

_agent_client: Optional[FoundryAgentClient] = None


@app.on_event("startup")
async def _startup() -> None:
    global _agent_client
    cosmos_store.init()
    try:
        _agent_client = FoundryAgentClient()
        log.info("Foundry AgentsClient initialised.")
    except Exception as e:
        log.error("Foundry init failed: %s", e)
        _agent_client = None
    # Pre-warm AAD tokens / Foundry list so the first "Start call" doesn't
    # pay the cold cache cost (~1-2 s per scope).
    async def _warm() -> None:
        tasks = [stt_warm_up()]
        if _agent_client is not None:
            # Touching list_agents forces the OpenAI client / AAD token used
            # for Foundry to materialise before the user clicks Start.
            tasks.append(_agent_client.list_agents())
            # Prime the agent_reference resolution path on the Foundry side
            # so the first real suggestion of the first call is fast.
            if settings.agent_name:
                tasks.append(_agent_client.warm_up_agent(settings.agent_name))
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
            log.info("Startup warm-up done.")
        except Exception as e:
            log.warning("Startup warm-up error: %s", e)
    asyncio.create_task(_warm())


def _require_agent_client() -> FoundryAgentClient:
    if _agent_client is None:
        raise HTTPException(503, "Foundry agent client not initialised. Check PROJECT_ENDPOINT and auth.")
    return _agent_client


# ───────────────────────── Pages ─────────────────────────
@app.get("/")
async def index() -> FileResponse:
    return FileResponse(FRONTEND / "simulator.html")


@app.get("/agent-assist")
async def agent_assist() -> FileResponse:
    return FileResponse(FRONTEND / "agent-assist.html")


@app.get("/favicon.ico", include_in_schema=False)
async def favicon() -> Response:
    fav = FRONTEND / "favicon.ico"
    if fav.exists():
        return FileResponse(fav, media_type="image/x-icon")
    return Response(status_code=204)


@app.get("/health", include_in_schema=False)
async def health() -> dict:
    return {"status": "ok"}


# ───────────────────────── REST API ──────────────────────
@app.get("/api/config")
async def api_config() -> dict:
    return {
        "language": settings.stt_language,
        "defaultAgentName": settings.agent_name,
        "categories": list(settings.categories),
        "cosmosEnabled": cosmos_store.is_ready(),
    }


@app.get("/api/agents")
async def api_agents() -> list[dict]:
    client = _require_agent_client()
    items = await client.list_agents()
    allowed = set(settings.allowed_agent_names)
    if allowed:
        items = [a for a in items if a["id"] in allowed]
    # ensure default agent is always present even if list call failed
    if settings.agent_name and not any(a["id"] == settings.agent_name for a in items):
        items.insert(0, {"id": settings.agent_name, "name": settings.agent_name})
    return items


@app.post("/api/wrapup/{conv_id}")
async def api_wrapup(conv_id: str) -> dict:
    sess = session_manager.get(conv_id)
    if sess is None:
        raise HTTPException(404, "No active session")
    result = await sess.wrap_up()
    # Once the wrap-up summary has been produced and persisted, tear the
    # session down. The transcript is no longer needed in memory and a new
    # call will create a fresh session with a new conversationId.
    await session_manager.drop(conv_id)
    return result


@app.get("/api/conversations/{conv_id}")
async def api_conversation(conv_id: str) -> dict:
    return await cosmos_store.get_conversation(conv_id)


# ───────────────────────── WS: Agent Assist UI ──────────
@app.websocket("/ws/assist/{conv_id}")
async def ws_assist(websocket: WebSocket, conv_id: str) -> None:
    await websocket.accept()
    try:
        sess = session_manager.get(conv_id)
        if sess is None:
            try:
                await websocket.send_text(json.dumps({"type": "session.notfound"}))
            except WebSocketDisconnect:
                return
            # keep the socket open so the UI can wait until the call starts
            while True:
                try:
                    await asyncio.sleep(1)
                    sess = session_manager.get(conv_id)
                    if sess is not None:
                        break
                except WebSocketDisconnect:
                    return
        await sess.subscribe(websocket)
        while True:
            # we don't expect client messages; keep alive
            await websocket.receive_text()
    except WebSocketDisconnect:
        pass
    finally:
        s = session_manager.get(conv_id)
        if s is not None:
            s.unsubscribe(websocket)


# ───────────────────────── WS: AudioHook v2 ─────────────
@app.websocket("/ws/audiohook")
async def ws_audiohook(
    websocket: WebSocket,
    x_api_key: Optional[str] = Header(default=None, alias="X-API-KEY"),
) -> None:
    # Optional shared-secret auth
    if settings.audiohook_api_key and x_api_key != settings.audiohook_api_key:
        await websocket.close(code=4401)
        return
    await websocket.accept()
    client = _require_agent_client()

    session_id = ""
    conv_id = ""
    server_seq = 0
    sess: Optional[session_manager.ConversationSession] = None

    try:
        while True:
            msg = await websocket.receive()
            if msg.get("type") == "websocket.disconnect":
                break

            # Binary frames = audio (interleaved µ-law 8 kHz, 2 channels)
            if "bytes" in msg and msg["bytes"] is not None:
                if sess is None:
                    continue
                cust_8k, agent_8k = audiohook.deinterleave_stereo_ulaw(msg["bytes"])
                cust_pcm24 = audiohook.upsample_8k_to_24k(cust_8k)
                agent_pcm24 = audiohook.upsample_8k_to_24k(agent_8k)
                # run both pushes concurrently
                await asyncio.gather(
                    sess.feed_customer_audio(cust_pcm24),
                    sess.feed_agent_audio(agent_pcm24),
                )
                continue

            # Text JSON protocol message
            text = msg.get("text")
            if not text:
                continue
            try:
                m = json.loads(text)
            except Exception:
                continue

            mtype = m.get("type")
            client_seq = int(m.get("seq", 0))
            session_id = m.get("id", session_id)

            if mtype == "open":
                params = m.get("parameters", {}) or {}
                conv_id = params.get("conversationId") or session_id
                input_vars = params.get("inputVariables") or {}
                requested_agent = (
                    input_vars.get("agentName")
                    or input_vars.get("agentId")  # legacy field accepted
                    or settings.agent_name
                )
                language = params.get("language") or settings.stt_language
                if not requested_agent:
                    await websocket.close(code=4400)
                    return
                sess = await session_manager.get_or_create(
                    conv_id, client, requested_agent, language,
                )
                server_seq += 1
                await websocket.send_text(json.dumps(audiohook.build_opened(
                    session_id, client_seq, server_seq,
                    channels=["external", "internal"],
                )))

            elif mtype == "ping":
                server_seq += 1
                await websocket.send_text(json.dumps(audiohook.build_pong(
                    session_id, client_seq, server_seq,
                )))

            elif mtype == "close":
                server_seq += 1
                await websocket.send_text(json.dumps(audiohook.build_closed(
                    session_id, client_seq, server_seq,
                )))
                break

            else:
                log.debug("audiohook: ignored msg type=%s", mtype)

    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.exception("audiohook error: %s", e)
    finally:
        # Keep the session alive on disconnect so the UI can still query / wrap-up.
        # Actual teardown happens via wrap-up + explicit close API if added later.
        pass


# Stop audio capture but KEEP the session in memory so the UI can still
# request /api/wrapup. The session is fully torn down after wrap-up (or
# implicitly when a new Start call creates a new conversationId).
@app.post("/api/sessions/{conv_id}/close")
async def api_close(conv_id: str) -> JSONResponse:
    sess = session_manager.get(conv_id)
    if sess is not None:
        await sess.stop_audio()
    return JSONResponse({"status": "audio-stopped"})
