"""Telegram Bot API adapter using only the Python standard library."""

from __future__ import annotations

import json
import re
import ssl
import urllib.error
import urllib.request
from typing import Protocol, runtime_checkable

import truststore

from ..models import MessageEnvelope
from .base import ChannelError, PollBatch


@runtime_checkable
class TelegramTransport(Protocol):
    def call(self, method: str, payload: dict) -> dict: ...


class TelegramBotApiTransport:
    def __init__(
        self,
        token: str,
        *,
        api_base: str = "https://api.telegram.org",
        request_timeout_s: int = 35,
        ssl_context=None,
    ):
        if not re.fullmatch(r"\d+:[A-Za-z0-9_-]{20,}", token):
            raise ValueError(
                "Telegram bot token has an invalid format; paste only the BotFather token"
            )
        self._url = f"{api_base.rstrip('/')}/bot{token}"
        self._request_timeout_s = request_timeout_s
        self._ssl_context = ssl_context or truststore.SSLContext(
            ssl.PROTOCOL_TLS_CLIENT
        )

    def call(self, method: str, payload: dict) -> dict:
        request = urllib.request.Request(
            f"{self._url}/{method}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(
                request,
                timeout=self._request_timeout_s,
                context=self._ssl_context,
            ) as response:
                body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                error_body = json.loads(exc.read().decode("utf-8"))
                description = error_body.get("description", exc.reason)
            except (ValueError, UnicodeDecodeError):
                description = exc.reason
            raise ChannelError(
                f"Telegram Bot API rejected {method} (HTTP {exc.code}): {description}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ChannelError(
                f"Telegram Bot API network error for {method}: {exc.reason}"
            ) from exc
        except TimeoutError as exc:
            raise ChannelError(f"Telegram Bot API timed out during {method}") from exc
        except ValueError as exc:
            raise ChannelError(
                f"Telegram Bot API returned invalid JSON for {method}"
            ) from exc
        if not body.get("ok"):
            description = body.get("description", "unknown Telegram API error")
            raise ChannelError(f"Telegram Bot API rejected {method}: {description}")
        return body


class TelegramAdapter:
    """Long-polling adapter restricted to text messages in private bot chats."""

    name = "telegram"

    def __init__(self, transport: TelegramTransport):
        self._transport = transport
        self._business_owner_ids: dict[str, str] = {}

    @classmethod
    def from_token(cls, token: str) -> "TelegramAdapter":
        return cls(TelegramBotApiTransport(token))

    def poll(self, *, offset: int | None = None, timeout_s: int = 25) -> PollBatch:
        payload: dict = {
            "timeout": timeout_s,
            "allowed_updates": ["message", "business_connection", "business_message"],
        }
        if offset is not None:
            payload["offset"] = offset
        body = self._transport.call("getUpdates", payload)
        updates = body.get("result", [])
        messages: list[MessageEnvelope] = []
        next_offset = offset
        for update in updates:
            update_id = update.get("update_id")
            if isinstance(update_id, int):
                next_offset = max(next_offset or 0, update_id + 1)
            connection = update.get("business_connection") or {}
            if connection:
                connection_id = connection.get("id")
                owner_id = (connection.get("user") or {}).get("id")
                if connection_id and owner_id is not None:
                    self._business_owner_ids[str(connection_id)] = str(owner_id)
                continue
            update_kind = (
                "business_message" if update.get("business_message") else "message"
            )
            message = update.get(update_kind) or {}
            chat = message.get("chat") or {}
            sender = message.get("from") or {}
            text = message.get("text")
            chat_id = chat.get("id")
            connection_id = message.get("business_connection_id")
            if (
                chat.get("type") != "private"
                or chat_id is None
                or not isinstance(text, str)
                or "sender_business_bot" in message
            ):
                continue
            sender_id = str(sender.get("id", ""))
            owner_id = self._business_owner_ids.get(str(connection_id))
            is_business = update_kind == "business_message" and bool(connection_id)
            is_outbound = is_business and (
                sender_id == owner_id if owner_id else sender_id != str(chat_id)
            )
            reply_to = message.get("reply_to_message") or {}
            messages.append(
                MessageEnvelope(
                    channel=self.name,
                    conversation_id=str(chat_id),
                    sender_id=sender_id,
                    text=text,
                    direction="outbound" if is_outbound else "inbound",
                    message_id=str(message["message_id"])
                    if "message_id" in message
                    else None,
                    metadata={
                        "update_id": update_id,
                        "update_kind": update_kind,
                        "business_connection_id": connection_id,
                        "reply_scope": (
                            f"telegram-business:{connection_id}"
                            if connection_id
                            else "telegram-bot"
                        ),
                        "reply_to_text": reply_to.get("text"),
                    },
                )
            )
        return PollBatch(messages=tuple(messages), next_offset=next_offset)

    def send(self, message: MessageEnvelope, text: str) -> None:
        payload = {"chat_id": message.conversation_id, "text": text}
        business_connection_id = message.metadata.get("business_connection_id")
        if business_connection_id:
            payload["business_connection_id"] = business_connection_id
        self._transport.call("sendMessage", payload)
