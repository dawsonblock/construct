from __future__ import annotations
import base64
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Protocol
import httpx
from construction_ai.ingestion.email import normalize_email

class TokenProvider(Protocol):
    def get_token(self) -> str: ...

@dataclass
class StaticTokenProvider:
    token: str
    def get_token(self) -> str: return self.token

class HTTPJSON:
    def __init__(self, client: httpx.Client|None=None): self.client=client or httpx.Client(timeout=30)
    def get(self,url,*,headers=None,params=None):
        r=self.client.get(url,headers=headers,params=params); r.raise_for_status(); return r.json()
    def get_bytes(self,url,*,headers=None,params=None):
        r=self.client.get(url,headers=headers,params=params); r.raise_for_status(); return r.content


def _b64url(v: str) -> str:
    return base64.urlsafe_b64decode(v + '=' * (-len(v)%4)).decode('utf-8',errors='replace')

@dataclass
class GmailConnector:
    token_provider: TokenProvider
    transport: HTTPJSON = None
    base_url: str = 'https://gmail.googleapis.com/gmail/v1/users/me'
    def __post_init__(self):
        if self.transport is None: self.transport=HTTPJSON()
    def _h(self): return {'Authorization':f'Bearer {self.token_provider.get_token()}'}
    def list_message_ids(self, *, query: str|None=None, max_results: int=100):
        data=self.transport.get(f'{self.base_url}/messages',headers=self._h(),params={'q':query or '', 'maxResults':max_results})
        return [x['id'] for x in data.get('messages',[])]
    def fetch_message(self, message_id: str, organization_id: str):
        m=self.transport.get(f'{self.base_url}/messages/{message_id}',headers=self._h(),params={'format':'full'})
        headers={x['name'].lower():x['value'] for x in m.get('payload',{}).get('headers',[])}
        body=[]; attachments=[]
        def walk(part):
            mime=part.get('mimeType','')
            b=part.get('body',{})
            if b.get('data') and mime in {'text/plain','text/html'}: body.append(_b64url(b['data']))
            if b.get('attachmentId'):
                attachments.append({'attachment_id':b['attachmentId'],'filename':part.get('filename','attachment.bin'),'mime_type':mime})
            for c in part.get('parts',[]) or []: walk(c)
        walk(m.get('payload',{}))
        ts=datetime.fromtimestamp(int(m.get('internalDate','0'))/1000,tz=timezone.utc)
        c=normalize_email(organization_id=organization_id,source='gmail',message_id=m['id'],sender=headers.get('from',''),recipients=[x.strip() for x in headers.get('to','').split(',') if x.strip()],subject=headers.get('subject',''),body='\n'.join(body),received_at=ts,thread_id=m.get('threadId'),attachments=[a['filename'] for a in attachments])
        return c,attachments
    def fetch_attachment(self, message_id: str, attachment_id: str) -> bytes:
        d=self.transport.get(f'{self.base_url}/messages/{message_id}/attachments/{attachment_id}',headers=self._h())
        return base64.urlsafe_b64decode(d['data'] + '=' * (-len(d['data'])%4))

@dataclass
class MicrosoftGraphConnector:
    token_provider: TokenProvider
    transport: HTTPJSON = None
    base_url: str = 'https://graph.microsoft.com/v1.0/me'
    def __post_init__(self):
        if self.transport is None: self.transport=HTTPJSON()
    def _h(self): return {'Authorization':f'Bearer {self.token_provider.get_token()}'}
    def list_message_ids(self, *, top: int=100):
        d=self.transport.get(f'{self.base_url}/messages',headers=self._h(),params={'$top':top,'$select':'id'})
        return [x['id'] for x in d.get('value',[])]
    def fetch_message(self, message_id: str, organization_id: str):
        m=self.transport.get(f'{self.base_url}/messages/{message_id}',headers=self._h(),params={'$expand':'attachments'})
        sender=((m.get('from') or {}).get('emailAddress') or {}).get('address','')
        to=[(x.get('emailAddress') or {}).get('address','') for x in m.get('toRecipients',[]) if (x.get('emailAddress') or {}).get('address')]
        body=(m.get('body') or {}).get('content','')
        atts=[{'attachment_id':a['id'],'filename':a.get('name','attachment.bin'),'mime_type':a.get('contentType','application/octet-stream')} for a in m.get('attachments',[]) if not a.get('isInline')]
        c=normalize_email(organization_id=organization_id,source='microsoft365',message_id=m['id'],sender=sender,recipients=to,subject=m.get('subject',''),body=body,received_at=m['receivedDateTime'],thread_id=m.get('conversationId'),attachments=[a['filename'] for a in atts])
        return c,atts
    def fetch_attachment(self, message_id: str, attachment_id: str) -> bytes:
        a=self.transport.get(f'{self.base_url}/messages/{message_id}/attachments/{attachment_id}',headers=self._h())
        if 'contentBytes' not in a: raise ValueError('attachment has no contentBytes; item/reference attachments require explicit handling')
        return base64.b64decode(a['contentBytes'])
