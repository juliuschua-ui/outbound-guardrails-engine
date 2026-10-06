"""In-memory Gmail test doubles. Owner D writes them; Owner E imports them.

PLAN.md §13. **Public API — keep these names stable:**

    FakeGmailService(...)          the service object adapters call
      .users().drafts().create(userId=, body=).execute()
      .users().drafts().list(userId=, q=None, maxResults=None).execute()
      .users().drafts().delete(userId=, id=).execute()
      .users().messages().list(userId=, q=None, maxResults=None).execute()
      .users().messages().get(userId=, id=, format=None).execute()
      seed_sent(to, subject=, body=, msg_id=, thread_id=)     -> message id
      seed_inbound(from_addr, subject=, snippet=, ...)        -> message id
      seed_bounce(address, msg_id=, from_addr=)               -> message id
      seed_draft(to, subject, body=, draft_id=)               -> draft id
      drafts_to(address)     -> list of draft dicts
      draft_count()          -> int
      calls                  -> list[str]  every executed API call, in order
      created_drafts         -> list[dict] one per drafts().create
    FakeServiceFactory(service=None, email=..., raises=None)
      __call__(cfg, account='operator_personal') -> (service, email)
      .calls                 -> list[str] the account names asked for
    RefreshError              auth-expiry injection; the NAME matters — the email
                              adapter recognises Gmail auth failure by class name
                              (it must not import google.auth in test-blocked code)
    FakeRefreshError          alias of RefreshError
    FakeGmailError            generic API failure injection

Failure injection:
    FakeGmailService(fail_on_call=3)          raise on the 3rd executed call
    FakeGmailService(fail_on_draft_create=3)  raise on the 3rd drafts().create
    FakeGmailService(..., fail_with=RefreshError('token expired'))

Nothing here imports a Google library, touches the network, or reads a token.
"""

import base64
import email
import itertools

__all__ = [
    "FakeGmailService",
    "FakeServiceFactory",
    "FakeGmailError",
    "RefreshError",
    "FakeRefreshError",
]


class FakeGmailError(RuntimeError):
    """Generic Gmail API failure (a 500, a quota block)."""


class RefreshError(Exception):
    """Stands in for google.auth.exceptions.RefreshError — the class NAME is the contract."""


FakeRefreshError = RefreshError


class _Request:
    """Deferred call: Gmail's client builds a request, then you .execute() it."""

    def __init__(self, service, name, fn):
        self._service = service
        self._name = name
        self._fn = fn

    def execute(self):
        self._service._tick(self._name)
        return self._fn()


class _Drafts:
    def __init__(self, service):
        self._service = service

    def create(self, userId=None, body=None):
        return _Request(self._service, "drafts.create",
                        lambda: self._service._create_draft(body))

    def list(self, userId=None, q=None, maxResults=None):
        return _Request(self._service, "drafts.list",
                        lambda: self._service._list_drafts(q, maxResults))

    def delete(self, userId=None, id=None):
        return _Request(self._service, "drafts.delete",
                        lambda: self._service._delete_draft(id))


class _Messages:
    def __init__(self, service):
        self._service = service

    def list(self, userId=None, q=None, maxResults=None):
        return _Request(self._service, "messages.list",
                        lambda: self._service._list_messages(q, maxResults))

    def get(self, userId=None, id=None, format=None):
        return _Request(self._service, "messages.get",
                        lambda: self._service._get_message(id))


class _Users:
    def __init__(self, service):
        self._service = service

    def drafts(self):
        return _Drafts(self._service)

    def messages(self):
        return _Messages(self._service)


class FakeGmailService:
    """Gmail over two dicts. Implements only the call shapes the engine uses."""

    def __init__(self, *, fail_on_call=None, fail_on_draft_create=None, fail_with=None):
        self.drafts_store: dict[str, dict] = {}
        self.messages_store: dict[str, dict] = {}
        self.calls: list[str] = []
        self.created_drafts: list[dict] = []
        self.fail_on_call = fail_on_call
        self.fail_on_draft_create = fail_on_draft_create
        self.fail_with = fail_with or FakeGmailError("fake Gmail API failure")
        self._ids = itertools.count(1)
        self._create_count = 0

    # -- plumbing ---------------------------------------------------------- #

    def users(self):
        return _Users(self)

    def _tick(self, name: str) -> None:
        self.calls.append(name)
        if self.fail_on_call is not None and len(self.calls) == self.fail_on_call:
            raise self.fail_with
        if name == "drafts.create":
            self._create_count += 1
            if (self.fail_on_draft_create is not None
                    and self._create_count == self.fail_on_draft_create):
                raise self.fail_with

    def _next(self, prefix: str) -> str:
        return f"{prefix}{next(self._ids)}"

    # -- drafts ------------------------------------------------------------ #

    def _create_draft(self, body: dict) -> dict:
        raw = ((body or {}).get("message") or {}).get("raw", "")
        parsed = _parse_raw(raw)
        draft_id = self._next("draft-")
        message_id = self._next("msg-")
        record = {
            "id": draft_id,
            "message": {"id": message_id, "threadId": self._next("thread-")},
            "to": parsed["to"],
            "subject": parsed["subject"],
            "body": parsed["body"],
        }
        self.drafts_store[draft_id] = record
        self.messages_store[message_id] = _message(
            message_id, record["message"]["threadId"], labels=["DRAFT"],
            to=parsed["to"], sender="me", subject=parsed["subject"],
            snippet=parsed["body"][:120],
        )
        self.created_drafts.append(dict(record))
        return {"id": draft_id, "message": record["message"]}

    def _list_drafts(self, q, max_results) -> dict:
        drafts = [d for d in self.drafts_store.values() if _draft_matches(d, q)]
        if max_results:
            drafts = drafts[:max_results]
        return {"drafts": [{"id": d["id"], "message": d["message"]} for d in drafts]}

    def _delete_draft(self, draft_id) -> str:
        self.drafts_store.pop(draft_id, None)
        return ""

    # -- messages ---------------------------------------------------------- #

    def _list_messages(self, q, max_results) -> dict:
        found = [m for m in self.messages_store.values() if _message_matches(m, q)]
        if max_results:
            found = found[:max_results]
        if not found:
            return {}
        return {"messages": [{"id": m["id"], "threadId": m["threadId"]} for m in found]}

    def _get_message(self, message_id) -> dict:
        try:
            return self.messages_store[message_id]
        except KeyError:
            raise FakeGmailError(f"no message {message_id}") from None

    # -- seeding + assertions ---------------------------------------------- #

    def seed_sent(self, to, subject="", body="", msg_id=None, thread_id=None) -> str:
        message_id = msg_id or self._next("sent-")
        self.messages_store[message_id] = _message(
            message_id, thread_id or self._next("thread-"), labels=["SENT"],
            to=to, sender="sam.taylor@example.com", subject=subject, snippet=body[:120],
        )
        return message_id

    def seed_inbound(self, from_addr, subject="", snippet="", msg_id=None,
                     thread_id=None) -> str:
        message_id = msg_id or self._next("in-")
        self.messages_store[message_id] = _message(
            message_id, thread_id or self._next("thread-"), labels=["INBOX"],
            to="sam.taylor@example.com", sender=from_addr, subject=subject,
            snippet=snippet,
        )
        return message_id

    def seed_bounce(self, address, msg_id=None,
                    from_addr="mailer-daemon@googlemail.com") -> str:
        message_id = msg_id or self._next("bounce-")
        self.messages_store[message_id] = _message(
            message_id, self._next("thread-"), labels=["INBOX"],
            to="sam.taylor@example.com", sender=from_addr,
            subject="Delivery Status Notification (Failure)",
            snippet=f"Address not found: {address} 550 5.1.1",
        )
        return message_id

    def seed_draft(self, to, subject, body="", draft_id=None) -> str:
        draft_id = draft_id or self._next("draft-")
        message_id = self._next("msg-")
        thread_id = self._next("thread-")
        self.drafts_store[draft_id] = {
            "id": draft_id,
            "message": {"id": message_id, "threadId": thread_id},
            "to": to, "subject": subject, "body": body,
        }
        self.messages_store[message_id] = _message(
            message_id, thread_id, labels=["DRAFT"], to=to, sender="me",
            subject=subject, snippet=body[:120],
        )
        return draft_id

    def drafts_to(self, address) -> list[dict]:
        return [d for d in self.drafts_store.values()
                if d["to"].lower() == address.lower()]

    def draft_count(self) -> int:
        return len(self.drafts_store)

    def call_count(self, name=None) -> int:
        return len(self.calls) if name is None else self.calls.count(name)


class FakeServiceFactory:
    """Drop-in for `gmail_compat.get_service`: `factory(cfg) -> (service, email)`."""

    def __init__(self, service=None, email="sam.taylor@example.com", raises=None):
        self.service = service if service is not None else FakeGmailService()
        self.email = email
        self.raises = raises
        self.calls: list[str] = []

    def __call__(self, cfg=None, account="operator_personal"):
        self.calls.append(account)
        if self.raises is not None:
            raise self.raises
        return self.service, self.email


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #

def _message(message_id, thread_id, *, labels, to, sender, subject, snippet) -> dict:
    return {
        "id": message_id,
        "threadId": thread_id,
        "labelIds": list(labels),
        "snippet": snippet,
        "payload": {"headers": [
            {"name": "To", "value": to},
            {"name": "From", "value": sender},
            {"name": "Subject", "value": subject},
        ]},
    }


def _parse_raw(raw: str) -> dict:
    """Undo what gmail_compat.create_draft did, so assertions read naturally."""
    if not raw:
        return {"to": "", "subject": "", "body": ""}
    decoded = base64.urlsafe_b64decode(raw.encode()).decode("utf-8", "replace")
    parsed = email.message_from_string(decoded)
    if parsed.is_multipart():
        body = "".join(part.get_payload(decode=True).decode("utf-8", "replace")
                       for part in parsed.get_payload())
    else:
        payload = parsed.get_payload(decode=True)
        body = payload.decode("utf-8", "replace") if payload else parsed.get_payload()
    return {"to": parsed.get("to", ""), "subject": parsed.get("subject", ""), "body": body}


def _terms(q):
    return (q or "").split()


def _header_value(message, name) -> str:
    for header in message["payload"]["headers"]:
        if header["name"].lower() == name.lower():
            return header["value"] or ""
    return ""


def _message_matches(message, q) -> bool:
    """Supports the operators the engine actually uses: in:sent, to:, from:, bare."""
    if not q:
        return True
    for term in _terms(q):
        low = term.lower()
        if low == "in:sent":
            if "SENT" not in message["labelIds"]:
                return False
        elif low.startswith("to:"):
            if term[3:].lower() not in _header_value(message, "To").lower():
                return False
        elif low.startswith("from:"):
            if term[5:].lower() not in _header_value(message, "From").lower():
                return False
        elif low.startswith("in:"):
            return False                       # unmodelled label filter: no match
        else:
            haystack = " ".join([
                message.get("snippet", ""),
                _header_value(message, "Subject"),
                _header_value(message, "To"),
                _header_value(message, "From"),
            ]).lower()
            if low not in haystack:
                return False
    # Gmail excludes drafts from message searches; so does this.
    return "DRAFT" not in message["labelIds"]


def _draft_matches(draft, q) -> bool:
    if not q:
        return True
    for term in _terms(q):
        low = term.lower()
        if low.startswith("to:"):
            if term[3:].lower() not in draft["to"].lower():
                return False
        elif low not in f"{draft['subject']} {draft['to']} {draft['body']}".lower():
            return False
    return True
