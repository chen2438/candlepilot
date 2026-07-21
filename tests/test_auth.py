from candlepilot.auth import AuthManager, hash_password, verify_password
from pydantic import SecretStr


def test_password_hash_and_signed_session_expiry() -> None:
    encoded = hash_password("correct horse battery staple")
    assert verify_password("correct horse battery staple", encoded) is True
    assert verify_password("wrong password", encoded) is False

    auth = AuthManager(
        enabled=True,
        username="operator",
        password_hash=SecretStr(encoded),
        session_secret=SecretStr("s" * 32),
        session_ttl_seconds=3600,
        cookie_secure=True,
    )
    assert auth.authenticate("operator", "correct horse battery staple", "client") is True
    token = auth.issue_session(now=1000)
    assert auth.validate_session(token, now=1001).username == "operator"
    assert auth.validate_session(token + "tampered", now=1001) is None
    assert auth.validate_session(token, now=4600) is None


def test_failed_login_rate_limit_is_per_client(monkeypatch) -> None:
    clock = [1000.0]
    monkeypatch.setattr("candlepilot.auth.time.monotonic", lambda: clock[0])
    auth = AuthManager(
        enabled=True,
        username="operator",
        password_hash=SecretStr(hash_password("correct horse battery staple")),
        session_secret=SecretStr("s" * 32),
        session_ttl_seconds=3600,
        cookie_secure=False,
    )
    for _ in range(5):
        assert auth.authenticate("operator", "wrong", "attacker") is False
    assert auth.blocked_for("attacker") > 0
    assert auth.blocked_for("different-client") == 0
    assert "different-client" not in auth._failed_logins

    assert auth.authenticate("operator", "wrong", "second-attacker") is False
    clock[0] += 301
    assert auth.blocked_for("new-client") == 0
    assert auth._failed_logins == {}
