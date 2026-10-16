"""Operator opt-in cannot disable NNP or select an arbitrary AppArmor profile."""
import pytest
from pydantic import ValidationError

from app.core.config import Settings
from app.infrastructure.external.sandbox import helper_security


@pytest.mark.parametrize("value, expected", [
    (None, ["no-new-privileges:true"]),
    ("false", ["no-new-privileges:true"]),
    ("true", ["no-new-privileges:true", "apparmor=&docker-default"]),
])
def test_environment_opt_in_preserves_no_new_privileges(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("DOCKER_APPARMOR_STACK_DEFAULT", raising=False)
    else:
        monkeypatch.setenv("DOCKER_APPARMOR_STACK_DEFAULT", value)
    settings = Settings(_env_file=None)
    monkeypatch.setattr(helper_security, "get_settings", lambda: settings)
    assert helper_security.helper_security_options() == expected


def test_operator_cannot_supply_an_unconfined_profile(monkeypatch):
    monkeypatch.setenv("DOCKER_APPARMOR_STACK_DEFAULT", "apparmor=unconfined")
    with pytest.raises(ValidationError):
        Settings(_env_file=None)
