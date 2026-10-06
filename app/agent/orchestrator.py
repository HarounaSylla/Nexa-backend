"""LangGraph ReAct loop over the OpenAI Responses API."""

from __future__ import annotations

import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.handover import handover_developer_item, item_customer_text
from app.agent.models import Conversation, Message
from app.agent.prompts import build_system_prompt
from app.agent.service import (
    STATUS_ACTIVE,
    STATUS_CLOSED,
    STATUS_ESCALATED,
    TURN_ROLE_CUSTOMER,
    notifier_message_escalade,
    trouver_conversation_escaladee,
    trouver_conversation_ouverte,
)
from app.agent.tools import TOOLS, execute_tool
from app.catalogue.models import Merchant
from app.core.config import settings
from app.core.phone import try_normalize_phone
from app.merchants.service import get_preferences
from app.orders.service import lister_zones_livraison, phone_lookup_variants

CONVERSATION_INACTIVITY_TIMEOUT = timedelta(hours=48)
CONVERSATION_REOPEN_GRACE_PERIOD = timedelta(minutes=15)

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


def history_items_from_messages(messages: list[Message]) -> list[Any]:
    prior_items: list[Any] = []
    for message in messages:
        for item in message.items or []:
            if _is_replayable_history_item(item):
                prior_items.append(item)
    return prior_items


def prior_items_for_agent(messages: list[Message]) -> list[Any]:
    """Replayable history plus at most one handover developer note."""
    prior_items = history_items_from_messages(messages)
    note = handover_developer_item(prior_items)
    if note is not None:
        prior_items.append(note)
    return prior_items


def _is_replayable_history_item(item: Any) -> bool:
    """Keep Responses API items. Legacy rows with items=[] contribute nothing."""
    if not isinstance(item, dict):
        return False
    if item.get("type") == "inbound_image":
        return False
    if item.get("role") or item.get("type") in {
        "function_call",
        "function_call_output",
        "message",
        "reasoning",
    }:
        return True
    return False


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
    canonical = try_normalize_phone(customer_phone)
    variants = phone_lookup_variants(canonical)
    now = datetime.now(timezone.utc)

    open_row = await trouver_conversation_ouverte(db, merchant_id, canonical)
    closed_stale = False
    if open_row is not None:
        if open_row.status == STATUS_ESCALATED:
            return open_row
        age = now - open_row.updated_at
        if age <= CONVERSATION_INACTIVITY_TIMEOUT:
            return open_row
        open_row.status = STATUS_CLOSED
        await db.flush()
        closed_stale = True

    if not closed_stale:
        result = await db.execute(
            select(Conversation)
            .where(
                Conversation.merchant_id == merchant_id,
                Conversation.customer_phone.in_(variants),
                Conversation.status == STATUS_CLOSED,
            )
            .order_by(Conversation.updated_at.desc())
            .limit(1)
        )
        closed = result.scalar_one_or_none()
        if closed is not None:
            age = now - closed.updated_at
            if age <= CONVERSATION_REOPEN_GRACE_PERIOD:
                try:
                    async with db.begin_nested():
                        closed.status = STATUS_ACTIVE
                        await db.flush()
                    return closed
                except IntegrityError:
                    found = await trouver_conversation_ouverte(
                        db, merchant_id, canonical
                    )
                    if found is None:
                        raise
                    return found

    try:
        async with db.begin_nested():
            conversation = Conversation(
                merchant_id=merchant_id,
                customer_phone=canonical,
                status=STATUS_ACTIVE,
            )
            db.add(conversation)
            await db.flush()
            return conversation
    except IntegrityError:
        found = await trouver_conversation_ouverte(db, merchant_id, canonical)
        if found is None:
            raise
        return found


async def _store_escalated_inbound(
    db: AsyncSession,
    conversation: Conversation,
    merchant_id: uuid.UUID,
    customer_phone: str,
    message_text: str,
) -> None:
    db.add(
        Message(
            conversation_id=conversation.id,
            turn_role=TURN_ROLE_CUSTOMER,
            display_text=message_text,
            items=[item_customer_text(message_text)],
        )
    )
    conversation.updated_at = datetime.now(timezone.utc)
    await notifier_message_escalade(db, merchant_id, conversation, customer_phone)
    await db.commit()


async def traiter_message_entrant(
    db: AsyncSession,
    merchant_id: uuid.UUID,
    customer_phone: str,
    message_text: str,
    now: datetime | None = None,
) -> str | None:
    """Process one inbound customer message and return the agent reply text.

    Returns None when the conversation is escalated: the message is stored
    on that thread and no WhatsApp reply should be sent.

    `now` overrides the clock used in the merchant-settings prompt block.
    Tests and proof scripts may pass it; the public simulate route does not.
    """
    merchant = await db.get(Merchant, merchant_id)
    if merchant is None:
        raise ValueError(f"Merchant {merchant_id} was not found")

    phone = try_normalize_phone(customer_phone)
    escalated = await trouver_conversation_escaladee(db, merchant_id, phone)
    if escalated is not None:
        await _store_escalated_inbound(
            db, escalated, merchant_id, phone, message_text
        )
        return None

    conversation = await _get_or_create_conversation(db, merchant_id, phone)

    history = await db.execute(
        select(Message)
        .where(Message.conversation_id == conversation.id)
        .order_by(Message.created_at, Message.id)
    )
    prior_items = prior_items_for_agent(list(history.scalars().all()))

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

    preferences = await get_preferences(db, merchant_id)
    zones = await lister_zones_livraison(db, merchant_id)
    available_cities = [zone.city for zone in zones if zone.available]
    system_item = build_system_prompt(
        merchant.name,
        preferences,
        available_cities=available_cities,
        now=now,
    )
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
