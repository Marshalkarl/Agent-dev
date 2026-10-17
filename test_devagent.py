import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import devagent  # noqa: E402


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setattr(devagent, "ROOT", tmp_path.resolve())
    return tmp_path.resolve()


def test_safe_path_ok(repo):
    assert devagent.safe_path("src/app.py") == repo / "src" / "app.py"


@pytest.mark.parametrize("bad", ["../secret.txt", ".git/config", ".env", ".env.local", "key.pem", "a/id_rsa"])
def test_safe_path_refuse(repo, bad):
    with pytest.raises(ValueError):
        devagent.safe_path(bad)


def test_env_example_autorise(repo):
    assert devagent.safe_path(".env.example").name == ".env.example"


def test_truncate():
    assert devagent.truncate("") == "(aucune sortie)"
    long = "x" * (devagent.MAX_OUT + 10)
    assert "tronqué" in devagent.truncate(long)


def test_edit_file_refuse_sans_confirmation(repo, monkeypatch):
    (repo / "a.txt").write_text("bonjour", encoding="utf-8")
    monkeypatch.setattr(devagent, "confirm", lambda q: False)
    assert "Refusé" in devagent.edit_file("a.txt", "bonjour", "salut")
    assert (repo / "a.txt").read_text(encoding="utf-8") == "bonjour"


def test_edit_file_applique(repo, monkeypatch):
    (repo / "a.txt").write_text("bonjour", encoding="utf-8")
    monkeypatch.setattr(devagent, "confirm", lambda q: True)
    devagent.edit_file("a.txt", "bonjour", "salut")
    assert (repo / "a.txt").read_text(encoding="utf-8") == "salut"


def test_edit_file_extrait_ambigu(repo, monkeypatch):
    (repo / "a.txt").write_text("x x", encoding="utf-8")
    monkeypatch.setattr(devagent, "confirm", lambda q: True)
    assert "2 fois" in devagent.edit_file("a.txt", "x", "y")


def test_detect_test_cmd(repo, monkeypatch):
    monkeypatch.delenv("DEVAGENT_TEST_CMD", raising=False)
    assert devagent.detect_test_cmd() is None
    (repo / "artisan").write_text("", encoding="utf-8")
    assert devagent.detect_test_cmd() == "php artisan test"


def test_confirm_refuse_hors_terminal(monkeypatch):
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    assert devagent.confirm("ok ?") is False


def test_outils_lecture_seule_sont_valides():
    noms = {t["name"] for t in devagent.TOOLS}
    assert devagent.READONLY <= noms
    assert noms == set(devagent.FUNCS)


def test_outils_format_openai():
    tools = devagent.to_openai_tools(devagent.TOOLS)
    assert all(t["type"] == "function" and "parameters" in t["function"] for t in tools)


class FakeResp:
    def __init__(self, lines):
        self.lines = [l.encode() for l in lines]

    def __iter__(self):
        return iter(self.lines)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def test_read_stream_assemble_texte_et_outils():
    import json as j
    ev = lambda d: "data: " + j.dumps(d)
    resp = FakeResp([
        ev({"choices": [{"delta": {"content": "Bon"}}]}),
        ev({"choices": [{"delta": {"content": "jour"}}]}),
        ev({"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "a", "function": {"name": "read_file", "arguments": "{\"pa"}}]}}]}),
        ev({"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "th\":\"x\"}"}}]}}]}),
        "data: [DONE]",
    ])
    text, calls, _ = devagent.read_stream(resp)
    assert text == "Bonjour"
    assert calls == [{"id": "a", "name": "read_file", "arguments": '{"path":"x"}'}]
