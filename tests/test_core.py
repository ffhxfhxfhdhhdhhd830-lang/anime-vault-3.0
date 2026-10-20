from types import SimpleNamespace
import pytest
from anime_bot.config import Settings
from anime_bot.services.core import normalize, STATUSES

def test_config_requires_token_and_owner():
    with pytest.raises(ValueError): Settings(TELEGRAM_BOT_TOKEN="", OWNER_TELEGRAM_ID=0).validate_runtime()

def test_config_accepts_configured_private_bot():
    Settings(TELEGRAM_BOT_TOKEN="token", OWNER_TELEGRAM_ID=1234).validate_runtime()

def test_normalization_is_stable():
    assert normalize("Weathering with You!") == "weathering with you"

def test_status_vocabulary_keeps_unknown_separate():
    assert "unknown" in STATUSES and "completed" in STATUSES and len(STATUSES)==8
