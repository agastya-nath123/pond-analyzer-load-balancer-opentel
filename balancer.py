"""
Load balancer for the Pond Planning API.

Sits in front of two (or more) copies of the backend and sends each request
to one of them. While it runs you can choose how users are spread across the
backends, or send everyone to one of them, from a small control page at
/lb/ or with the admin API.

Routing modes
    sticky             each user stays on one backend (a cookie remembers
                       which); new users go to the less busy one. The
                       default, because /findCatchment is fastest on the
                       backend that already analysed the same file.
    round_robin        requests alternate between backends.
    least_connections  each request goes to the backend with the fewest
                       requests in progress.
    <backend name>     everyone goes to that backend, for example "a". The
                       other backends are used only while it is down.

Health
    Every backend is checked every few seconds (GET / by default). After two
    failed checks it is taken out of rotation, and after two passed checks it
    is put back. A request that cannot connect to a backend is retried on the
    next one, which is safe here because the analysis endpoints have no side
    effects.

Run (one process: the routing state lives in memory)
    LB_BACKENDS="a=http://127.0.0.1:8001,b=http://127.0.0.1:8002" \
    uvicorn balancer:app --host 127.0.0.1 --port 8000
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import os
import secrets
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
from opentelemetry import metrics, trace
from opentelemetry.instrumentation.utils import suppress_instrumentation
from opentelemetry.metrics import Observation
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from starlette.routing import Route

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s balancer: %(message)s")
log = logging.getLogger("balancer")
# httpx logs every request at INFO, which would include each health check.
logging.getLogger("httpx").setLevel(logging.WARNING)

import telemetry  # noqa: E402

VERSION = "1.1.0"

# Off unless OTEL_EXPORTER_OTLP_ENDPOINT or OTEL_TRACES_EXPORTER is set; see
# telemetry.py.
TELEMETRY = telemetry.setup("pond-load-balancer", VERSION)


# ---------------------------------------------------------------------------
# Settings (environment variables)
# ---------------------------------------------------------------------------

STRATEGIES = ("sticky", "round_robin", "least_connections")


def _parse_backends(text: str):
    """'a=http://h:8001,b=http://h:8002' or 'http://h:8001,http://h:8002'."""
    backends = []
    for i, item in enumerate(part.strip() for part in text.split(",") if part.strip()):
        name, sep, url = item.partition("=")
        if not sep:
            name, url = chr(ord("a") + i), item
        name, url = name.strip().lower(), url.strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"Backend URL must start with http:// or https://: {url!r}")
        if name in STRATEGIES:
            raise ValueError(f"A backend can't be called {name!r}; that is a routing mode.")
        backends.append((name, url))
    if not backends:
        raise ValueError("LB_BACKENDS lists no backends.")
    if len({name for name, _ in backends}) != len(backends):
        raise ValueError("Backend names in LB_BACKENDS must be different.")
    return backends


BACKENDS = _parse_backends(os.environ.get("LB_BACKENDS", "a=http://127.0.0.1:8001,b=http://127.0.0.1:8002"))
START_MODE = os.environ.get("LB_MODE", "sticky").strip().lower()
ADMIN_TOKEN = os.environ.get("LB_ADMIN_TOKEN", "")

HEALTH_PATH = os.environ.get("LB_HEALTH_PATH", "/")
HEALTH_INTERVAL_S = float(os.environ.get("LB_HEALTH_INTERVAL", "5"))
HEALTH_TIMEOUT_S = 2.0
FALL = 2   # failed checks in a row before a backend is taken out
RISE = 2   # passed checks in a row before it is put back

CONNECT_TIMEOUT_S = 3.0
# The frontend waits up to 180 s for an analysis; the balancer waits as long.
READ_TIMEOUT_S = float(os.environ.get("LB_TIMEOUT", "180"))
# Slightly above the backend's own 100 MB upload limit, so the backend's
# clearer message is what users normally see.
MAX_BODY_BYTES = int(float(os.environ.get("LB_MAX_BODY_MB", "110")) * 1024 * 1024)

COOKIE = "lb_backend"

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "transfer-encoding", "upgrade",
}


# ---------------------------------------------------------------------------
# Backends and routing
# ---------------------------------------------------------------------------

@dataclass
class Backend:
    name: str
    url: str
    enabled: bool = True        # set by the operator
    healthy: bool = True        # set by health checks and failed connections
    active: int = 0             # requests in progress
    served: int = 0
    failed: int = 0
    passes: int = 0             # consecutive passed checks
    fails: int = 0              # consecutive failed checks
    latency_ms: float | None = None
    last_error: str = ""
    checked_at: float = 0.0

    @property
    def available(self) -> bool:
        return self.enabled and self.healthy

    def mark_down(self, reason: str) -> None:
        if self.healthy:
            log.warning("backend %s is down: %s", self.name, reason)
        self.healthy = False
        self.fails = max(self.fails, FALL)
        self.passes = 0
        self.last_error = reason

    def record(self, elapsed_ms: float) -> None:
        self.served += 1
        # Smoothed response time; recent requests count most.
        self.latency_ms = elapsed_ms if self.latency_ms is None else 0.8 * self.latency_ms + 0.2 * elapsed_ms

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "url": self.url,
            "enabled": self.enabled,
            "healthy": self.healthy,
            "available": self.available,
            "in_progress": self.active,
            "served": self.served,
            "failed": self.failed,
            "avg_response_ms": round(self.latency_ms) if self.latency_ms is not None else None,
            "last_error": self.last_error,
            "checked_seconds_ago": round(time.time() - self.checked_at, 1) if self.checked_at else None,
        }


class Pool:
    def __init__(self, backends, mode: str):
        self.backends = [Backend(name, url) for name, url in backends]
        self.by_name = {b.name: b for b in self.backends}
        self._turn = 0
        self.mode = "sticky"
        self.set_mode(mode)

    @property
    def modes(self):
        return list(STRATEGIES) + [b.name for b in self.backends]

    def set_mode(self, mode: str) -> None:
        mode = mode.strip().lower()
        if mode not in self.modes:
            raise ValueError(f"Unknown mode {mode!r}. Choose one of: {', '.join(self.modes)}.")
        if mode != self.mode:
            log.info("routing mode: %s -> %s", self.mode, mode)
        self.mode = mode

    def _next_turn(self) -> int:
        self._turn += 1
        return self._turn

    def _least_busy(self, backends):
        # Ties go to whoever's turn it is, so idle backends share the load.
        turn = self._next_turn()
        n = len(self.backends)
        position = {b.name: i for i, b in enumerate(self.backends)}
        return sorted(backends, key=lambda b: (b.active, (position[b.name] - turn) % n))

    def candidates(self, request: Request):
        """Backends to try for this request, best first."""
        up = [b for b in self.backends if b.available]
        if not up:
            return []

        if self.mode in self.by_name:
            chosen = self.by_name[self.mode]
            others = self._least_busy([b for b in up if b is not chosen])
            return ([chosen] if chosen.available else []) + others

        if self.mode == "round_robin":
            k = self._next_turn() % len(up)
            return up[k:] + up[:k]

        if self.mode == "least_connections":
            return self._least_busy(up)

        # sticky
        remembered = self.by_name.get(request.cookies.get(COOKIE, ""))
        if remembered is not None and remembered.available:
            return [remembered] + self._least_busy([b for b in up if b is not remembered])
        return self._least_busy(up)


pool = Pool(BACKENDS, START_MODE)
client: httpx.AsyncClient | None = None


# ---------------------------------------------------------------------------
# Metrics (do nothing unless telemetry is on)
# ---------------------------------------------------------------------------

_meter = metrics.get_meter("pond.load_balancer", VERSION)
_backend_duration = _meter.create_histogram(
    "lb.backend.request.duration", unit="s",
    description="Time a backend took to answer a forwarded request, by backend and status code")
_failovers = _meter.create_counter(
    "lb.failovers", unit="{request}",
    description="Requests resent to another backend because this one could not be reached")
_refused = _meter.create_counter(
    "lb.requests.refused", unit="{request}",
    description="Requests answered 503 because no backend was available")


def _observe_available(options):
    for b in pool.backends:
        yield Observation(1 if b.available else 0, {"lb.backend": b.name})


def _observe_in_progress(options):
    for b in pool.backends:
        yield Observation(b.active, {"lb.backend": b.name})


_meter.create_observable_gauge(
    "lb.backend.available", callbacks=[_observe_available], unit="1",
    description="1 while a backend is in rotation, 0 while it is down or out of service")
_meter.create_observable_gauge(
    "lb.backend.requests.in_progress", callbacks=[_observe_in_progress], unit="{request}",
    description="Requests a backend is working on right now")


# ---------------------------------------------------------------------------
# Health checks
# ---------------------------------------------------------------------------

async def _check(backend: Backend) -> None:
    try:
        # Not traced: a check every few seconds would bury the real requests.
        with suppress_instrumentation():
            response = await client.get(backend.url + HEALTH_PATH, timeout=HEALTH_TIMEOUT_S)
        ok = response.status_code == 200
        error = "" if ok else f"health check returned HTTP {response.status_code}"
    except httpx.HTTPError as exc:
        ok, error = False, f"health check failed ({type(exc).__name__})"

    backend.checked_at = time.time()
    if ok:
        backend.passes += 1
        backend.fails = 0
        if not backend.healthy and backend.passes >= RISE:
            backend.healthy = True
            backend.last_error = ""
            log.info("backend %s is back up", backend.name)
    else:
        backend.fails += 1
        backend.passes = 0
        backend.last_error = error
        if backend.healthy and backend.fails >= FALL:
            backend.healthy = False
            log.warning("backend %s is down: %s", backend.name, error)


async def _health_loop() -> None:
    while True:
        await asyncio.gather(*(_check(b) for b in pool.backends))
        await asyncio.sleep(HEALTH_INTERVAL_S)


@asynccontextmanager
async def lifespan(app):
    global client
    # trust_env=False: talk to the backends directly, never through a proxy
    # configured in the environment.
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=CONNECT_TIMEOUT_S, read=READ_TIMEOUT_S, write=60.0, pool=10.0),
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=20),
        trust_env=False,
    )
    if TELEMETRY:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        # A client span for each forwarded request, and the W3C traceparent
        # header, so the backend's spans join the same trace.
        HTTPXClientInstrumentor.instrument_client(client)
    task = asyncio.create_task(_health_loop())
    log.info("routing to %s in mode %s", ", ".join(f"{b.name}={b.url}" for b in pool.backends), pool.mode)
    try:
        yield
    finally:
        task.cancel()
        await client.aclose()


# ---------------------------------------------------------------------------
# Proxy
# ---------------------------------------------------------------------------

def _error(status: int, detail: str, request_id: str) -> JSONResponse:
    return JSONResponse({"detail": detail}, status_code=status, headers={"x-request-id": request_id})


async def _read_body(request: Request) -> bytes | None:
    """The whole request body, or None if it is over the size limit."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > MAX_BODY_BYTES:
        return None
    body = bytearray()
    async for chunk in request.stream():
        body += chunk
        if len(body) > MAX_BODY_BYTES:
            return None
    return bytes(body)


def _forward_headers(request: Request, request_id: str):
    # Headers named in Connection are hop-by-hop too.
    named = {h.strip().lower() for h in request.headers.get("connection", "").split(",") if h.strip()}
    skip = HOP_BY_HOP | named | {"host", "content-length"}
    headers = [(k, v) for k, v in request.headers.items() if k.lower() not in skip]
    client_ip = request.client.host if request.client else ""
    earlier = request.headers.get("x-forwarded-for")
    headers += [
        ("x-forwarded-for", f"{earlier}, {client_ip}" if earlier else client_ip),
        ("x-forwarded-proto", request.url.scheme),
        ("x-forwarded-host", request.headers.get("host", "")),
    ]
    if "x-request-id" not in request.headers:
        headers.append(("x-request-id", request_id))
    return headers


async def proxy(request: Request) -> Response:
    request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]

    body = await _read_body(request)
    if body is None:
        return _error(413, f"The upload is larger than {MAX_BODY_BYTES // (1024 * 1024)} MB.", request_id)

    # The server span the OpenTelemetry middleware opened for this request
    # (a do-nothing span when telemetry is off). Routing decisions go on it.
    span = trace.get_current_span()
    context = span.get_span_context()
    trace_id = format(context.trace_id, "032x") if context.is_valid else ""
    span.set_attribute("lb.mode", pool.mode)

    candidates = pool.candidates(request)
    if not candidates:
        _refused.add(1)
        span.set_attribute("lb.outcome", "no_backend")
        return _error(503, "No backend is available right now. Check that the backend servers are running.", request_id)

    target_path = request.url.path + (f"?{request.url.query}" if request.url.query else "")
    headers = _forward_headers(request, request_id)
    errors = []

    def failed(backend, reason: str, attempt: int) -> None:
        backend.failed += 1
        backend.mark_down(reason)
        errors.append(f"{backend.name}: {reason}")
        span.add_event("lb.backend_failed", {"lb.backend": backend.name, "error.type": reason})
        if attempt < len(candidates):
            _failovers.add(1, {"lb.backend": backend.name})

    for attempt, backend in enumerate(candidates, start=1):
        backend.active += 1
        started = time.perf_counter()
        try:
            upstream = await client.request(request.method, backend.url + target_path, content=body, headers=headers)
        except httpx.TimeoutException as exc:
            if isinstance(exc, httpx.ConnectTimeout):
                failed(backend, "ConnectTimeout", attempt)
                continue
            # The backend accepted the request but is still working on it;
            # sending it elsewhere would only double the load.
            backend.failed += 1
            span.set_attributes({"lb.backend": backend.name, "lb.attempts": attempt, "lb.outcome": "timeout"})
            return _error(504, f"Backend {backend.name} did not answer within {READ_TIMEOUT_S:.0f} s.", request_id)
        except httpx.HTTPError as exc:
            # Couldn't connect, or the connection broke. The analysis has no
            # side effects, so trying the next backend is safe.
            failed(backend, type(exc).__name__, attempt)
            continue
        finally:
            backend.active -= 1

        elapsed_ms = (time.perf_counter() - started) * 1000
        backend.record(elapsed_ms)
        span.set_attributes({"lb.backend": backend.name, "lb.attempts": attempt, "lb.outcome": "forwarded"})
        _backend_duration.record(elapsed_ms / 1000, {"lb.backend": backend.name,
                                                     "http.response.status_code": upstream.status_code})
        log.info("%s %s -> %s %d %.0f ms [%s]%s", request.method, request.url.path, backend.name,
                 upstream.status_code, elapsed_ms, request_id, f" trace {trace_id}" if trace_id else "")

        response = Response(content=upstream.content, status_code=upstream.status_code)
        drop = HOP_BY_HOP | {"content-length", "content-encoding"}
        response.raw_headers += [
            (k.encode("latin-1"), v.encode("latin-1"))
            for k, v in upstream.headers.multi_items()
            if k.lower() not in drop
        ]
        response.raw_headers += [(b"x-backend", backend.name.encode()), (b"x-request-id", request_id.encode())]
        if trace_id:
            # Paste this into Jaeger or Grafana to find the request's trace.
            response.raw_headers.append((b"x-trace-id", trace_id.encode()))
        if pool.mode == "sticky" and request.cookies.get(COOKIE) != backend.name:
            response.set_cookie(COOKIE, backend.name, httponly=True, samesite="lax", path="/")
        return response

    span.set_attributes({"lb.attempts": len(candidates), "lb.outcome": "all_failed"})
    return _error(502, "Every backend failed to answer: " + "; ".join(errors) + ".", request_id)


# ---------------------------------------------------------------------------
# Admin API and control page
# ---------------------------------------------------------------------------

def _authorized(request: Request) -> bool:
    if ADMIN_TOKEN:
        return secrets.compare_digest(request.headers.get("x-admin-token", ""), ADMIN_TOKEN)
    # Without a token, only this computer may manage routing.
    try:
        return request.client is not None and ipaddress.ip_address(request.client.host).is_loopback
    except ValueError:
        return False


def _denied() -> JSONResponse:
    if ADMIN_TOKEN:
        return JSONResponse({"detail": "Enter the admin token (X-Admin-Token header) to manage routing."}, status_code=401)
    return JSONResponse({"detail": "Routing can only be managed from this computer, or set LB_ADMIN_TOKEN."}, status_code=403)


def _status() -> dict:
    return {
        "mode": pool.mode,
        "modes": pool.modes,
        "backends": [b.as_dict() for b in pool.backends],
        "admin_token_required": bool(ADMIN_TOKEN),
    }


async def status(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _denied()
    return JSONResponse(_status())


async def set_mode(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _denied()
    previous = pool.mode
    try:
        payload = await request.json()
        pool.set_mode(str(payload["mode"]))
        trace.get_current_span().set_attributes({"lb.mode.previous": previous, "lb.mode": pool.mode})
    except (ValueError, KeyError, TypeError) as exc:
        message = str(exc) if isinstance(exc, ValueError) and "mode" in str(exc) else 'Send JSON like {"mode": "sticky"}.'
        return JSONResponse({"detail": message}, status_code=400)
    return JSONResponse(_status())


async def set_backend(request: Request) -> JSONResponse:
    if not _authorized(request):
        return _denied()
    backend = pool.by_name.get(request.path_params["name"].lower())
    if backend is None:
        return JSONResponse({"detail": f"No backend named {request.path_params['name']!r}."}, status_code=404)
    try:
        payload = await request.json()
        enabled = payload["enabled"]
        if not isinstance(enabled, bool):
            raise TypeError
    except (ValueError, KeyError, TypeError):
        return JSONResponse({"detail": 'Send JSON like {"enabled": false}.'}, status_code=400)
    if backend.enabled != enabled:
        log.info("backend %s %s by operator", backend.name, "put back in service" if enabled else "taken out of service")
    backend.enabled = enabled
    trace.get_current_span().set_attributes({"lb.backend": backend.name, "lb.backend.enabled": enabled})
    return JSONResponse(_status())


async def health(request: Request) -> JSONResponse:
    available = sum(b.available for b in pool.backends)
    return JSONResponse({"ok": available > 0, "available_backends": available},
                        status_code=200 if available else 503)


CONTROL_PAGE = (Path(__file__).with_name("control.html")).read_text(encoding="utf-8")


async def control_page(request: Request) -> HTMLResponse:
    return HTMLResponse(CONTROL_PAGE)


async def control_page_redirect(request: Request) -> RedirectResponse:
    # Relative, so it also works behind a prefix such as the frontend's /api.
    return RedirectResponse("lb/", status_code=307)


ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]

app = Starlette(
    routes=[
        Route("/lb", control_page_redirect, methods=["GET"]),
        Route("/lb/", control_page, methods=["GET"]),
        Route("/lb/status", status, methods=["GET"]),
        Route("/lb/mode", set_mode, methods=["POST"]),
        Route("/lb/backends/{name}", set_backend, methods=["POST"]),
        Route("/lb/health", health, methods=["GET"]),
        Route("/{path:path}", proxy, methods=ALL_METHODS),
    ],
    lifespan=lifespan,
)

if TELEMETRY:
    from opentelemetry.instrumentation.asgi import OpenTelemetryMiddleware

    # A server span for every request, named like "POST /analyzeContour".
    # The control page's status polling and the health endpoint are left out,
    # and so are the receive/send spans for each chunk of an upload.
    app.add_middleware(
        OpenTelemetryMiddleware,
        excluded_urls=r"/lb/(status|health)$",
        exclude_spans=["receive", "send"],
    )
    # Must stay the outermost layer; see FreshTraceContext in telemetry.py.
    app = telemetry.FreshTraceContext(app)
