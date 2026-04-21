"""FastAPI chatbot API: Hugging Face inference router (OpenAI-compatible) with per-session history."""

from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from openai import OpenAI
from pydantic import AliasChoices, BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from utils import SYSTEM_PROMPT


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    huggingface_api_key: str = Field(
        default="",
        validation_alias=AliasChoices("HF_TOKEN", "HUGGINGFACE_API_KEY"),
    )
    openai_api_key: str = Field(default="", validation_alias="OPENAI_API_KEY")
    hf_router_base_url: str = "https://router.huggingface.co/v1"
    chat_model: str = "deepseek-ai/DeepSeek-R1:novita"
    # Loaded from SYSTEM_PROMPT in .env when present.
    system_prompt: str = SYSTEM_PROMPT
    cors_origins: str = "*"
    max_history_messages: int = 40


def _api_key(settings: Settings) -> str:
    return (settings.huggingface_api_key or settings.openai_api_key or "").strip()


settings = Settings()
app = FastAPI(title="Website Chatbot API", version="0.1.0")

_origins = [o.strip() for o in settings.cors_origins.split(",") if o.strip()]
_wildcard_cors = not _origins or _origins == ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins if _origins else ["*"],
    allow_credentials=not _wildcard_cors,
    allow_methods=["*"],
    allow_headers=["*"],
)

_client: OpenAI | None = None
_sessions: dict[str, list[dict[str, Any]]] = defaultdict(list)
_session_lock = asyncio.Lock()


def get_client() -> OpenAI:
    global _client
    if _client is None:
        key = _api_key(settings)
        if not key:
            raise HTTPException(
                status_code=503,
                detail="LLM is not configured: set HF_TOKEN or OPENAI_API_KEY in the environment.",
            )
        _client = OpenAI(base_url=settings.hf_router_base_url, api_key=key)
    return _client


class ChatRequest(BaseModel):
    message: str = Field(..., min_length=1, max_length=16000)
    session_id: str | None = Field(default=None, max_length=128)


class ChatResponse(BaseModel):
    reply: str
    session_id: str


class MessageItem(BaseModel):
    role: str
    content: str


class HistoryResponse(BaseModel):
    session_id: str
    messages: list[MessageItem]


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/chat", response_model=ChatResponse)
async def chat(body: ChatRequest) -> ChatResponse:
    sid = body.session_id.strip() if body.session_id else str(uuid.uuid4())
    if not sid:
        sid = str(uuid.uuid4())

    user_text = body.message.strip()
    if not user_text:
        raise HTTPException(status_code=400, detail="message cannot be empty")

    client = get_client()

    async with _session_lock:
        history = _sessions[sid]
        trimmed = history[-settings.max_history_messages :]
        _sessions[sid] = trimmed
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": settings.system_prompt},
            *trimmed,
            {"role": "user", "content": user_text},
        ]

    try:
        completion = client.chat.completions.create(
            model=settings.chat_model,
            messages=messages,
        )
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"LLM request failed: {e!s}") from e

    choice = completion.choices[0].message
    reply = (choice.content or "").strip()
    if not reply:
        raise HTTPException(status_code=502, detail="LLM returned an empty reply.")

    async with _session_lock:
        _sessions[sid].append({"role": "user", "content": user_text})
        _sessions[sid].append({"role": "assistant", "content": reply})

    return ChatResponse(reply=reply, session_id=sid)


@app.get("/chat/sessions/{session_id}/history", response_model=HistoryResponse)
async def get_history(session_id: str) -> HistoryResponse:
    if not session_id.strip():
        raise HTTPException(status_code=400, detail="invalid session_id")
    sid = session_id.strip()
    async with _session_lock:
        rows = list(_sessions.get(sid, []))
    return HistoryResponse(
        session_id=sid,
        messages=[MessageItem(role=m["role"], content=m["content"]) for m in rows],
    )


@app.delete("/chat/sessions/{session_id}")
async def delete_session(session_id: str) -> dict[str, str]:
    sid = session_id.strip()
    async with _session_lock:
        _sessions.pop(sid, None)
    return {"status": "cleared", "session_id": sid}
