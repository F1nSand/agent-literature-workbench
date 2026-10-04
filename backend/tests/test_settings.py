from pydantic import SecretStr

from app.core.config import Settings


def test_database_url_is_masked_from_repr_and_string() -> None:
    secret = "postgresql+psycopg://user:never-print-this@localhost/db"
    settings = Settings(database_url=SecretStr(secret))
    assert "never-print-this" not in repr(settings)
    assert "never-print-this" not in str(settings)
    assert settings.database_url.get_secret_value() == secret


def test_database_url_loads_from_environment_without_leaking(monkeypatch) -> None:
    secret = "postgresql+psycopg://user:env-secret@localhost/db"
    monkeypatch.setenv("DATABASE_URL", secret)
    settings = Settings()
    assert settings.database_url.get_secret_value() == secret
    assert "env-secret" not in repr(settings)


def test_siliconflow_key_loads_from_environment_without_leaking(monkeypatch) -> None:
    secret = "sk-" + "fixture-" + "never-print-this"
    monkeypatch.setenv("SILICONFLOW_API_KEY", secret)
    settings = Settings()
    assert settings.siliconflow_api_key is not None
    assert settings.siliconflow_api_key.get_secret_value() == secret
    assert "never-print-this" not in repr(settings)
    assert "never-print-this" not in str(settings)
