"""Password gate for the HISTORIA UI.

A password is chosen the first time the app is opened (on this computer only). After that every
``/api`` call except ``/api/auth/*`` needs a signed session cookie, so nothing can be operated
(or read) without logging in. The password is stored as a salted scrypt hash inside the
encrypted local settings table; sessions are stateless HMAC tokens, so restarting the app does
not log you out, while changing the password invalidates every existing session.
"""
import base64
import hashlib
import hmac
import secrets
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ghm import remote_access

HASH_SETTING = "ui_password_hash"
KEY_SETTING = "ui_session_key"
COOKIE = "historia_session"
SESSION_DAYS = 7
MIN_LENGTH = 8
MAX_LENGTH = 200
FREE_ATTEMPTS = 5
MAX_LOCK = 900
_SCRYPT = {"n": 2 ** 14, "r": 8, "p": 1}


def _hash(password: str, salt: bytes) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, dklen=32, maxmem=64 * 1024 * 1024, **_SCRYPT)


def make_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(_hash(password, salt)).decode()


def check_hash(password: str, stored: str) -> bool:
    try:
        scheme, salt, digest = stored.split("$")
        if scheme != "scrypt":
            return False
        return hmac.compare_digest(_hash(password, base64.b64decode(salt)), base64.b64decode(digest))
    except (ValueError, TypeError):
        return False


class Credentials(BaseModel):
    password: str = Field(max_length=MAX_LENGTH)


class PasswordChange(BaseModel):
    current: str = Field(max_length=MAX_LENGTH)
    new: str = Field(max_length=MAX_LENGTH)


class Auth:
    def __init__(self, service, enabled: bool = True):
        self.service, self.enabled = service, enabled
        self._lock = threading.Lock()
        self._fails, self._locked_until = 0, 0.0
        self._key: bytes | None = None

    # -- state -------------------------------------------------------------------------
    def configured(self) -> bool:
        return bool(self.service.setting(HASH_SETTING))

    def _session_key(self) -> bytes:
        if self._key is None:
            value = self.service.setting(KEY_SETTING)
            if not value:
                value = secrets.token_urlsafe(48)
                self.service.save_setting(KEY_SETTING, value)
            self._key = value.encode()
        return self._key

    def set_password(self, password: str) -> None:
        if not MIN_LENGTH <= len(password) <= MAX_LENGTH:
            raise HTTPException(422, f"Mật khẩu cần từ {MIN_LENGTH} ký tự trở lên.")
        self.service.save_setting(HASH_SETTING, make_hash(password))
        # New key = every previously issued session stops working.
        self._key = None
        self.service.save_setting(KEY_SETTING, secrets.token_urlsafe(48))

    def clear(self) -> None:
        self.service.save_setting(HASH_SETTING, "")
        self._key = None
        self.service.save_setting(KEY_SETTING, secrets.token_urlsafe(48))

    # -- sessions ----------------------------------------------------------------------
    def issue(self) -> str:
        body = f"{int(time.time()) + SESSION_DAYS * 86400}.{secrets.token_urlsafe(12)}"
        return body + "." + hmac.new(self._session_key(), body.encode(), "sha256").hexdigest()

    def valid(self, token: str | None) -> bool:
        if not token or not self.configured():
            return False
        try:
            expires, nonce, signature = token.split(".")
            expected = hmac.new(self._session_key(), f"{expires}.{nonce}".encode(), "sha256").hexdigest()
            return hmac.compare_digest(signature, expected) and int(expires) > time.time()
        except ValueError:
            return False

    def authenticated(self, request: Request) -> bool:
        return not self.enabled or self.valid(request.cookies.get(COOKIE))

    def deny(self) -> JSONResponse:
        code = "login_required" if self.configured() else "setup_required"
        detail = "Đăng nhập để dùng HISTORIA." if code == "login_required" else "Hãy tạo mật khẩu để bắt đầu dùng HISTORIA."
        return JSONResponse({"detail": detail, "code": code}, status_code=401)

    # -- brute-force protection --------------------------------------------------------
    def wait_seconds(self) -> int:
        with self._lock:
            return max(0, int(self._locked_until - time.time() + 0.999))

    def failed(self) -> None:
        with self._lock:
            self._fails += 1
            if self._fails >= FREE_ATTEMPTS:
                self._locked_until = time.time() + min(MAX_LOCK, 5 * 2 ** (self._fails - FREE_ATTEMPTS))

    def succeeded(self) -> None:
        with self._lock:
            self._fails, self._locked_until = 0, 0.0

    def set_cookie(self, request: Request, response: JSONResponse) -> None:
        secure = request.url.scheme == "https" or request.headers.get("x-forwarded-proto") == "https"
        response.set_cookie(COOKIE, self.issue(), max_age=SESSION_DAYS * 86400, httponly=True,
                            secure=secure, samesite="strict", path="/")


def router(auth: Auth) -> APIRouter:
    api = APIRouter(prefix="/api/auth")

    def status(request: Request) -> dict:
        return {"enabled": auth.enabled, "configured": auth.configured(), "authenticated": auth.authenticated(request),
                "min_length": MIN_LENGTH}

    def throttled():
        wait = auth.wait_seconds()
        if wait:
            raise HTTPException(429, f"Sai mật khẩu quá nhiều lần. Thử lại sau {wait} giây.")

    @api.get("/status")
    def get_status(request: Request):
        return status(request)

    @api.post("/setup")
    def setup(payload: Credentials, request: Request):
        remote_access.require_local(request)
        if auth.configured():
            raise HTTPException(409, "Đã có mật khẩu. Hãy đăng nhập.")
        auth.set_password(payload.password)
        response = JSONResponse({"enabled": True, "configured": True, "authenticated": True, "min_length": MIN_LENGTH})
        auth.set_cookie(request, response)
        return response

    @api.post("/login")
    def login(payload: Credentials, request: Request):
        stored = auth.service.setting(HASH_SETTING)
        if not stored:
            raise HTTPException(409, "Chưa có mật khẩu. Hãy tạo mật khẩu trước.")
        throttled()
        if not check_hash(payload.password, stored):
            auth.failed()
            raise HTTPException(401, "Mật khẩu không đúng.")
        auth.succeeded()
        response = JSONResponse({"enabled": True, "configured": True, "authenticated": True, "min_length": MIN_LENGTH})
        auth.set_cookie(request, response)
        return response

    @api.post("/logout")
    def logout():
        response = JSONResponse({"authenticated": False})
        response.delete_cookie(COOKIE, path="/")
        return response

    @api.post("/change-password")
    def change_password(payload: PasswordChange, request: Request):
        if not auth.authenticated(request):
            return auth.deny()
        stored = auth.service.setting(HASH_SETTING)
        throttled()
        if not stored or not check_hash(payload.current, stored):
            auth.failed()
            raise HTTPException(401, "Mật khẩu hiện tại không đúng.")
        auth.succeeded()
        auth.set_password(payload.new)
        response = JSONResponse({"enabled": True, "configured": True, "authenticated": True, "min_length": MIN_LENGTH})
        auth.set_cookie(request, response)
        return response

    return api
