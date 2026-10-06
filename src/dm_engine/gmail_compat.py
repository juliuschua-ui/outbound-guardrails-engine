"""The ONLY module that touches the gmail-helper skill. Contract: PLAN.md §12.5.

Import rule (load-bearing): **nothing here imports a Google library at module
scope.** `create_drafts.py` and the Google SDKs are imported lazily, inside the
one function that needs a live API client. `tests/conftest.py` installs a
`sys.meta_path` blocker that raises on `googleapiclient` / `google.auth` /
`google.oauth2`, so a module-level import would kill every test that so much as
touches this file.

What is a true passthrough and what is not
------------------------------------------
- `get_service()` and `verify_batch_smtp()` delegate to the skill scripts. Both
  need the real SDK (or dnspython + port 25) anyway, and both refuse outright in
  test mode.
- `create_draft()`, `already_sent_to()` and `has_inbound_reply()` are
  reimplemented here in stdlib against the *service object*. They cannot be
  passthroughs: importing `create_drafts` pulls in `googleapiclient` at its
  module scope, which the test blocker rejects and which the fake service does
  not need. The logic is copied from `create_drafts.py` line for line —
  including which failure mode is fail-open and which is fail-closed — and
  `tests/test_channels.py::test_gmail_compat_matches_the_skill_source` greps the
  skill script to catch drift in the query strings and DEMO_LINK.

Sending: there is none, here or anywhere under src/. See PLAN.md §14 non-goal 1.
Secrets: none. The token lives at
`~/.config/skills/gmail-helper/gmail_token.json` and is only ever read
by the skill's own `get_service`; this module passes an account NAME, never a
credential.
"""

import base64
import importlib
import re
import sys
from email.mime.text import MIMEText
from pathlib import Path

from .config import Config

#: The skill's script directory. Resolves on WSL, VPS root and VPS operator.
SKILL_SCRIPTS = Path.home() / ".config" / "skills" / "gmail-helper" / "scripts"

#: Mirrors create_drafts.DEMO_LINK. Message 2 only — never in Message 1 (L1/L2/G6).
DEMO_LINK = "brightdesk.brightflowcure.com/demo-ai-voice"

#: verify_emails.py verdicts.
PASS, RISK, FAIL = "PASS", "RISK", "FAIL"

#: `contacts.address_source` values that mean a human saw the address somewhere
#: real, so a RISK verdict is still stageable (G9 / standing rule 6).
VERIFIED_SOURCES = frozenset({"website", "reply", "linkedin", "manual"})

#: The default Gmail account for cold outreach: sam.taylor@example.com.
DEFAULT_ACCOUNT = "operator_personal"

_HTML_RE = re.compile(r"<(html|body|div|br|p|a|blockquote|table)\b|&#\d+;|&lt;|&gt;", re.I)


class TestModeError(RuntimeError):
    """A test reached for a live Gmail/DNS/SMTP path. Inject a fake instead."""


class GmailCompatError(RuntimeError):
    """The gmail-helper skill is missing or has moved."""


#: The ONLY scripts this engine may import out of the skill directory.
#:
#: Load-bearing, not tidiness: `~/.config/skills/gmail-helper/scripts/` also holds
#: `send_lea635.py`, `send_lea201_25.py` and `send_scheduled_drafts.py`. A
#: by-name import with no allowlist meant `_load_skill_module('send_lea635').main()`
#: was a live send path inside a codebase whose whole invariant is that it cannot
#: send — and it is invisible to an AST scan of src/, because the send call is in
#: someone else's file. Those scripts call `require_sender_host()` at module scope,
#: which kills the import on WSL; on the VPS, which is the sending host, it does
#: not. This allowlist is the part that holds on both.
ALLOWED_SKILL_MODULES = frozenset({"create_drafts", "verify_emails"})


def _load_skill_module(name: str):
    """Import an ALLOWLISTED script from the gmail-helper skill dir. Lazy, never at import."""
    if name not in ALLOWED_SKILL_MODULES:
        raise GmailCompatError(
            f"refusing to import {name!r} from the gmail-helper skill. Only "
            f"{sorted(ALLOWED_SKILL_MODULES)} may be loaded: that directory also "
            "contains send_*.py, and this engine has no send path (PLAN.md §14 "
            "non-goal 1)."
        )
    script = SKILL_SCRIPTS / f"{name}.py"
    if not script.exists():
        raise GmailCompatError(
            f"{script} not found — the gmail-helper skill is missing or has moved. "
            "The engine reads it by path and never vendors a copy."
        )
    # sys.path is restored afterwards: leaving the skill dir on sys.path[0] for the
    # life of the process silently changes what `import <anything>` resolves to.
    added = str(SKILL_SCRIPTS) not in sys.path
    if added:
        sys.path.insert(0, str(SKILL_SCRIPTS))
    try:
        return importlib.import_module(name)
    finally:
        if added:
            try:
                sys.path.remove(str(SKILL_SCRIPTS))
            except ValueError:                      # pragma: no cover — someone else did
                pass


def _without_argv(call, *args, **kwargs):
    """Run `call` with sys.argv hidden.

    Two separate bugs need this. `create_drafts.get_service()` re-parses argv for
    '--account' and would hijack the account from an unrelated dmctl flag. And
    `_sender_host_guard.require_sender_host()` returns EARLY when '--dry-run' or
    '--verify' appears in argv — which `dmctl stage --dry-run` puts there — so a
    guard meant to pin the sending host silently disarms itself for anything the
    skill scripts do downstream.
    """
    saved_argv = sys.argv
    sys.argv = saved_argv[:1]
    try:
        return call(*args, **kwargs)
    finally:
        sys.argv = saved_argv


def get_service(cfg: Config, account: str = DEFAULT_ACCOUNT):
    """Live Gmail client. Returns `(service, sender_email)`.

    Host note: read/write of DRAFTS is host-agnostic — WSL is draft-only but
    drafting is exactly what it is allowed to do. Sending is not in this codebase
    at all, so `_sender_host_guard.require_sender_host()` has nothing to guard
    here; it stays the gate on `send_*.py` in the skill.
    """
    if cfg.test_mode:
        raise TestModeError(
            "get_service() refuses to build a real Gmail client in test mode. "
            "Pass service_factory=FakeServiceFactory(...) from tests/fakes.py."
        )
    create_drafts = _load_skill_module("create_drafts")
    return _without_argv(create_drafts.get_service, account)


def verify_batch_smtp(cfg: Config, emails: list[str]) -> list[dict]:
    """Wrap `verify_emails.verify_batch(emails, smtp=True)`.

    Returns one dict per address: `{'email', 'verdict', 'reason', 'sendable'}`
    with verdict in PASS/RISK/FAIL (read off the real function, not assumed).

    Host-sensitive: an SMTP RCPT probe from WSL's residential IP is Spamhaus-PBL
    rejected, which `verify_emails._is_ip_block` downgrades to RISK rather than a
    false FAIL. Authoritative verification runs on the VPS (PLAN.md §9.10).
    """
    if cfg.test_mode:
        raise TestModeError(
            "verify_batch_smtp() performs live DNS + SMTP probes and refuses to run "
            "in test mode. Seed contacts.verify_verdict directly in tests."
        )
    verify_emails = _load_skill_module("verify_emails")
    return _without_argv(verify_emails.verify_batch, list(emails), smtp=True)


def _is_html(body: str) -> bool:
    """Copied from create_drafts._is_html (the TICKET-656 'raw tags' bug)."""
    return bool(_HTML_RE.search(body))


def create_draft(service, to: str, subject: str, body: str) -> str:
    """Create a Gmail DRAFT and return its id. Never sends.

    Same construction as `create_drafts.create_draft`: MIMEText (html when the
    body looks like markup), urlsafe base64, `drafts().create`.
    """
    message = MIMEText(body, "html", "utf-8") if _is_html(body) else MIMEText(body)
    message["to"] = to
    message["subject"] = subject
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    draft = service.users().drafts().create(
        userId="me", body={"message": {"raw": raw}}
    ).execute()
    return draft["id"]


def already_sent_to(service, email: str) -> bool:
    """Has a message already been SENT to this address? (Gmail Sent is truth.)

    Fail-OPEN on API error, matching create_drafts: a transient API blip must not
    block an entire valid batch. The duplicate-touch invariant does not rest on
    this alone — trigger G11 allows one active Message 1 per prospect.
    """
    try:
        resp = service.users().messages().list(
            userId="me", q=f"in:sent to:{email}", maxResults=1).execute()
        return bool(resp.get("messages"))
    except Exception:
        return False


def has_inbound_reply(service, email: str) -> bool:
    """Is there a real inbound message FROM this prospect?

    Fail-CLOSED for the reply gate, matching create_drafts: no proof of a reply
    means no Message 2. (`from:` excludes Sent mail in Gmail search.)
    """
    try:
        resp = service.users().messages().list(
            userId="me", q=f"from:{email}", maxResults=1).execute()
        return bool(resp.get("messages"))
    except Exception:
        return False
