import pytest
from pydantic import SecretStr, ValidationError

from app.core.config import Settings


def test_env_lists_and_redacted_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_OWNER_IDS", "101, 102,101")
    monkeypatch.setenv("MAX_OPERATOR_IDS", "303")
    monkeypatch.setenv("MAX_EMPLOYEE_IDS", "202")
    monkeypatch.setenv("MAX_STAFF_TOKEN", "synthetic-sensitive-token")
    settings = Settings(_env_file=None)
    assert settings.max_owner_ids == (101, 102)
    assert settings.max_operator_ids == (303,)
    assert settings.max_employee_ids == (202,)
    assert settings.max_staff_ids == (101, 102, 303, 202)
    assert settings.max_dispatcher_ids == (101, 102, 303)
    assert "synthetic-sensitive-token" not in repr(settings)


@pytest.mark.parametrize("value", ["0", "-1", "1.2", "true", "1,,2", str(2**63), [True]])
def test_invalid_ids_rejected(value: object) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, max_owner_ids=value)


def test_bad_database_url_does_not_expose_credential() -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, database_url=SecretStr("invalid://sensitive-credential"))
    assert "sensitive-credential" not in str(caught.value)


def test_blank_deployment_tokens_disable_auth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAX_STAFF_TOKEN", "")
    monkeypatch.setenv("MAX_OWNER_IDS", "")
    assert Settings(_env_file=None).max_staff_token is None
    assert Settings(_env_file=None).max_owner_ids == ()


@pytest.mark.parametrize("secret", ["short", "x" * 257, "x" * 32 + "\n", "ю" * 32])
def test_invalid_webhook_secret_is_redacted(secret: str) -> None:
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, max_staff_webhook_secret=SecretStr(secret))
    assert secret not in str(caught.value)


def test_bot_namespaces_require_distinct_secrets() -> None:
    with pytest.raises(ValidationError, match="must differ"):
        Settings(
            _env_file=None,
            max_staff_webhook_secret=SecretStr("x" * 32),
            max_observer_webhook_secret=SecretStr("x" * 32),
        )
