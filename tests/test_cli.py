"""
tests/test_cli.py

Tests for the Codebud CLI: subcommand routing, doctor checks, plan display,
and error mapping.
"""

import sys
from unittest.mock import MagicMock, patch

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _good_plan():
    return {
        "status": "ok",
        "plan": [
            {
                "id": "step_0",
                "description": "list files",
                "tool": "command",
                "args": {"cmd": "ls"},
            }
        ],
    }


def _plan_error(msg="LLM returned empty output"):
    return {"status": "plan_error", "error": msg}


# ---------------------------------------------------------------------------
# Doctor checks (unit)
# ---------------------------------------------------------------------------


class TestDoctorChecks:
    def test_python_version_pass(self):
        from agent.cli.doctor import check_python_version

        passed, label, _ = check_python_version()
        assert passed
        assert "Python version" in label

    def test_agent_importable_pass(self):
        from agent.cli.doctor import check_agent_importable

        passed, label, _ = check_agent_importable()
        assert passed
        assert "importable" in label

    def test_ollama_model_not_set(self, monkeypatch):
        from agent.cli.doctor import check_ollama_model_set

        monkeypatch.delenv("OLLAMA_MODEL", raising=False)
        passed, label, hint = check_ollama_model_set()
        assert not passed
        assert "OLLAMA_MODEL" in label
        assert "export" in hint

    def test_ollama_model_set(self, monkeypatch):
        from agent.cli.doctor import check_ollama_model_set

        monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:3b")
        passed, label, _ = check_ollama_model_set()
        assert passed
        assert "qwen2.5-coder:3b" in label

    def test_ollama_reachable_pass(self, monkeypatch):
        import requests

        from agent.cli import doctor

        def fake_get(url, timeout):
            r = MagicMock()
            r.status_code = 200
            return r

        monkeypatch.setattr(requests, "get", fake_get)
        passed, label, _ = doctor.check_ollama_reachable()
        assert passed

    def test_ollama_reachable_fail_connection(self, monkeypatch):
        import requests

        from agent.cli import doctor

        def fake_get(url, timeout):
            raise requests.exceptions.ConnectionError("refused")

        monkeypatch.setattr(requests, "get", fake_get)
        passed, label, hint = doctor.check_ollama_reachable()
        assert not passed
        assert "ollama serve" in hint or "systemctl" in hint

    def test_model_pulled_pass(self, monkeypatch):
        import requests

        from agent.cli import doctor

        monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:3b")

        def fake_get(url, timeout):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"models": [{"name": "qwen2.5-coder:3b"}]}
            return r

        monkeypatch.setattr(requests, "get", fake_get)
        passed, label, _ = doctor.check_model_pulled()
        assert passed

    def test_model_pulled_fail(self, monkeypatch):
        import requests

        from agent.cli import doctor

        monkeypatch.setenv("OLLAMA_MODEL", "missing-model")

        def fake_get(url, timeout):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"models": []}
            return r

        monkeypatch.setattr(requests, "get", fake_get)
        passed, label, hint = doctor.check_model_pulled()
        assert not passed
        assert "ollama pull" in hint

    def test_config_files_pass(self, tmp_path, monkeypatch):
        from agent.cli import doctor

        # Point _REPO_ROOT to a temp dir with valid config files
        config = tmp_path / "config"
        config.mkdir()
        (config / "harmless_commands.json").write_text('["ls", "cat"]')
        (config / "powerful_commands.json").write_text('["rm"]')
        monkeypatch.setattr(doctor, "_REPO_ROOT", tmp_path)

        passed, label, _ = doctor.check_config_files()
        assert passed

    def test_config_files_bad_json(self, tmp_path, monkeypatch):
        from agent.cli import doctor

        config = tmp_path / "config"
        config.mkdir()
        (config / "harmless_commands.json").write_text("not-json{{{")
        (config / "powerful_commands.json").write_text("[]")
        monkeypatch.setattr(doctor, "_REPO_ROOT", tmp_path)

        passed, label, hint = doctor.check_config_files()
        assert not passed
        assert "JSON" in label or "JSON" in hint

    def test_run_doctor_all_pass(self, monkeypatch, capsys):
        import requests

        from agent.cli import doctor

        monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:3b")

        def fake_get(url, timeout):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"models": [{"name": "qwen2.5-coder:3b"}]}
            return r

        monkeypatch.setattr(requests, "get", fake_get)

        rc = doctor.run_doctor()
        captured = capsys.readouterr()
        assert rc == 0
        assert "All checks passed" in captured.out


# ---------------------------------------------------------------------------
# Display helpers (unit)
# ---------------------------------------------------------------------------


class TestDisplay:
    def test_print_plan_shows_steps(self, capsys):
        from agent.cli.display import print_plan

        plan = _good_plan()
        print_plan(plan)
        captured = capsys.readouterr()
        assert "step_0" not in captured.err or "list files" in captured.err
        # At minimum the tool name appears
        assert "command" in captured.err

    def test_print_plan_error(self, capsys):
        from agent.cli.display import print_plan_error

        print_plan_error("LLM returned empty output")
        captured = capsys.readouterr()
        assert "LLM returned empty output" in captured.err
        assert "doctor" in captured.err


# ---------------------------------------------------------------------------
# Error mapper (unit)
# ---------------------------------------------------------------------------


class TestErrors:
    def test_known_error_connection_refused(self):
        from agent.cli.errors import explain

        result = explain("Connection refused to localhost:11434")
        assert result is not None
        explanation, fix = result
        assert "systemctl" in fix or "ollama serve" in fix

    def test_known_error_model_not_found(self):
        from agent.cli.errors import explain

        result = explain("model not found")
        assert result is not None
        _, fix = result
        assert "ollama pull" in fix

    def test_known_error_invalid_json(self):
        from agent.cli.errors import explain

        result = explain("LLM returned invalid JSON")
        assert result is not None

    def test_unknown_error_returns_none(self):
        from agent.cli.errors import explain

        result = explain("some completely unknown error xyz123")
        assert result is None


# ---------------------------------------------------------------------------
# Subcommand routing (integration-level, mocked AgentCore)
# ---------------------------------------------------------------------------


class TestSubcommandRouting:
    def _run_main(self, argv):
        """Run main() with the given argv list, capturing SystemExit."""
        import run_agent

        with patch.object(sys, "argv", ["codebud"] + argv):
            try:
                run_agent.main()
            except SystemExit as exc:
                return exc.code
        return 0

    def test_doctor_subcommand(self, monkeypatch):
        """doctor subcommand calls run_doctor."""
        from agent.cli import doctor

        called = {}

        def fake_doctor():
            called["ran"] = True
            return 0

        monkeypatch.setattr(doctor, "run_doctor", fake_doctor)
        rc = self._run_main(["doctor"])
        assert called.get("ran")
        assert rc == 0

    def test_config_subcommand(self, monkeypatch, capsys):
        monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:3b")
        self._run_main(["config"])
        captured = capsys.readouterr()
        assert "qwen2.5-coder:3b" in captured.out

    def test_run_subcommand_plan_error(self, monkeypatch):
        """run subcommand exits 1 on plan_error."""

        class FakeAgent:
            executor = MagicMock()

            def handle_user_message(self, msg, on_chunk=None):
                return _plan_error()

            def close(self):
                pass

        monkeypatch.setattr("run_agent._make_agent", lambda: FakeAgent())
        rc = self._run_main(["run", "do something"])
        assert rc == 1

    def test_backwards_compat_no_subcommand(self, monkeypatch):
        """Bare `codebud "msg"` maps to run."""

        ran = {}

        class FakeAgent:
            executor = MagicMock()
            executor.execute_plan = MagicMock(
                return_value={
                    "status": "ok",
                    "results": {"step_0": {"stdout": "", "returncode": 0}},
                }
            )

            def handle_user_message(self, msg, on_chunk=None):
                ran["msg"] = msg
                return _good_plan()

            def close(self):
                pass

        monkeypatch.setattr("run_agent._make_agent", lambda: FakeAgent())
        # Suppress progress output
        monkeypatch.setattr("run_agent._no_progress", lambda _: True)
        self._run_main(["list files"])
        assert ran.get("msg") == "list files"
