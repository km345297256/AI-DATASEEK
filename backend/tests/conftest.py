"""
Pytest configuration and fixtures
"""
import sys
import os
import pytest
import tempfile
from pathlib import Path

# Add the parent directory to Python path so we can import app modules
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.interfaces.schemas import mcp

import requests

# Base URL for API testing
BASE_URL = "http://localhost:8000/api/v1"

@pytest.fixture
def client():
    """Create requests session"""
    session = requests.Session()
    # Don't set default Content-Type to allow multipart/form-data for file uploads
    return session


@pytest.fixture(params=[False, True], ids=["default-profile", "stacked-docker-default"])
def expected_helper_security_options(request, monkeypatch):
    """Exercise real helper Docker requests under both operator configurations."""
    from types import SimpleNamespace
    from app.infrastructure.external.sandbox import helper_security

    monkeypatch.setattr(helper_security, "get_settings", lambda: SimpleNamespace(
        docker_apparmor_stack_default=request.param,
    ))
    return (["no-new-privileges:true", "apparmor=&docker-default"] if request.param
            else ["no-new-privileges:true"])

