"""CLMS authentication client — M4.1b.

Scope is deliberately narrow: mint a short-lived CLMS access token from the
operator's service key. That token authenticates the EGMS archive API (see
egms_api.py).

Auth flow, per https://eea.github.io/clms-api-docs/authentication.html and
verified live: sign a JWT with the service key's RSA private key (claims iss/
sub/aud/iat/exp, RS256), POST it to @@oauth2-token as a jwt-bearer grant, and
receive an access_token valid ~1 hour. Tokens are re-minted on expiry and on a
401, per the docs' "anticipate expiry rather than predict it" guidance.

HISTORY: this module previously also implemented the CLMS @datarequest_post /
FME download flow. That flow does NOT serve EGMS — every EGMS dataset returns an
empty dataset_download_information.items (verified live against the portal), so
the request/poll/download code was removed rather than left to mislead. EGMS is
distributed through its own archive API.

Requires: pyjwt (RS256), requests.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Callable, Optional

import jwt
import requests

# --- configuration knobs (all overridable from run_egms via env) -------------
DEFAULT_BASE = "https://land.copernicus.eu"
TOKEN_SKEW_S = 60  # re-mint this many seconds before nominal expiry


class ClmsError(RuntimeError):
    """CLMS API error surfaced with an actionable message."""


class ClmsClient:
    def __init__(
        self,
        service_key: dict,
        base_url: str = DEFAULT_BASE,
        session: Optional[requests.Session] = None,
    ):
        self._key = service_key
        self._base = base_url.rstrip("/")
        self._s = session or requests.Session()
        self._access_token: Optional[str] = None
        self._token_exp: float = 0.0
        self._egms_ids: Optional[tuple[str, str]] = None  # (UID, download_info_id)

    # ---- credential loading -------------------------------------------------
    @classmethod
    def from_key_file(cls, path: str, base_url: str = DEFAULT_BASE) -> "ClmsClient":
        raw = Path(path).read_text()
        try:
            key = json.loads(raw)
        except json.JSONDecodeError as e:
            raise ClmsError(
                f"CLMS service key at {path} is not valid JSON ({e}). It must be "
                "the service-key JSON downloaded from the CLMS website "
                "(client_id, user_id, private_key, token_uri)."
            ) from e
        missing = [k for k in ("client_id", "user_id", "private_key", "token_uri") if k not in key]
        if missing:
            raise ClmsError(f"CLMS service key is missing required fields: {missing}")
        return cls(key, base_url=base_url)

    # ---- auth ---------------------------------------------------------------
    def _mint_access_token(self) -> None:
        now = int(time.time())
        claim = {
            "iss": self._key["client_id"],
            "sub": self._key["user_id"],
            "aud": self._key["token_uri"],
            "iat": now,
            "exp": now + 3600,  # docs: max 1h after iat
        }
        grant = jwt.encode(claim, self._key["private_key"], algorithm="RS256")
        resp = self._s.post(
            self._key["token_uri"],
            headers={"Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"},
            data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                  "assertion": grant},
            timeout=60,
        )
        if resp.status_code != 200:
            raise ClmsError(
                f"CLMS token exchange failed ({resp.status_code}): {resp.text[:300]}. "
                "Check that the service key is valid and not revoked."
            )
        body = resp.json()
        self._access_token = body["access_token"]
        self._token_exp = time.time() + int(body.get("expires_in", 3600)) - TOKEN_SKEW_S

    def _auth_header(self) -> dict:
        if self._access_token is None or time.time() >= self._token_exp:
            self._mint_access_token()
        return {"Authorization": f"Bearer {self._access_token}", "Accept": "application/json"}

    def _get(self, path: str, **kw):
        return self._request("GET", path, **kw)

    def _post(self, path: str, **kw):
        return self._request("POST", path, **kw)

    def _request(self, method: str, path: str, _retry_auth: bool = True, **kw):
        url = path if path.startswith("http") else f"{self._base}{path}"
        headers = {**self._auth_header(), **kw.pop("headers", {})}
        resp = self._s.request(method, url, headers=headers, timeout=kw.pop("timeout", 120), **kw)
        if resp.status_code == 401 and _retry_auth:
            # token expired mid-flight — re-mint once and retry (docs pattern)
            self._access_token = None
            return self._request(method, path, _retry_auth=False, **kw)
        return resp
