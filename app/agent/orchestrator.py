"""LangGraph ReAct loop over the OpenAI Responses API."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.models import Conversation, Message
from app.agent.prompts import build_system_prompt
from app.agent.tools import TOOLS, execute_tool
from app.catalogue.models import Merchant
from app.core.config import settings

_client: AsyncOpenAI | None = None


class AgentState(TypedDict):
    input_list: list[Any]
    new_items: list[Any]
    last_output: list[Any]
    conversation_id: str
    merchant_id: str
    output_text: str


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(api_key=settings.openai_api_key)
    return _client


def serialize_item(item: Any) -> dict[str, Any]:
    if isinstance(item, dict):
        raw = item
    elif hasattr(item, "model_dump"):
        raw = item.model_dump(mode="json")
    elif hasattr(item, "model_dump_json"):
        raw = json.loads(item.model_dump_json())
    else:
        raw = {"type": "unknown", "repr": str(item)}
    return _sanitize_item(raw)


def _sanitize_item(item: dict[str, Any]) -> dict[str, Any]:
    """Drop SDK-only fields (e.g. status) that 400 when replayed as input."""
    item_type = item.get("type")
    if item_type == "function_call":
        cleaned = {
            "type": "function_call",
            "name": item.get("name"),
            "arguments": item.get("arguments") or "",
            "call_id": item.get("call_id"),
        }
        if item.get("id"):
            cleaned["id"] = item["id"]
        return cleaned
    if item_type == "function_call_output":
        return {
            "type": "function_call_output",
            "call_id": item.get("call_id"),
            "output": item.get("output"),
        }
    if item_type == "message":
        cleaned = {
            "type": "message",
            "role": item.get("role"),
            "content": item.get("content"),
        }
        if item.get("id"):
            cleaned["id"] = item["id"]
        return cleaned
    if item_type == "reasoning":
        cleaned = {"type": "reasoning"}
        for key in ("id", "summary", "content", "encrypted_content"):
            if item.get(key) is not None:
                cleaned[key] = item[key]
        return cleaned
    if item.get("role") and item_type is None:
        return {"role": item["role"], "content": item.get("content", "")}
    return {key: value for key, value in item.items() if key != "status"}


def extract_assistant_text(items: list[dict[str, Any]], fallback: str = "") -> str:
    if fallback:
        return fallback
    for item in reversed(items):
        if item.get("type") != "message":
            continue
        role = item.get("role")
        if role not in (None, "assistant"):
            continue
        content = item.get("content")
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") in {"output_text", "text"} and part.get("text"):
                    parts.append(str(part["text"]))
            if parts:
                return "".join(parts)
    return ""


def _build_graph(db: AsyncSession):
    async def call_model(state: AgentState) -> dict[str, Any]:
        response = await _get_client().responses.create(
            model=settings.agent_model,
            input=state["input_list"],
            tools=TOOLS,
            store=False,
        )
        output_items = [serialize_item(item) for item in response.output]
        return {
            "input_list": state["input_list"] + output_items,
            "new_items": state["new_items"] + output_items,
            "last_output": output_items,
            "output_text": response.output_text or "",
        }

    async def call_tools(state: AgentState) -> dict[str, Any]:
        merchant_id = uuid.UUID(state["merchant_id"])
        conversation_id = uuid.UUID(state["conversation_id"])
        appended: list[dict[str, Any]] = []
        for item in state["last_output"]:
            if item.get("type") != "function_call":
                continue
            raw_args = item.get("arguments") or "{}"
            try:
                tool_args = json.loads(raw_args) if raw_args else {}
            except json.JSONDecodeError:
                tool_args = {}
                result = json.dumps({"error": "Malformed tool arguments"})
            else:
                result = await execute_tool(
                    db,
                    tool_name=str(item.get("name")),
                    tool_args=tool_args,
                    merchant_id=merchant_id,
                    conversation_id=conversation_id,
                )
            appended.append(
                {
                    "type": "function_call_output",
                    "call_id": item.get("call_id"),
                    "output": result,
                }
            )
        return {
            "input_list": state["input_list"] + appended,
            "new_items": state["new_items"] + appended,
            "last_output": appended,
        }

    def should_continue(state: AgentState) -> Literal["call_tools", "end"]:
        if any(item.get("type") == "function_call" for item in state["last_output"]):
            return "call_tools"
        return "end"

    graph = StateGraph(AgentState)
    graph.add_node("call_model", call_model)
    graph.add_node("call_tools", call_tools)
    graph.add_edge(START, "call_model")
    graph.add_conditional_edges(
        "call_model",
        should_continue,
        {"call_tools": "call_tools", "end": END},
    )
    graph.add_edge("call_tools", "call_model")
    return graph.compile()


async def _get_or_create_conversation(
    db: AsyncSession, merchant_id: uuid.UUID, customer_phone: str
) -> Conversation:
    result = await db.execute(
        select(Conversation).where(
            Conversation.merchant_id == merchant_id,
            Conversation.customer_phone == customer_phone,
            Conversation.status == "active",
        )
    )
    conversation = result.scalar_one_or_none()
    if conversation is not None:
        return conversation
    conversation = Conversation(
        merchant_id=merchant_id,
        customer_phone=customer_phone,
        status="active",
    )
    db.add(conversation)
    await db.flush()
    return conversation


async def traiter_message_entrant(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    customer_phone: str,
    message_text: str,
) -> str:
    """Process one inbound customer message and return the agent reply text."""
    merchant = await db.get(Merchant, merchant_id)
    if merchant is None:
        raise ValueError(f"Merchant {merchant_id} was not found")

    conversation = await _get_or_create_conversation(db, merchant_id, customer_phone)

    history = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at, Message.id)
    )
    prior_items: list[Any] = []
    for message in history.scalars().all():
        prior_items.extend(message.items or [])

    customer_item = {"role": "user", "content": message_text}
    db.add(
        Message(
            conversation_id=conversation.id,
            turn_role="customer",
            display_text=message_text,
            items=[customer_item],
        )
    )
    await db.commit()

    system_item = build_system_prompt(merchant.name)
    compiled = _build_graph(db)
    initial: AgentState = {
        "input_list": [system_item, *prior_items, customer_item],
        "new_items": [],
        "last_output": [],
        "conversation_id": str(conversation.id),
        "merchant_id": str(merchant_id),
        "output_text": "",
    }
    final_state = await compiled.ainvoke(initial, {"recursion_limit": 12})
    new_items = list(final_state["new_items"])
    reply = extract_assistant_text(new_items, fallback=final_state.get("output_text") or "")
    if not reply:
        reply = "Désolé, je n'ai pas pu répondre. Un conseiller va vous aider."

    conversation.updated_at = datetime.now(timezone.utc)
    db.add(
        Message(
            conversation_id=conversation.id,
            turn_role="agent",
            display_text=reply,
            items=new_items,
        )
    )
    await db.commit()
    return reply
