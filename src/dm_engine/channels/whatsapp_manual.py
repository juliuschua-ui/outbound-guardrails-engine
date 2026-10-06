"""WhatsApp channel: composed by the engine, SENT BY A HUMAN. Contract: PLAN.md §12.5.

    ═══ THERE IS NO DM-SENDING CODE PATH IN THIS FILE. NOT EVEN BEHIND A FLAG. ═══

There is nothing here to automate, and that is not a limitation being worked around:

  * The WhatsApp Business Platform prohibits cold, business-initiated messaging. An
    outbound conversation requires prior opt-in from the recipient AND a
    pre-approved message template. A scraped contractor has given neither, so there
    is no compliant API call for a first touch. This engine does not hold a Meta
    token and never asks for one.
  * On the consumer app, bans are spam-DETECTION driven — volume, velocity,
    near-identical text, low reply rates — and the account lost is the phone number
    itself. That number is on the operator's vans, his invoices and his Google listing.

So staging produces a card a human sends by hand:

  1. `data/outbox/whatsapp/<message_id>-<slug>.md` — the destination number, the
     exact text in a copy-ready fence, the prospect-local send window, and the
     `dmctl mark-sent` line to run afterwards.
  2. a DASHBOARD approvals item (`{"items":[{"id","title","summary","link"}]}`) so the
     card surfaces where the operator already looks.

The operator opens WhatsApp, pastes, sends; `dmctl mark-sent <id> --evidence "sent
manually …"` moves STAGED -> SENT. The human IS the transport, so the second release
gate is inherent.

Operating note that belongs next to the code: message BUSINESS numbers — the one on
the company's website, its Google listing, its truck — not a personal mobile scraped
from somewhere else. The first is a published channel for exactly this; the second
reads as a stranger with your number, and reports are what train the detector.
"""

from .base import ChannelConstraints
from .manual_dm import ManualDMAdapter

_REFUSAL_NOTE = (
    "This engine does not send WhatsApp messages. Meta's Business Platform forbids\n"
    "cold business-initiated messaging (it needs opt-in plus an approved template),\n"
    "and consumer-app bans cost the number itself. Open WhatsApp, paste the text\n"
    "below, send it yourself. Business numbers only — not a personal mobile."
)


class WhatsAppManualAdapter(ManualDMAdapter):
    name = "whatsapp"
    display = "WhatsApp"
    destination_label = "number"
    refusal_note = _REFUSAL_NOTE
    missing_address_reason = "contact has no WhatsApp number"

    def constraints(self) -> ChannelConstraints:
        """The tightest shape of any channel. Lint L7 enforces it.

        Three sentences, no subject: a cold WhatsApp message is read on a phone, in a
        thread otherwise full of family and suppliers, and length alone is what makes
        it read as a blast. The sentence cap is the limit that actually binds — three
        sentences of contractor-plain English land at roughly 250–350 characters.

        400 characters is the backstop, chosen so a message that clears the sentence
        cap still fits a WhatsApp bubble without the "Read more" fold, which is the
        visual tell of pasted marketing copy. It is deliberately NOT generous: the
        room is there for one long specific hook (a project, a street, a year), not
        for a second paragraph.
        """
        return ChannelConstraints(max_words=None, max_sentences=3,
                                  max_chars=400, has_subject=False)


__all__ = ["WhatsAppManualAdapter"]
