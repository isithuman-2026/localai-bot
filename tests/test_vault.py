# tests/test_vault.py
import os
import tempfile
import shutil
from pathlib import Path
import pytest

os.environ.setdefault("CHROMA_PATH", tempfile.mkdtemp())


@pytest.fixture
def vault_dir(tmp_path):
    """Minimal vault with base note and two content notes."""
    (tmp_path / "TheLab").mkdir()
    (tmp_path / "Areas" / "Security").mkdir(parents=True)

    (tmp_path / "TheLab" / "jarvis-triage-base.md").write_text(
        "# Homelab topology\nnode1 runs Docker services. NAS: vault44, alpha60.\n"
    )
    (tmp_path / "TheLab" / "fail2ban.md").write_text(
        "# fail2ban\nBans IPs after 5 failed SSH attempts. Logs in /var/log/fail2ban.log.\n"
    )
    (tmp_path / "Areas" / "Security" / "ssh-hardening.md").write_text(
        "# SSH Hardening\nPasswordAuthentication no. Port 22 blocked externally via UDR.\n"
    )
    return tmp_path


@pytest.fixture(autouse=True)
def patch_vault_path(vault_dir, monkeypatch):
    monkeypatch.setenv("VAULT_PATH", str(vault_dir))
    monkeypatch.setenv("CHROMA_PATH", str(vault_dir / ".chroma"))
    import importlib
    import vault
    importlib.reload(vault)


def test_search_returns_string(vault_dir):
    import vault
    result = vault.search("fail2ban SSH ban spike")
    assert isinstance(result, str)


def test_base_note_always_included(vault_dir):
    import vault
    result = vault.search("unrelated query xyz")
    assert "Homelab topology" in result


def test_relevant_note_retrieved(vault_dir):
    import vault
    result = vault.search("fail2ban banned too many IPs SSH brute force")
    assert "fail2ban" in result.lower()


def test_irrelevant_note_excluded(vault_dir):
    import vault
    result = vault.search("disk full NAS storage volume usage")
    assert "PasswordAuthentication" not in result or result.index("Homelab topology") < result.find("PasswordAuthentication")


def test_empty_vault(tmp_path, monkeypatch):
    import importlib
    import vault
    monkeypatch.setenv("VAULT_PATH", str(tmp_path / "nonexistent"))
    monkeypatch.setenv("CHROMA_PATH", str(tmp_path / ".chroma"))
    importlib.reload(vault)
    result = vault.search("any query")
    assert result == ""


def test_total_output_within_budget(vault_dir):
    import vault
    result = vault.search("SSH fail2ban security hardening")
    assert len(result) <= 2500
