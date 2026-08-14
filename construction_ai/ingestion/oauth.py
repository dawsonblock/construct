from __future__ import annotations
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
import httpx

@dataclass
class OAuthRefreshTokenProvider:
    token_url: str
    client_id: str
    client_secret: str
    refresh_token: str
    scope: str|None = None
    _token: str|None = field(default=None, init=False)
    _expires_at: datetime|None = field(default=None, init=False)

    def get_token(self) -> str:
        now=datetime.now(timezone.utc)
        if self._token and self._expires_at and now < self._expires_at-timedelta(seconds=60):
            return self._token
        data={'grant_type':'refresh_token','client_id':self.client_id,'client_secret':self.client_secret,'refresh_token':self.refresh_token}
        if self.scope: data['scope']=self.scope
        r=httpx.post(self.token_url,data=data,timeout=30); r.raise_for_status(); payload=r.json()
        self._token=payload['access_token']; self._expires_at=now+timedelta(seconds=int(payload.get('expires_in',3600)))
        if payload.get('refresh_token'): self.refresh_token=payload['refresh_token']
        return self._token

@dataclass
class OAuthClientCredentialsProvider:
    token_url: str
    client_id: str
    client_secret: str
    scope: str
    _token: str|None = field(default=None, init=False)
    _expires_at: datetime|None = field(default=None, init=False)

    def get_token(self) -> str:
        now=datetime.now(timezone.utc)
        if self._token and self._expires_at and now < self._expires_at-timedelta(seconds=60): return self._token
        r=httpx.post(self.token_url,data={'grant_type':'client_credentials','client_id':self.client_id,'client_secret':self.client_secret,'scope':self.scope},timeout=30)
        r.raise_for_status(); payload=r.json(); self._token=payload['access_token']; self._expires_at=now+timedelta(seconds=int(payload.get('expires_in',3600))); return self._token
