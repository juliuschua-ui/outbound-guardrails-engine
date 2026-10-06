"""Messenger channel: composed by the engine, SENT BY A HUMAN. Contract: PLAN.md §12.5.

    ═══ THERE IS NO DM-SENDING CODE PATH IN THIS FILE. NOT EVEN BEHIND A FLAG. ═══

The Messenger Platform lets a Page send only inside a 24-hour window that the USER
opens by messaging first. There is no compliant API call for a cold first touch —
not a restricted one, not a paid one, none. This engine holds no Meta token and never
asks for one.

So staging produces a card a human sends by hand:

  1. `data/outbox/messenger/<message_id>-<slug>.md` — the destination Page URL, the
     exact text in a copy-ready fence, the prospect-local send window, and the
     `dmctl mark-sent` line to run afterwards.
  2. a DASHBOARD approvals item (`{"items":[{"id","title","summary","link"}]}`) so the
     card surfaces where the operator already looks.

The operator opens the Page, pastes, sends; `dmctl mark-sent <id> --evidence "sent manually
…"` moves STAGED -> SENT.

Operating note that belongs next to the code: message the company PAGE, not a
personal profile. A Page has a published inbox and an owner who expects business
mail; a personal profile does not, and a first message to one lands in the "message
requests" folder anyway, where nobody reads it and reporting it costs the recipient
one tap.
"""

from .base import ChannelConstraints
from .manual_dm import ManualDMAdapter

_REFUSAL_NOTE = (
    "This engine does not send Messenger messages. The Messenger Platform only\n"
    "permits a Page to reply inside a 24-hour window the USER opens, so there is no\n"
    "compliant API call for a cold first touch. Open the Page, paste the text below,\n"
    "send it yourself. Company Pages only — not personal profiles."
)


class MessengerManualAdapter(ManualDMAdapter):
    name = "messenger"
    display = "Messenger"
    destination_label = "page"
    refusal_note = _REFUSAL_NOTE
    missing_address_reason = "contact has no Messenger page or profile URL"

    def constraints(self) -> ChannelConstraints:
        """Short-form, no subject. Lint L7 enforces it.

        Slightly more room than WhatsApp — four sentences, 500 characters — and the
        extra room is defensible for one reason: a Page inbox is a business inbox
        that already receives quote requests, so a message there is read in a
        commercial context rather than between a text from a supplier and one from a
        spouse. That buys a sentence, not a paragraph.

        Still no subject line, still nowhere near email's 120 words: a Page message
        that arrives as a wall of text is filed under "message requests" and never
        opened, which is the same failure as a WhatsApp block with a quieter sound.
        """
        return ChannelConstraints(max_words=None, max_sentences=4,
                                  max_chars=500, has_subject=False)


__all__ = ["MessengerManualAdapter"]
