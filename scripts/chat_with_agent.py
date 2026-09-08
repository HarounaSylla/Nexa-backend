"""Interactive CLI to chat with the WhatsApp agent over HTTP.

Hits POST /agent/simulate — the same path a WhatsApp webhook will use later.
Does not import or call traiter_message_entrant.

Requires merchant_id via --merchant-id or AGENT_TEST_MERCHANT_ID. There is
no silent default. Boutique Awa's id is printed by `python scripts/seed_products.py`
(look for `merchant_id=...`) or:

    SELECT id, name FROM merchants;

Example:

    python scripts/chat_with_agent.py --merchant-id 37292228-b8f5-437d-b8e6-2ff81d4e249d
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

DEFAULT_PHONE = "+221771234567"
DEFAULT_URL = "http://localhost:8000"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Chat with the Nexa WhatsApp agent via POST /agent/simulate. "
            "Reusing the same --phone continues the same conversation."
        )
    )
    parser.add_argument(
        "--merchant-id",
        default=os.environ.get("AGENT_TEST_MERCHANT_ID"),
        help=(
            "Merchant UUID. Falls back to AGENT_TEST_MERCHANT_ID. "
            "No silent default — get Boutique Awa's id from the seed script "
            "output (merchant_id=...) or SELECT id, name FROM merchants."
        ),
    )
    parser.add_argument(
        "--phone",
        default=DEFAULT_PHONE,
        help=f"Customer WhatsApp number (default: {DEFAULT_PHONE}).",
    )
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help=f"API base URL (default: {DEFAULT_URL}).",
    )
    return parser.parse_args()


def _require_merchant_id(merchant_id: str | None) -> str:
    if merchant_id and merchant_id.strip():
        return merchant_id.strip()
    print(
        "Missing merchant_id. Pass --merchant-id or set AGENT_TEST_MERCHANT_ID.\n"
        "There is no silent default. Find Boutique Awa's id in the seed script "
        "output (python scripts/seed_products.py, look for merchant_id=...) "
        "or query: SELECT id, name FROM merchants;",
        file=sys.stderr,
    )
    sys.exit(2)


def _send(url: str, merchant_id: str, phone: str, message: str) -> str:
    endpoint = url.rstrip("/") + "/agent/simulate"
    try:
        response = httpx.post(
            endpoint,
            json={
                "merchant_id": merchant_id,
                "customer_phone": phone,
                "message": message,
            },
            timeout=120.0,
        )
    except httpx.ConnectError:
        print(
            "Can't reach the API — is `uvicorn app.main:app --reload` running?",
            file=sys.stderr,
        )
        sys.exit(1)
    except httpx.HTTPError as exc:
        print(f"Request failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if response.status_code >= 400:
        detail = response.text
        try:
            payload = response.json()
            detail = str(payload.get("detail", payload))
        except ValueError:
            pass
        print(
            f"API error {response.status_code}: {detail}",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        return str(response.json()["reply"])
    except (ValueError, KeyError, TypeError):
        print(f"Unexpected API response: {response.text}", file=sys.stderr)
        sys.exit(1)


def main() -> None:
    args = _parse_args()
    merchant_id = _require_merchant_id(args.merchant_id)
    phone = args.phone
    url = args.url.rstrip("/")

    print(f"API: {url}")
    print(f"merchant_id: {merchant_id}")
    print(
        f"phone: {phone} "
        "(same number continues the same conversation; a new number starts fresh)"
    )
    print("Type quit or exit to stop.\n")

    while True:
        try:
            line = input("Vous: ")
        except (EOFError, KeyboardInterrupt):
            print()
            break
        message = line.strip()
        if not message:
            continue
        if message.lower() in {"quit", "exit"}:
            break
        reply = _send(url, merchant_id, phone, message)
        print(f"Agent: {reply}\n")


if __name__ == "__main__":
    main()
