from __future__ import annotations
import hashlib, re
from datetime import datetime, timezone
from typing import Any
from construction_ai.domain.models import Communication

def _clean_html(value: str) -> str:
    value=re.sub(r"<style.*?</style>|<script.*?</script>"," ",value,flags=re.I|re.S)
    value=re.sub(r"<[^>]+>"," ",value)
    return " ".join(value.replace("&nbsp;"," ").split())

def normalize_email(*, organization_id: str, source: str, message_id: str, sender: str,
                    recipients: list[str]|str, subject: str, body: str, received_at: str|datetime,
                    thread_id: str|None=None, attachments: list[str]|None=None) -> Communication:
    if isinstance(received_at,str):
        received_at=datetime.fromisoformat(received_at.replace("Z","+00:00"))
    if received_at.tzinfo is None: received_at=received_at.replace(tzinfo=timezone.utc)
    clean=_clean_html(body)
    if isinstance(recipients,str): recipients=[x.strip() for x in recipients.split(',') if x.strip()]
    raw_hash=hashlib.sha256((source+"\n"+message_id+"\n"+sender+"\n"+body).encode()).hexdigest()
    return Communication(message_id,organization_id,source,sender,[r.lower() for r in recipients],subject.strip(),clean,received_at,thread_id,attachments or [],raw_hash)

def from_gmail_webhook(payload: dict[str,Any], organization_id: str) -> Communication:
    # Adapter contract deliberately normalized; OAuth/fetching is deployment infrastructure.
    return normalize_email(organization_id=organization_id, source="gmail", message_id=str(payload["id"]),
      sender=payload.get("from","").lower(), recipients=payload.get("to",[]), subject=payload.get("subject",""),
      body=payload.get("body_html") or payload.get("body_text", ""), received_at=payload["received_at"],
      thread_id=payload.get("thread_id"), attachments=payload.get("attachments",[]))

def from_microsoft_webhook(payload: dict[str,Any], organization_id: str) -> Communication:
    return normalize_email(organization_id=organization_id, source="microsoft365", message_id=str(payload["id"]),
      sender=payload.get("from","").lower(), recipients=payload.get("to",[]), subject=payload.get("subject",""),
      body=payload.get("body_html") or payload.get("body_text", ""), received_at=payload["received_at"],
      thread_id=payload.get("conversation_id"), attachments=payload.get("attachments",[]))
