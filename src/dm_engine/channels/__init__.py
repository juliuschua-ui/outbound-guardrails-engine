"""Channel adapters. Every one stages an artifact; none of them sends.

    from dm_engine.channels import get_adapter
    get_adapter('email').stage(conn, cfg, [12, 13], dry_run=True)

PLAN.md §6. `email` writes Gmail drafts; `linkedin`, `whatsapp` and `messenger` write
copy-ready human-send cards — Meta's Business APIs forbid cold business-initiated
messaging outright, so on those channels the operator is the transport and the second
release gate is inherent. `sms` and `instagram` deliberately raise (TCPA / ToS — §6.3).
"""

from .base import ChannelAdapter, ChannelConstraints, StageResult, get_adapter
from .linkedin_manual import LinkedInManualAdapter
from .manual_dm import ManualDMAdapter
from .messenger_manual import MessengerManualAdapter
from .whatsapp_manual import WhatsAppManualAdapter

__all__ = ["ChannelAdapter", "ChannelConstraints", "StageResult", "get_adapter",
           "LinkedInManualAdapter", "ManualDMAdapter", "MessengerManualAdapter",
           "WhatsAppManualAdapter"]
