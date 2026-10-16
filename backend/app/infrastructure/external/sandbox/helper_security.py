"""Fixed security options for short-lived dataset and preview helpers."""
from app.core.config import get_settings


def helper_security_options() -> list[str]:
    options = ["no-new-privileges:true"]
    if getattr(get_settings(), "docker_apparmor_stack_default", False) is True:
        # A stack transition retains the daemon's existing AppArmor profile
        # while adding docker-default, including under no-new-privileges.
        options.append("apparmor=&docker-default")
    return options
