"""Access control for using the local UI through a tunnel (cloudflared, ngrok, ...).

Requests made directly on this computer (loopback, no proxy headers) work as before.
Anything that arrives through a proxy must carry an access cookie, obtained once by
opening ``<tunnel address>/?access=<token>``. The token is stored encrypted in the
local settings table and can be rotated from the UI, which invalidates old links.
"""
import hmac
import secrets
from urllib.parse import urlencode

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

SETTING = "remote_access_token"
COOKIE = "historia_access"
QUERY = "access"
LOOPBACK = {"127.0.0.1", "::1", "localhost", "testclient"}
# Headers that tunnels and reverse proxies add. Their presence means the browser is elsewhere.
FORWARDED = ("cf-connecting-ip", "cf-ray", "x-forwarded-for", "x-forwarded-host", "x-forwarded-proto",
             "forwarded", "x-real-ip")
COOKIE_DAYS = 30


def is_remote(request) -> bool:
    client = request.client.host if request.client else ""
    return client not in LOOPBACK or any(name in request.headers for name in FORWARDED)


def ensure_token(service) -> str:
    token = service.setting(SETTING)
    if not token:
        token = secrets.token_urlsafe(32)
        service.save_setting(SETTING, token)
    return token


def rotate(service) -> str:
    token = secrets.token_urlsafe(32)
    service.save_setting(SETTING, token)
    return token


def _matches(value: str | None, service) -> bool:
    token = service.setting(SETTING)
    return bool(value and token) and hmac.compare_digest(value.encode(), token.encode())


def has_valid_cookie(request, service) -> bool:
    return _matches(request.cookies.get(COOKIE), service)


def login_requested(request) -> bool:
    return request.method == "GET" and QUERY in request.query_params and not request.url.path.startswith("/api")


def login(request, service):
    if not _matches(request.query_params.get(QUERY), service):
        return denied(request, "Mã truy cập không đúng hoặc đã được đổi.")
    # Drop the token from the address bar (and from history/referrers) right away.
    rest = {k: v for k, v in request.query_params.multi_items() if k != QUERY}
    response = RedirectResponse(request.url.path + ("?" + urlencode(rest) if rest else ""), status_code=303)
    response.set_cookie(COOKIE, request.query_params[QUERY], max_age=COOKIE_DAYS * 86400, httponly=True,
                        secure=True, samesite="lax", path="/")
    return response


def denied(request, reason: str = "Cần mã truy cập để dùng HISTORIA từ xa."):
    if request.url.path.startswith("/api"):
        return JSONResponse({"detail": reason}, status_code=401)
    return HTMLResponse(
        "<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width'>"
        "<title>HISTORIA</title><body style='font-family:system-ui;max-width:32rem;margin:3rem auto;padding:0 1rem'>"
        f"<h1>HISTORIA</h1><p>{reason}</p><p>Trên máy chạy HISTORIA, mở trang <b>Kết nối GPU → Truy cập từ xa</b> "
        "để lấy đường link có mã, rồi mở link đó trên thiết bị này.</p></body>", status_code=401)


def require_local(request) -> None:
    if is_remote(request):
        raise HTTPException(403, "Chỉ xem hoặc đổi mã truy cập ngay trên máy chạy HISTORIA.")
