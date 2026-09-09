"""
Shared HTTP GET wrapper for all collectors.

Known local issue: Norton AV intercepts TLS to domains it hasn't seen before
with a malformed re-signed root cert, which raises SSLError on the *first*
request to any new domain (see config/settings.py REQUIRED_EXTERNAL_DOMAINS
and project memory market_watchlist_ssl_ca_bundle_issue). That is fixed on
the user's end via a Norton Web Shield exclusion, not from inside this code.
This wrapper catches that failure mode specifically and raises a clear,
actionable error rather than a raw traceback, so collector_runs.error_message
is useful at a glance.
"""
import requests
from requests.exceptions import SSLError

from config.settings import HTTP_TIMEOUT_SECONDS


class BlockedDomainError(Exception):
    """Raised when a request fails with an SSL error, most likely Norton TLS interception."""
    pass


def get(url: str, headers: dict | None = None, params: dict | None = None, timeout: int | None = None) -> requests.Response:
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=timeout or HTTP_TIMEOUT_SECONDS)
        resp.raise_for_status()
        return resp
    except SSLError as e:
        from urllib.parse import urlparse
        domain = urlparse(url).netloc
        raise BlockedDomainError(
            f"SSL handshake to '{domain}' failed -- likely Norton AV TLS interception. "
            f"Add '{domain}' to Norton Web Shield's HTTPS-scan exclusions, then re-run this collector. "
            f"(Original error: {e})"
        ) from e
