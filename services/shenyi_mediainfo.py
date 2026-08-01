"""Single-submit adapter for Shenyi's public SyncMediaInfo Item endpoint."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass
from typing import Any, Dict, Optional

import requests


EXACT_ITEM_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
DEFAULT_TIMEOUT_SECONDS = 75
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024


@dataclass(frozen=True)
class SyncResult:
    outcome: str
    reason_code: str
    response_kind: str
    http_status: Optional[int]
    response_bytes: int
    response_fingerprint: Optional[str]
    post_attempts: int = 1

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


class ShenyiMediaInfoAdapter:
    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        post=None,
    ):
        self.base_url = str(base_url or "").rstrip("/")
        self.api_key = str(api_key or "")
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.max_response_bytes = max(1024, int(max_response_bytes))
        self._post = post or requests.post

    def sync_item(self, exact_item_id: str) -> SyncResult:
        item_id = str(exact_item_id or "").strip()
        if not EXACT_ITEM_ID_RE.fullmatch(item_id):
            raise ValueError("exact item ID is invalid")
        if not self.base_url or not self.api_key:
            raise ValueError("Emby endpoint is not configured")

        url = f"{self.base_url}/Items/SyncMediaInfo"
        headers = {
            "X-Emby-Token": self.api_key,
            "Accept": "application/json",
            "Content-Length": "0",
        }
        try:
            response = self._post(
                url,
                params={"Id": item_id},
                headers=headers,
                data=b"",
                timeout=self.timeout_seconds,
                allow_redirects=False,
                stream=True,
            )
        except requests.exceptions.Timeout:
            return SyncResult(
                "ambiguous",
                "sync_timeout",
                "transport_timeout",
                None,
                0,
                None,
            )
        except requests.exceptions.SSLError:
            return SyncResult(
                "ambiguous",
                "sync_tls_error",
                "transport_tls_error",
                None,
                0,
                None,
            )
        except requests.exceptions.ConnectionError:
            return SyncResult(
                "ambiguous",
                "sync_connection_error",
                "transport_connection_error",
                None,
                0,
                None,
            )
        except requests.exceptions.RequestException:
            return SyncResult(
                "ambiguous",
                "sync_connection_error",
                "transport_request_error",
                None,
                0,
                None,
            )

        status_code = int(getattr(response, "status_code", 0) or 0)
        try:
            if 300 <= status_code < 400:
                return SyncResult(
                    "rejected",
                    "sync_redirect_rejected",
                    "redirect",
                    status_code,
                    0,
                    None,
                )
            if status_code >= 500:
                return SyncResult(
                    "ambiguous",
                    "sync_http_5xx",
                    "http_5xx",
                    status_code,
                    0,
                    None,
                )
            if status_code == 400 or 400 <= status_code < 500:
                return SyncResult(
                    "rejected",
                    "sync_item_rejected",
                    "http_4xx",
                    status_code,
                    0,
                    None,
                )
            if not 200 <= status_code < 300:
                return SyncResult(
                    "rejected",
                    "sync_item_rejected",
                    "unexpected_status",
                    status_code,
                    0,
                    None,
                )

            body = bytearray()
            for chunk in response.iter_content(chunk_size=64 * 1024):
                if not chunk:
                    continue
                body.extend(chunk)
                if len(body) > self.max_response_bytes:
                    return SyncResult(
                        "failed",
                        "sync_response_too_large",
                        "response_too_large",
                        status_code,
                        len(body),
                        None,
                    )
            raw = bytes(body)
            digest = hashlib.sha256(raw).hexdigest()
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeError, ValueError, TypeError):
                return SyncResult(
                    "failed",
                    "sync_empty_result",
                    "invalid_json",
                    status_code,
                    len(raw),
                    digest,
                )
            if payload == [] or payload is None:
                return SyncResult(
                    "failed",
                    "sync_empty_result",
                    "empty_array",
                    status_code,
                    len(raw),
                    digest,
                )
            if not isinstance(payload, (list, dict)):
                return SyncResult(
                    "failed",
                    "sync_empty_result",
                    "invalid_structure",
                    status_code,
                    len(raw),
                    digest,
                )
            if isinstance(payload, list) and not payload:
                return SyncResult(
                    "failed",
                    "sync_empty_result",
                    "empty_array",
                    status_code,
                    len(raw),
                    digest,
                )
            return SyncResult(
                "submitted",
                "readback_not_ready",
                "nonempty_json",
                status_code,
                len(raw),
                digest,
            )
        except requests.exceptions.Timeout:
            return SyncResult(
                "ambiguous",
                "sync_timeout",
                "response_timeout",
                status_code,
                0,
                None,
            )
        except requests.exceptions.SSLError:
            return SyncResult(
                "ambiguous",
                "sync_tls_error",
                "response_tls_error",
                status_code,
                0,
                None,
            )
        except requests.exceptions.ConnectionError:
            return SyncResult(
                "ambiguous",
                "sync_connection_error",
                "response_connection_error",
                status_code,
                0,
                None,
            )
        except requests.exceptions.RequestException:
            return SyncResult(
                "ambiguous",
                "sync_connection_error",
                "response_request_error",
                status_code,
                0,
                None,
            )
        finally:
            try:
                response.close()
            except Exception:
                pass
