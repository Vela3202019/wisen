"""
Telegram: a second channel beside Web Push (cloud-architecture §9.2), run by
the server's one process. It long-polls Telegram's Bot API, so the server
opens no extra endpoint.

  linking        send `/link CODE` to the bot in a private chat, with a code
                 from the dashboard's Devices panel (the same one-time code
                 `wisen cloud pair` takes). The chat is then linked to that
                 user; messages from any other chat are ignored.
  notifications  a finished reflection, each proposal waiting for a decision
                 (with Approve / Reject buttons), what became of an applied
                 one. A proposal message carries its target, a short
                 description (its rationale, cut to DESCRIPTION_CAP) and a
                 link to the dashboard; never the diff or the evidence quotes,
                 so they stay off Telegram's servers (open question 8).
  buttons        a tap is a decision through Mailbox.decide, the same path
                 as the web app, with the tap's callback id as its request
                 id: a retried tap returns the first result, and a tap after
                 the web app decided says it was already decided.
  history chat   any other text is a question for the laptop's chat agent,
                 answered from reflections; `/search …` also lets it search
                 the raw history. Both are device jobs, so a question asked
                 while the laptop sleeps is answered when it wakes. The
                 answer comes back as an answer packet, never raw hits.

The bot token (WISEN_TELEGRAM_BOT_TOKEN) is part of every API URL, so no
error message or log line here may carry a URL: TelegramError names the
method and Telegram's own description only, and httpx's request log has the
token masked.
"""
import asyncio
import logging
import sys
from datetime import timedelta

import httpx

from wisen.proposals import IllegalTransition

API = "https://api.telegram.org"
POLL_TIMEOUT = 25  # seconds Telegram holds a getUpdates call open
DESCRIPTION_CAP = 300  # characters of a proposal's rationale in a message
ONLINE = timedelta(minutes=2)  # a device seen this recently is awake (it polls every 25 s)
BACKOFF_CAP = 60.0
HELP = (
    "Ask me anything about your coding sessions and I'll answer from your reflections.\n"
    "/search <words> also searches the raw history on your laptop.\n"
    "/unlink stops this chat getting messages."
)


class TelegramError(Exception):
    """A Bot API call failed. The message never holds the token."""


class _MaskToken(logging.Filter):
    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        message = record.getMessage()
        if self.token in message:
            record.msg, record.args = message.replace(self.token, "<secret>"), ()
        return True


class TelegramApi:
    def __init__(self, token: str, http: httpx.AsyncClient | None = None):
        self._token = token
        self._base = f"{API}/bot{token}"
        self._http = http or httpx.AsyncClient(timeout=httpx.Timeout(15.0, read=POLL_TIMEOUT + 15))
        logging.getLogger("httpx").addFilter(_MaskToken(token))

    async def call(self, method: str, **params) -> object:
        try:
            response = await self._http.post(f"{self._base}/{method}", json=params)
            body = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise TelegramError(f"{method}: {type(e).__name__}") from None
        if not isinstance(body, dict) or not body.get("ok"):
            description = body.get("description") if isinstance(body, dict) else None
            raise TelegramError(f"{method}: {response.status_code} {description or 'no description'}")
        return body.get("result")

    async def send(self, chat_id: int, text: str, buttons: list[list[dict]] | None = None) -> None:
        params: dict = {"chat_id": chat_id, "text": text, "link_preview_options": {"is_disabled": True}}
        if buttons:
            params["reply_markup"] = {"inline_keyboard": buttons}
        await self.call("sendMessage", **params)

    def redact(self, text: str) -> str:
        return text.replace(self._token, "<secret>")


def proposal_message(proposal_id: str, target: str, rationale: str, public_url: str) -> tuple[str, list]:
    """What Telegram may see of a proposal: its target, a short description
    and a dashboard link, plus the decision buttons."""
    description = rationale if len(rationale) <= DESCRIPTION_CAP else rationale[:DESCRIPTION_CAP - 1] + "…"
    lines = [f"Proposal: {target}", description]
    if public_url:
        lines.append(f"{public_url}/#proposal-{proposal_id}")
    buttons = [[{"text": "Approve", "callback_data": f"approve:{proposal_id}"},
                {"text": "Reject", "callback_data": f"reject:{proposal_id}"}]]
    return "\n\n".join(lines), buttons


class TelegramBot:
    """The long-polling loop and what each update does. `mailbox` holds every
    rule about the data; this only translates updates."""

    def __init__(self, mailbox, api: TelegramApi):
        self.mailbox = mailbox
        self.api = api

    async def run_forever(self) -> None:
        offset, backoff = None, 1.0
        while True:
            try:
                updates = await self.api.call("getUpdates", offset=offset, timeout=POLL_TIMEOUT,
                                              allowed_updates=["message", "callback_query"])
                backoff = 1.0
            except TelegramError as e:
                print(f"[telegram] {e}; retrying in {backoff:.0f}s", file=sys.stderr)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_CAP)
                continue
            for update in updates:
                # Confirmed by the next getUpdates call's offset. A crash before
                # that delivers the update again; every effect is keyed by a
                # request id, so handling it twice decides or asks once.
                offset = update["update_id"] + 1
                try:
                    await self.handle(update)
                except Exception as e:  # noqa: BLE001 - one bad update must not stop the bot
                    print(f"[telegram] update {update['update_id']}: {type(e).__name__}: {self.api.redact(str(e))}",
                          file=sys.stderr)

    async def handle(self, update: dict) -> None:
        if "callback_query" in update:
            await self._callback(update["callback_query"])
        elif "message" in update:
            await self._message(update["message"])

    async def _message(self, message: dict) -> None:
        chat, text = message.get("chat") or {}, (message.get("text") or "").strip()
        chat_id = chat.get("id")
        if not text or not isinstance(chat_id, int):
            return
        command, _, rest = text.partition(" ")
        command = command.split("@", 1)[0].lower()  # "/link@wisen_bot" in a menu tap
        if command == "/link":
            if chat.get("type") != "private":
                return  # a group's members could all tap the buttons
            user_id = await asyncio.to_thread(self.mailbox.link_telegram, rest.strip(), chat_id)
            await self.api.send(chat_id, f"Linked to {user_id}.\n\n{HELP}" if user_id else
                                "That code is unknown, used or expired. Get a new one from the dashboard "
                                "(Devices → Add device).")
            return
        user_id = await asyncio.to_thread(self.mailbox.telegram_user, chat_id)
        if user_id is None:
            return  # an unlinked chat is ignored
        if command == "/unlink":
            await asyncio.to_thread(self.mailbox.unlink_telegram, user_id)
            await self.api.send(chat_id, "Unlinked. Send /link CODE to link again.")
        elif command == "/search" and rest.strip():
            await self._ask(user_id, chat_id, message, "search", rest.strip())
        elif command.startswith("/"):
            await self.api.send(chat_id, HELP)
        else:
            await self._ask(user_id, chat_id, message, "ask", text)

    async def _ask(self, user_id: str, chat_id: int, message: dict, kind: str, question: str) -> None:
        await self.mailbox.enqueue_device_job(user_id, kind, {"question": question},
                                              request_id=f"telegram:{chat_id}:{message['message_id']}",
                                              rerun=False)
        awake = await asyncio.to_thread(self.mailbox.device_online, user_id, ONLINE)
        await self.api.send(chat_id, "Asking your laptop…" if awake else
                            "Queued: your laptop is offline. I'll answer when it wakes.")

    async def _callback(self, query: dict) -> None:
        message = query.get("message") or {}
        chat_id = (message.get("chat") or {}).get("id")
        if not isinstance(chat_id, int) or (query.get("from") or {}).get("id") != chat_id:
            return  # linked chats are private: the tapper is the chat
        user_id = await asyncio.to_thread(self.mailbox.telegram_user, chat_id)
        if user_id is None:
            return  # not the linked chat (or no longer): it may not decide
        action, _, proposal_id = (query.get("data") or "").partition(":")
        decision = {"approve": "approved", "reject": "rejected"}.get(action)
        if decision is None:
            return
        try:
            result = await self.mailbox.decide(user_id, proposal_id, decision, request_id=f"telegram:{query['id']}",
                                               actor=f"telegram:{user_id}", note=None)
        except (IllegalTransition, ValueError) as e:
            await self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=str(e)[:200])
            return
        if result.request_id == f"telegram:{query['id']}":
            status = f"✓ {result.decision.capitalize()}"
        else:
            where = "here" if result.actor.startswith("telegram:") else "in the web app"
            status = f"Already {result.decision} {where}"
        await self.api.call("answerCallbackQuery", callback_query_id=query["id"], text=status)
        if "message_id" in message:
            await self.api.call("editMessageText", chat_id=chat_id, message_id=message["message_id"],
                                text=f"{message.get('text', '')}\n\n{status}",
                                link_preview_options={"is_disabled": True})
