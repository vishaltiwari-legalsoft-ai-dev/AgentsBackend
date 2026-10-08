"""The PDF renderer service client — the ONE path every HTML→PDF call takes.

The board report PDF, the vendor report PDF and (Phase 2) the template preview
all call :func:`render_pdf`. The renderer is another Cloud Run service
(``renderer/``), deployed PRIVATE: Google's front end admits only a caller
presenting a Google-signed ID token whose audience is the renderer's URL and
whose identity holds ``run.invoker`` on it. The app inside then checks its own
shared secret. So every request carries two credentials:

* ``Authorization: Bearer <Google ID token>`` — audience ``RENDERER_URL``,
  minted from this service's own identity (the metadata server on Cloud Run, a
  service-account key locally). This is what the front end checks.
* ``X-Renderer-Token: <RENDERER_TOKEN>`` — what ``renderer/src/app.js`` checks.
  The renderer does NOT trim what it receives, so this side strips it.

Every call is treated as hostile: explicit timeouts, a retry policy that only
retries what a retry can fix, and one defined, honest answer per failure —
raised as :class:`RendererError` (status + caller-safe detail). There is no
fallback document and there must never be one: a request is never sent without
the ID token, and HTML under a ``.pdf`` name is not a PDF.

Secrets are named, never echoed: the token values appear in no log line and no
error detail.
"""

from __future__ import annotations

import logging
import os
import time

import httpx

logger = logging.getLogger("agentos.pdf_renderer")

#: The request contract ``renderer/src/app.js`` checks (its ``v`` field).
CONTRACT_VERSION = 1

#: Attempts for one document. The render is a pure function of the HTML and
#: the renderer holds no state, so a retry is safe — but expensive (a Chromium
#: page load), so only a refused/dropped connection and the 502/504 a cold
#: ``min-instances=0`` instance answers are retried. NOT retried: a read
#: timeout (doubles the wait), any 4xx, and 503 (the renderer's deliberate
#: fail-closed answer).
ATTEMPTS = 2
BACKOFF_SECONDS = 1.0
RETRY_STATUS = frozenset({502, 504})


class RendererError(Exception):
    """A PDF could not be produced. ``status`` is the HTTP status the caller
    should answer with; ``detail`` is safe to show the user."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def read_timeout() -> float:
    """Seconds to wait for the render itself — generous on purpose: the
    renderer's own budget is 60s layout + 60s print on top of a cold start."""
    try:
        return max(1.0, float(os.environ.get("RENDERER_TIMEOUT_SECONDS", "150")))
    except ValueError:
        return 150.0


def config() -> tuple[str, str]:
    """``(url, token)``, both stripped; either may be ``""`` when unset."""
    url = os.environ.get("RENDERER_URL", "").strip().rstrip("/")
    token = os.environ.get("RENDERER_TOKEN", "").strip()
    return url, token


def missing_config() -> list[str]:
    """The env var NAMES that are unset — ``[]`` when PDF export is configured."""
    url, token = config()
    return [name for name, value in (("RENDERER_URL", url), ("RENDERER_TOKEN", token))
            if not value]


def _fetch_id_token(audience: str) -> str:
    """A Google-signed ID token for ``audience``. The one network call this
    module makes besides the render; tests replace it (no network in tests)."""
    import google.auth.transport.requests
    import google.oauth2.id_token

    return google.oauth2.id_token.fetch_id_token(
        google.auth.transport.requests.Request(), audience)


def identity_token(audience: str) -> str:
    """The ID token, or :class:`RendererError` 503 — never an empty string, and
    never a request sent without one."""
    try:
        token = _fetch_id_token(audience)
    except Exception as exc:  # DefaultCredentialsError, RefreshError, TransportError…
        logger.error("pdf renderer: no Google ID token for the renderer's audience "
                     "(RENDERER_URL) - %s", type(exc).__name__)
        raise RendererError(
            503, "PDF export is unavailable: this server could not obtain a Google identity "
                 f"token to call the PDF renderer ({type(exc).__name__}). On Cloud Run this "
                 "is the service account's identity; locally it needs service-account "
                 "credentials. Nothing was sent to the renderer.") from None
    token = (token or "").strip()
    if not token:
        logger.error("pdf renderer: the identity provider returned an empty ID token")
        raise RendererError(
            503, "PDF export is unavailable: the identity provider returned an empty token "
                 "for the PDF renderer. Nothing was sent to the renderer.")
    return token


def render_pdf(html: str, *, label: str, html_hint: str = "",
               backoff_seconds: float | None = None) -> tuple[bytes, str]:
    """The HTML rendered to PDF bytes. Returns ``(pdf, blocked_subresources)``.

    ``label`` identifies the document in logs (a run id); ``html_hint`` is
    appended to the not-configured answer so the user learns where the same
    document is available without the renderer. Raises :class:`RendererError`
    on every failure path.
    """
    url, token = config()
    missing = missing_config()
    if missing:
        raise RendererError(
            503, "PDF rendering is not configured on this service: "
                 + " and ".join(missing)
                 + (" is unset." if len(missing) == 1 else " are unset.") + html_hint)
    id_token = identity_token(url)
    backoff = BACKOFF_SECONDS if backoff_seconds is None else backoff_seconds
    endpoint = f"{url}/pdf"
    payload = {"v": CONTRACT_VERSION, "html": html}
    headers = {"X-Renderer-Token": token, "Authorization": f"Bearer {id_token}"}
    timeout = httpx.Timeout(read_timeout(), connect=10.0)

    for attempt in range(1, ATTEMPTS + 1):
        unreachable: str | None = None
        try:
            resp = httpx.post(endpoint, json=payload, headers=headers, timeout=timeout)
        except (httpx.ConnectError, httpx.ConnectTimeout, httpx.RemoteProtocolError) as exc:
            unreachable = type(exc).__name__
            logger.warning("pdf renderer %s: attempt %d/%d could not reach the renderer "
                           "(RENDERER_URL) - %s", label, attempt, ATTEMPTS, exc)
            if attempt >= ATTEMPTS:
                raise RendererError(
                    502, f"could not reach the PDF renderer ({unreachable}) after "
                         f"{ATTEMPTS} attempts - RENDERER_URL points at a service this "
                         "deployment cannot connect to.") from None
        except httpx.TimeoutException as exc:
            logger.error("pdf renderer %s: no answer in %.0fs - %s", label, read_timeout(), exc)
            raise RendererError(
                504, "the PDF renderer did not answer within "
                     f"{read_timeout():.0f}s (RENDERER_TIMEOUT_SECONDS). The report was not "
                     "rendered.") from None
        except httpx.HTTPError as exc:
            logger.error("pdf renderer %s: transport failure - %s", label, exc)
            raise RendererError(
                502, f"the PDF renderer call failed ({type(exc).__name__}).") from None

        if unreachable is not None:
            time.sleep(backoff * attempt)
            continue

        if resp.status_code == 200:
            body = resp.content
            if not body.startswith(b"%PDF"):
                logger.error("pdf renderer %s: 200 with %d bytes that are not a PDF",
                             label, len(body))
                raise RendererError(
                    502, "the PDF renderer answered 200 with something that is not a PDF; "
                         "nothing was rendered.")
            return body, resp.headers.get("x-blocked-subresources", "0")

        if resp.status_code == 401:
            logger.error("pdf renderer %s: the renderer rejected this service's "
                         "RENDERER_TOKEN", label)
            raise RendererError(
                502, "the PDF renderer rejected this service's credentials - the "
                     "RENDERER_TOKEN here does not match the renderer's.")
        if resp.status_code == 403:
            # Google's front end: the ID token was valid but this identity does
            # not hold run.invoker on the renderer (or the audience is wrong).
            logger.error("pdf renderer %s: 403 - the renderer refused this service's "
                         "Google identity (run.invoker / audience = RENDERER_URL)", label)
            raise RendererError(
                502, "the PDF renderer refused the backend's identity (403): this service's "
                     "account is not allowed to invoke it, or RENDERER_URL is not the "
                     "renderer's own URL. The report was not rendered.")
        if resp.status_code == 503:
            logger.error("pdf renderer %s: 503 (its own RENDERER_TOKEN unset, or it is "
                         "unavailable)", label)
            raise RendererError(
                503, "the PDF renderer is unavailable: it answered 503, which is what it "
                     "returns when its own RENDERER_TOKEN is unset.")
        if resp.status_code in RETRY_STATUS and attempt < ATTEMPTS:
            logger.warning("pdf renderer %s: answered %d on attempt %d/%d",
                           label, resp.status_code, attempt, ATTEMPTS)
            time.sleep(backoff * attempt)
            continue

        logger.error("pdf renderer %s: answered %d", label, resp.status_code)
        raise RendererError(
            502, f"the PDF renderer answered {resp.status_code}; the report was not rendered.")

    raise RendererError(502, "the PDF renderer could not be reached.")
