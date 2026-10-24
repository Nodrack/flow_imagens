import pytest


@pytest.fixture(autouse=True)
def isolated_agentkit_home(tmp_path, monkeypatch):
    """Trava de GPU e banco central do agentkit isolados por teste."""
    home = tmp_path / "agentkit_home"
    monkeypatch.setenv("AGENTKIT_HOME", str(home))
    return home
