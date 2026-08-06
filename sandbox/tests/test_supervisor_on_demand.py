import re
from pathlib import Path


def _program_section(config: str, name: str) -> str:
    match = re.search(
        rf"\[program:{re.escape(name)}\](.*?)(?=\n\[|\Z)",
        config,
        flags=re.DOTALL,
    )
    assert match, f"missing supervisor program {name}"
    return match.group(1)


def test_only_analysis_api_autostarts_in_sandbox():
    config = (Path(__file__).parent.parent / "supervisord.conf").read_text(
        encoding="utf-8"
    )

    assert "autostart=true" in _program_section(config, "app")
    for name in ("xvfb", "chrome", "socat", "x11vnc", "websockify"):
        assert "autostart=false" in _program_section(config, name)
