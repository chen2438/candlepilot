from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from collections import deque
from dataclasses import dataclass
from typing import Any

from pydantic import SecretStr


PASSWORD_SCHEME = "scrypt"
PASSWORD_N = 2**14
PASSWORD_R = 8
PASSWORD_P = 1
SESSION_COOKIE = "candlepilot_session"
LOGIN_WINDOW_SECONDS = 5 * 60
LOGIN_ATTEMPT_LIMIT = 5


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str) -> str:
    if len(password) < 12:
        raise ValueError("password must contain at least 12 characters")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=PASSWORD_N, r=PASSWORD_R, p=PASSWORD_P
    )
    return f"{PASSWORD_SCHEME}${PASSWORD_N}${PASSWORD_R}${PASSWORD_P}${_encode(salt)}${_encode(digest)}"


def verify_password(password: str, encoded: str) -> bool:
    try:
        scheme, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$", 5)
        if scheme != PASSWORD_SCHEME:
            return False
        expected = _decode(raw_digest)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_decode(raw_salt),
            n=int(raw_n),
            r=int(raw_r),
            p=int(raw_p),
            dklen=len(expected),
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(actual, expected)


def validate_password_hash(encoded: str) -> None:
    try:
        scheme, raw_n, raw_r, raw_p, raw_salt, raw_digest = encoded.split("$", 5)
        valid = (
            scheme == PASSWORD_SCHEME
            and int(raw_n) == PASSWORD_N
            and int(raw_r) == PASSWORD_R
            and int(raw_p) == PASSWORD_P
            and len(_decode(raw_salt)) == 16
            and len(_decode(raw_digest)) == 64
        )
    except (ValueError, TypeError):
        valid = False
    if not valid:
        raise ValueError("CANDLEPILOT_AUTH_PASSWORD_HASH is not a supported scrypt hash")


@dataclass(frozen=True, slots=True)
class AuthIdentity:
    username: str
    expires_at: int


class AuthManager:
    def __init__(
        self,
        *,
        enabled: bool,
        username: str | None,
        password_hash: SecretStr | None,
        session_secret: SecretStr | None,
        session_ttl_seconds: int,
        cookie_secure: bool,
    ) -> None:
        self.enabled = enabled
        self.username = username
        self._password_hash = password_hash.get_secret_value() if password_hash else ""
        self._session_secret = session_secret.get_secret_value().encode() if session_secret else b""
        self.session_ttl_seconds = session_ttl_seconds
        self.cookie_secure = cookie_secure
        self._failed_logins: dict[str, deque[float]] = {}
        self._last_failed_login_cleanup = time.monotonic()

    def _prune_failed_logins(self, current: float) -> None:
        if (
            current >= self._last_failed_login_cleanup
            and current - self._last_failed_login_cleanup < LOGIN_WINDOW_SECONDS
        ):
            return
        cutoff = current - LOGIN_WINDOW_SECONDS
        for client, attempts in list(self._failed_logins.items()):
            while attempts and attempts[0] <= cutoff:
                attempts.popleft()
            if not attempts:
                self._failed_logins.pop(client, None)
        self._last_failed_login_cleanup = current

    def blocked_for(self, client: str, *, now: float | None = None) -> int:
        current = time.monotonic() if now is None else now
        self._prune_failed_logins(current)
        attempts = self._failed_logins.get(client)
        if attempts is None:
            return 0
        while attempts and current - attempts[0] >= LOGIN_WINDOW_SECONDS:
            attempts.popleft()
        if not attempts:
            self._failed_logins.pop(client, None)
            return 0
        if len(attempts) < LOGIN_ATTEMPT_LIMIT:
            return 0
        return max(1, int(LOGIN_WINDOW_SECONDS - (current - attempts[0])))

    def authenticate(self, username: str, password: str, client: str) -> bool:
        if self.blocked_for(client):
            return False
        valid_user = hmac.compare_digest(username, self.username or "")
        valid_password = verify_password(password, self._password_hash)
        if not (valid_user and valid_password):
            self._failed_logins.setdefault(client, deque()).append(time.monotonic())
            return False
        self._failed_logins.pop(client, None)
        return True

    def issue_session(self, *, now: int | None = None) -> str:
        issued_at = int(time.time()) if now is None else now
        payload = _encode(
            json.dumps(
                {"u": self.username, "e": issued_at + self.session_ttl_seconds},
                separators=(",", ":"),
            ).encode()
        )
        signature = hmac.new(
            self._session_secret,
            f"{payload}.{self._password_hash}".encode(),
            hashlib.sha256,
        ).digest()
        return f"{payload}.{_encode(signature)}"

    def validate_session(self, token: str | None, *, now: int | None = None) -> AuthIdentity | None:
        if not self.enabled:
            return AuthIdentity(username=self.username or "local", expires_at=2**63 - 1)
        if not token:
            return None
        try:
            payload, raw_signature = token.split(".", 1)
            expected = hmac.new(
                self._session_secret,
                f"{payload}.{self._password_hash}".encode(),
                hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(_decode(raw_signature), expected):
                return None
            content: dict[str, Any] = json.loads(_decode(payload))
            expires_at = int(content["e"])
            username = str(content["u"])
        except (ValueError, TypeError, KeyError, json.JSONDecodeError):
            return None
        current = int(time.time()) if now is None else now
        if expires_at <= current or username != self.username:
            return None
        return AuthIdentity(username=username, expires_at=expires_at)


if __name__ == "__main__":
    import argparse
    import getpass
    import sys

    parser = argparse.ArgumentParser(description="Generate a CandlePilot password hash")
    parser.add_argument(
        "--password-stdin",
        action="store_true",
        help="read one password line from stdin instead of prompting twice",
    )
    arguments = parser.parse_args()
    if arguments.password_stdin:
        password = sys.stdin.readline().rstrip("\r\n")
    else:
        password = getpass.getpass("New CandlePilot password (minimum 12 characters): ")
        second = getpass.getpass("Repeat password: ")
        if password != second:
            raise SystemExit("passwords do not match")
    print(hash_password(password))
