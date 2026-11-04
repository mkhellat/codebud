"""
tests/test_cli.py

Tests for the Codebud CLI: subcommand routing, doctor checks, plan display,
and error mapping.
"""

import json
import sys
from unittest.mock import MagicMock, patch

import pytest

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
        from codebud.cli.doctor import check_python_version

        passed, label, _ = check_python_version()
        assert passed
        assert "Python version" in label

    def test_agent_importable_pass(self):
        from codebud.cli.doctor import check_agent_importable

        passed, label, _ = check_agent_importable()
        assert passed
        assert "importable" in label

    def test_ollama_model_not_set(self, monkeypatch):
        from codebud.cli.doctor import check_ollama_model_set

        monkeypatch.delenv("OLLAMA_MODEL", raising=False)
        passed, label, hint = check_ollama_model_set()
        assert not passed
        assert "OLLAMA_MODEL" in label
        assert "export" in hint

    def test_ollama_model_set(self, monkeypatch):
        from codebud.cli.doctor import check_ollama_model_set

        monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:3b")
        passed, label, _ = check_ollama_model_set()
        assert passed
        assert "qwen2.5-coder:3b" in label

    def test_ollama_reachable_pass(self, monkeypatch):
        import requests

        from codebud.cli import doctor

        def fake_get(url, timeout, **kwargs):
            r = MagicMock()
            r.status_code = 200
            return r

        monkeypatch.setattr(requests, "get", fake_get)
        passed, label, _ = doctor.check_ollama_reachable()
        assert passed

    def test_ollama_reachable_fail_connection(self, monkeypatch):
        import requests

        from codebud.cli import doctor

        def fake_get(url, timeout, **kwargs):
            raise requests.exceptions.ConnectionError("refused")

        monkeypatch.setattr(requests, "get", fake_get)
        passed, label, hint = doctor.check_ollama_reachable()
        assert not passed
        assert "ollama serve" in hint or "systemctl" in hint

    def test_model_pulled_pass(self, monkeypatch):
        import requests

        from codebud.cli import doctor

        monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:3b")

        def fake_get(url, timeout, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"models": [{"name": "qwen2.5-coder:3b"}]}
            return r

        monkeypatch.setattr(requests, "get", fake_get)
        passed, label, _ = doctor.check_model_pulled()
        assert passed

    def test_model_pulled_fail(self, monkeypatch):
        import requests

        from codebud.cli import doctor

        monkeypatch.setenv("OLLAMA_MODEL", "missing-model")

        def fake_get(url, timeout, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"models": []}
            return r

        monkeypatch.setattr(requests, "get", fake_get)
        passed, label, hint = doctor.check_model_pulled()
        assert not passed
        assert "ollama pull" in hint

    def test_config_files_pass(self, tmp_path, monkeypatch):
        from codebud.cli import doctor

        monkeypatch.chdir(tmp_path)  # anywhere: the packaged defaults apply
        passed, label, _ = doctor.check_config_files()
        assert passed
        assert "Policy files OK" in label

    def test_config_files_bad_json(self, tmp_path, monkeypatch):
        from codebud.cli import doctor

        project = tmp_path / ".codebud"
        project.mkdir()
        (project / "harmless_commands.json").write_text("not-json{{{")
        monkeypatch.chdir(tmp_path)

        passed, label, hint = doctor.check_config_files()
        assert not passed
        assert "JSON" in label or "JSON" in hint
        assert ".codebud" in label

    def test_run_doctor_all_pass(self, monkeypatch, capsys):
        import requests

        from codebud.cli import doctor

        monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:3b")

        def fake_get(url, timeout, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"models": [{"name": "qwen2.5-coder:3b"}]}
            return r

        monkeypatch.setattr(requests, "get", fake_get)

        rc = doctor.run_doctor()
        captured = capsys.readouterr()
        assert rc == 0
        assert "All checks passed" in captured.out

    def test_run_doctor_no_color_when_not_tty(self, monkeypatch, capsys):
        """Under pytest capture, stdout isn't a TTY, so no ANSI escapes appear."""
        import requests

        from codebud.cli import doctor

        monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:3b")

        def fake_get(url, timeout, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"models": [{"name": "qwen2.5-coder:3b"}]}
            return r

        monkeypatch.setattr(requests, "get", fake_get)
        doctor.run_doctor()
        captured = capsys.readouterr()
        assert "\033[" not in captured.out

    def test_run_doctor_color_when_tty(self, monkeypatch, capsys):
        """When stdout is a TTY, PASS/FAIL lines carry ANSI color codes."""
        import requests

        from codebud.cli import doctor

        monkeypatch.setattr(doctor, "_IS_TTY", True)
        monkeypatch.setattr(doctor, "_BOLD", "\033[1m")
        monkeypatch.setattr(doctor, "_DIM", "\033[2m")
        monkeypatch.setattr(doctor, "_GREEN", "\033[32m")
        monkeypatch.setattr(doctor, "_RED", "\033[31m")
        monkeypatch.setattr(doctor, "_RESET", "\033[0m")
        monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5-coder:3b")

        def fake_get(url, timeout, **kwargs):
            r = MagicMock()
            r.status_code = 200
            r.json.return_value = {"models": [{"name": "qwen2.5-coder:3b"}]}
            return r

        monkeypatch.setattr(requests, "get", fake_get)
        doctor.run_doctor()
        captured = capsys.readouterr()
        assert "\033[32m[PASS]\033[0m" in captured.out


# ---------------------------------------------------------------------------
# Display helpers (unit)
# ---------------------------------------------------------------------------


class TestDisplay:
    def test_print_rule_spans_terminal_width(self, monkeypatch, capsys):
        from codebud.cli import display

        monkeypatch.setattr(display, "_IS_TTY", True)
        monkeypatch.setenv("COLUMNS", "40")
        display.print_rule()
        line = capsys.readouterr().err.strip("\n")
        assert "\u2500" * 40 in line
        assert "\u2500" * 41 not in line

    def test_print_rule_prints_nothing_when_not_a_tty(self, monkeypatch, capsys):
        from codebud.cli import display

        monkeypatch.setattr(display, "_IS_TTY", False)
        display.print_rule()
        assert capsys.readouterr().err == ""

    def test_print_rule_falls_back_to_ascii(self, monkeypatch):
        import io

        from codebud.cli import display

        buf = io.BytesIO()
        stream = io.TextIOWrapper(buf, encoding="ascii")
        monkeypatch.setattr(display, "_IS_TTY", True)
        monkeypatch.setattr(sys, "stderr", stream)
        monkeypatch.setenv("COLUMNS", "10")
        display.print_rule()
        stream.flush()
        assert "-" * 10 in buf.getvalue().decode("ascii")

    def test_print_plan_shows_steps(self, capsys):
        from codebud.cli.display import print_plan

        plan = _good_plan()
        print_plan(plan)
        captured = capsys.readouterr()
        assert "step_0" not in captured.err or "list files" in captured.err
        # At minimum the tool name appears
        assert "command" in captured.err

    def test_print_step_result_shows_short_stdout_by_default(self, capsys):
        from codebud.cli.display import print_step_result

        result = {"returncode": 0, "stdout": "line1\nline2\nline3", "stderr": ""}
        print_step_result(result)
        captured = capsys.readouterr()
        assert "line1" in captured.err
        assert "line2" in captured.err
        assert "line3" in captured.err
        assert "more line" not in captured.err

    def test_print_step_result_truncates_long_stdout_by_default(self, capsys):
        from codebud.cli.display import _PREVIEW_LINES, print_step_result

        stdout = "\n".join(f"line{i}" for i in range(1, _PREVIEW_LINES + 11))
        result = {"returncode": 0, "stdout": stdout, "stderr": ""}
        print_step_result(result)
        captured = capsys.readouterr()
        assert f"line{_PREVIEW_LINES}" in captured.err
        assert f"line{_PREVIEW_LINES + 1}" not in captured.err
        assert "+10 more lines" in captured.err
        assert "/output" in captured.err

    def test_print_step_result_verbose_shows_everything(self, capsys):
        from codebud.cli.display import _PREVIEW_LINES, print_step_result

        stdout = "\n".join(f"line{i}" for i in range(1, _PREVIEW_LINES + 11))
        result = {"returncode": 0, "stdout": stdout, "stderr": ""}
        print_step_result(result, verbose=True)
        captured = capsys.readouterr()
        assert f"line{_PREVIEW_LINES + 10}" in captured.err
        assert "more line" not in captured.err

    def test_print_step_result_full_shows_everything(self, capsys):
        from codebud.cli.display import _PREVIEW_LINES, print_step_result

        stdout = "\n".join(f"line{i}" for i in range(1, _PREVIEW_LINES + 11))
        result = {"returncode": 0, "stdout": stdout, "stderr": ""}
        print_step_result(result, full=True)
        captured = capsys.readouterr()
        assert f"line{_PREVIEW_LINES + 10}" in captured.err
        assert "more line" not in captured.err


# ---------------------------------------------------------------------------
# Error mapper (unit)
# ---------------------------------------------------------------------------


class TestErrors:
    def test_known_error_connection_refused(self):
        from codebud.cli.errors import explain

        result = explain("Connection refused to localhost:11434")
        assert result is not None
        explanation, fix = result
        assert "systemctl" in fix or "ollama serve" in fix

    def test_known_error_model_not_found(self):
        from codebud.cli.errors import explain

        result = explain("model not found")
        assert result is not None
        _, fix = result
        assert "ollama pull" in fix

    def test_known_error_invalid_json(self):
        from codebud.cli.errors import explain

        result = explain("LLM returned invalid JSON")
        assert result is not None

    def test_safety_refusal_explains_how_to_allow_the_command(self):
        from codebud.cli.errors import explain

        explanation, fix = explain(
            "The safety policy refused step 1 (du -sh src): du is not on the allow-list"
        )
        assert "harmless_commands.json" in fix and "codebud config" in fix
        _, fix = explain("The plan uses tools not allowed in edit mode: command.")
        assert "/mode ask" in fix

    def test_unknown_error_returns_none(self):
        from codebud.cli.errors import explain

        result = explain("some completely unknown error xyz123")
        assert result is None


# ---------------------------------------------------------------------------
# Subcommand routing (integration-level, mocked AgentCore)
# ---------------------------------------------------------------------------


class TestModelOverride:
    """--model/-m: validates before construction, warms synchronously, never auto-pulls."""

    def _run_main(self, argv):
        from codebud.cli import main as run_agent

        with patch.object(sys, "argv", ["codebud"] + argv):
            try:
                run_agent.main()
            except SystemExit as exc:
                return exc.code
        return 0

    def test_valid_model_sets_env_and_constructs_agent(self, monkeypatch):
        monkeypatch.setattr("codebud.llm_stub.list_models", lambda: ["good-model"])
        monkeypatch.setattr("codebud.llm_stub.warm_model_sync", lambda model, on_chunk=None: True)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)

        seen = {}

        class FakeAgent:
            executor = MagicMock()

            def handle_user_message(self, msg, on_chunk=None):
                seen["model_at_call"] = __import__("os").environ.get("OLLAMA_MODEL")
                return {"status": "chat_reply", "reply": "ok"}

            def is_powerful(self, step):
                return False

            def confirmation_reason(self, step):
                return _fake_confirmation_reason(self, step)

            def close(self):
                pass

        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: FakeAgent())
        rc = self._run_main(["run", "--model", "good-model", "do something"])
        assert rc == 0
        assert seen["model_at_call"] == "good-model"

    def test_undownloaded_model_aborts_before_planning(self, monkeypatch, capsys):
        monkeypatch.setattr("codebud.llm_stub.list_models", lambda: ["other-model"])
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)

        called = {}

        def fail_make_agent():
            called["made"] = True
            raise AssertionError("agent should never be constructed")

        monkeypatch.setattr("codebud.cli.common.make_agent", fail_make_agent)
        rc = self._run_main(["run", "--model", "missing-model", "do something"])
        assert rc == 1
        assert "made" not in called
        captured = capsys.readouterr()
        assert "not downloaded" in captured.err
        assert "ollama pull missing-model" in captured.err

    def test_unreachable_ollama_aborts(self, monkeypatch, capsys):
        monkeypatch.setattr("codebud.llm_stub.list_models", lambda: [])
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)
        monkeypatch.setattr(
            "codebud.cli.common.make_agent",
            lambda: (_ for _ in ()).throw(AssertionError("should not construct")),
        )

        rc = self._run_main(["plan", "--model", "any-model", "do something"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "Cannot reach Ollama" in captured.err

    def test_warmup_failure_aborts(self, monkeypatch, capsys):
        monkeypatch.setattr("codebud.llm_stub.list_models", lambda: ["good-model"])
        monkeypatch.setattr("codebud.llm_stub.warm_model_sync", lambda model, on_chunk=None: False)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)
        monkeypatch.setattr(
            "codebud.cli.common.make_agent",
            lambda: (_ for _ in ()).throw(AssertionError("should not construct")),
        )

        rc = self._run_main(["run", "--model", "good-model", "do something"])
        assert rc == 1
        captured = capsys.readouterr()
        assert "Failed to warm up" in captured.err

    def test_no_model_flag_skips_override_entirely(self, monkeypatch):
        """Without --model, list_models/warm_model_sync must not be called."""

        def fail(*a, **kw):
            raise AssertionError("should not be called without --model")

        monkeypatch.setattr("codebud.llm_stub.list_models", fail)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)

        class FakeAgent:
            executor = MagicMock()

            def handle_user_message(self, msg, on_chunk=None):
                return {"status": "chat_reply", "reply": "ok"}

            def is_powerful(self, step):
                return False

            def confirmation_reason(self, step):
                return _fake_confirmation_reason(self, step)

            def close(self):
                pass

        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: FakeAgent())
        rc = self._run_main(["run", "do something"])
        assert rc == 0


class TestSubcommandRouting:
    def _run_main(self, argv):
        """Run main() with the given argv list, capturing SystemExit."""
        from codebud.cli import main as run_agent

        with patch.object(sys, "argv", ["codebud"] + argv):
            try:
                run_agent.main()
            except SystemExit as exc:
                return exc.code
        return 0

    def test_doctor_subcommand(self, monkeypatch):
        """doctor subcommand calls run_doctor."""
        from codebud.cli import doctor

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

    def test_config_shows_openai_model_and_its_default(self, monkeypatch, capsys):
        """It used to be left out, though it decides the OpenAI model."""
        monkeypatch.delenv("OPENAI_MODEL", raising=False)
        self._run_main(["config"])
        line = next(ln for ln in capsys.readouterr().out.splitlines() if "OPENAI_MODEL" in ln)
        assert line.split("=", 1)[1].split()[0] == "gpt-3.5-turbo"
        monkeypatch.setenv("OPENAI_MODEL", "gpt-4o-mini")
        self._run_main(["config"])
        assert "= gpt-4o-mini" in capsys.readouterr().out

    def test_config_shows_every_documented_variable(self, monkeypatch, capsys):
        self._run_main(["config"])
        out = capsys.readouterr().out
        for name in (
            "CODEBUD_BACKEND",
            "OLLAMA_MODEL",
            "OLLAMA_BASE_URL",
            "CODEBUD_NUM_CTX",
            "EMBED_MODEL",
            "OPENAI_API_KEY",
            "OPENAI_MODEL",
            "CODEBUD_RAG",
            "CODEBUD_RAG_HINTS",
            "WEB_SEARCH_ENABLED",
            "CODEBUD_MASK_SECRETS",
            "CODEBUD_SESSION_RETENTION_DAYS",
        ):
            assert name in out, name

    def test_run_subcommand_plan_error(self, monkeypatch):
        """run subcommand exits 1 on plan_error."""

        class FakeAgent:
            executor = MagicMock()

            def handle_user_message(self, msg, on_chunk=None):
                return _plan_error()

            def is_powerful(self, step):
                return False

            def confirmation_reason(self, step):
                return _fake_confirmation_reason(self, step)

            def close(self):
                pass

        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: FakeAgent())
        rc = self._run_main(["run", "do something"])
        assert rc == 1

    def test_run_subcommand_plan_error_prints_once(self, monkeypatch, capsys):
        """The plan_error message must appear exactly once, not duplicated
        by two separate error-printing paths."""

        class FakeAgent:
            executor = MagicMock()

            def handle_user_message(self, msg, on_chunk=None):
                return _plan_error("LLM returned empty output")

            def is_powerful(self, step):
                return False

            def confirmation_reason(self, step):
                return _fake_confirmation_reason(self, step)

            def close(self):
                pass

        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: FakeAgent())
        self._run_main(["run", "do something"])
        captured = capsys.readouterr()
        assert captured.err.count("LLM returned empty output") == 1

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

            def is_powerful(self, step):
                return False

            def confirmation_reason(self, step):
                return _fake_confirmation_reason(self, step)

            def close(self):
                pass

        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: FakeAgent())
        # Suppress progress output
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)
        self._run_main(["list files"])
        assert ran.get("msg") == "list files"

    def _two_step_plan(self):
        return {
            "status": "ok",
            "plan": [
                {"id": "step_0", "description": "first", "tool": "command", "args": {"cmd": "ls"}},
                {
                    "id": "step_1",
                    "description": "second",
                    "tool": "command",
                    "args": {"cmd": "pwd"},
                },
            ],
        }

    def _fake_agent_with_plan(self, plan):
        class FakeAgent:
            executor = MagicMock()
            executor.execute_plan = MagicMock(
                return_value={
                    "status": "ok",
                    "results": {"step_0": {"stdout": "", "returncode": 0}},
                }
            )

            def handle_user_message(self, msg, on_chunk=None):
                return plan

            def is_powerful(self, step):
                return step.get("args", {}).get("cmd", "").startswith("pytest")

            def confirmation_reason(self, step):
                return _fake_confirmation_reason(self, step)

            def close(self):
                pass

        return FakeAgent()

    def _powerful_plan(self):
        plan = self._two_step_plan()
        plan["plan"][1]["args"]["cmd"] = "pytest -q"
        return plan

    def test_run_asks_before_a_powerful_step(self, monkeypatch, capsys):
        agent = self._fake_agent_with_plan(self._powerful_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)
        prompts = []
        monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or "n")

        rc = self._run_main(["run", "do something"])
        assert agent.executor.execute_plan.call_count == 1  # only the harmless step
        assert prompts == ["  Run it? [y/N] "]
        assert "Powerful command" in capsys.readouterr().err
        assert rc == 0

    def test_run_unattended_powerful_step_fails_with_hint(self, monkeypatch, capsys):
        """No one to answer (stdin closed): the step is not run and run exits 1."""
        agent = self._fake_agent_with_plan(self._powerful_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)

        def eof(_prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)
        rc = self._run_main(["run", "do something"])
        assert agent.executor.execute_plan.call_count == 1
        assert rc == 1
        assert "--yes" in capsys.readouterr().err

    def test_run_asks_before_a_web_search_and_shows_the_query(self, monkeypatch, capsys):
        agent = self._fake_agent_with_plan(_web_search_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)
        monkeypatch.setattr("builtins.input", lambda p="": "n")
        self._run_main(["run", "find the latest ruff release"])
        err = capsys.readouterr().err
        assert agent.executor.execute_plan.call_count == 0
        assert 'sends "latest ruff release" to DuckDuckGo' in err

    def test_run_unattended_web_search_fails_with_hint(self, monkeypatch, capsys):
        agent = self._fake_agent_with_plan(_web_search_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)

        def eof(_prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)
        rc = self._run_main(["run", "find it"])
        err = capsys.readouterr().err
        assert rc == 1 and agent.executor.execute_plan.call_count == 0
        assert "--yes" in err and "web search" in err

    def test_run_yes_allows_web_search(self, monkeypatch):
        agent = self._fake_agent_with_plan(_web_search_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)
        monkeypatch.setattr("builtins.input", lambda p="": pytest.fail("asked"))
        self._run_main(["run", "--yes", "find it"])
        assert agent.executor.execute_plan.call_count == 1

    @pytest.mark.parametrize("flag", ["--yes", "-y"])
    def test_run_yes_runs_powerful_steps_without_asking(self, monkeypatch, flag):
        agent = self._fake_agent_with_plan(self._powerful_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)

        def fail_input(_prompt=""):
            raise AssertionError("--yes must not prompt")

        monkeypatch.setattr("builtins.input", fail_input)
        self._run_main(["run", flag, "do something"])
        assert agent.executor.execute_plan.call_count == 2

    def test_unattended_profile_runs_without_asking(self, monkeypatch, capsys):
        """--profile unattended: full-auto, so run asks nothing (phase 10)."""
        import os

        agent = self._fake_agent_with_plan(self._powerful_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)
        monkeypatch.setattr("builtins.input", lambda p="": pytest.fail("asked"))
        assert self._run_main(["--profile", "unattended", "run", "do something"]) == 0
        assert agent.executor.execute_plan.call_count == 2
        assert os.environ["CODEBUD_PRIVACY"] == "trusted"
        assert "Profile: unattended (full-auto, privacy trusted)" in capsys.readouterr().err

    def test_interactive_beats_an_unattended_profile(self, monkeypatch):
        agent = self._fake_agent_with_plan(self._powerful_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)
        prompts = []
        monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or "n")
        self._run_main(["--profile", "unattended", "run", "-i", "do something"])
        assert len(prompts) == 2 and agent.executor.execute_plan.call_count == 0

    def test_unknown_profile_exits_2(self, monkeypatch, capsys):
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: pytest.fail("built"))
        assert self._run_main(["--profile", "nope", "run", "x"]) == 2
        assert "no profile 'nope'" in capsys.readouterr().err

    def test_plan_execute_asks_like_run(self, monkeypatch, capsys):
        """plan -x used to run powerful steps and web searches unasked."""
        agent = self._fake_agent_with_plan(self._powerful_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)
        prompts = []
        monkeypatch.setattr("builtins.input", lambda p="": prompts.append(p) or "n")
        rc = self._run_main(["plan", "-x", "do something"])
        assert agent.executor.execute_plan.call_count == 1  # only the harmless step
        assert prompts == ["  Run it? [y/N] "]
        assert rc == 0

    def test_plan_execute_unattended_needs_yes(self, monkeypatch, capsys):
        agent = self._fake_agent_with_plan(self._powerful_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)

        def eof(_prompt=""):
            raise EOFError

        monkeypatch.setattr("builtins.input", eof)
        assert self._run_main(["plan", "-x", "do something"]) == 1
        assert "--yes" in capsys.readouterr().err

        agent = self._fake_agent_with_plan(self._powerful_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        assert self._run_main(["plan", "-x", "--yes", "do something"]) in (0, None)
        assert agent.executor.execute_plan.call_count == 2

    def test_run_interactive_approves_each_step(self, monkeypatch):
        """run -i prompts per-step; 'y' each time executes both steps."""
        agent = self._fake_agent_with_plan(self._two_step_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)
        monkeypatch.setattr("builtins.input", lambda _prompt="": "y")

        self._run_main(["run", "-i", "do something"])
        assert agent.executor.execute_plan.call_count == 2

    def test_run_interactive_skip_step(self, monkeypatch, capsys):
        """run -i with 'n' skips that step but continues to the next."""
        agent = self._fake_agent_with_plan(self._two_step_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)

        answers = iter(["n", "y"])
        monkeypatch.setattr("builtins.input", lambda _prompt="": next(answers))

        self._run_main(["run", "-i", "do something"])
        assert agent.executor.execute_plan.call_count == 1
        captured = capsys.readouterr()
        assert "skipped" in captured.err

    def test_run_interactive_quit_aborts(self, monkeypatch, capsys):
        """run -i with 'q' stops before executing any further steps."""
        agent = self._fake_agent_with_plan(self._two_step_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)
        monkeypatch.setattr("builtins.input", lambda _prompt="": "q")

        self._run_main(["run", "-i", "do something"])
        assert not agent.executor.execute_plan.called
        captured = capsys.readouterr()
        assert "aborted" in captured.err

    def test_run_interactive_all_approves_remaining_steps(self, monkeypatch):
        """run -i with 'a' approves the current and all remaining steps without further prompts."""
        agent = self._fake_agent_with_plan(self._two_step_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)
        monkeypatch.setattr("builtins.input", lambda _prompt="": "a")

        self._run_main(["run", "-i", "do something"])
        assert agent.executor.execute_plan.call_count == 2

    def test_run_non_interactive_does_not_prompt(self, monkeypatch):
        """Without -i, run executes all steps without calling input()."""
        agent = self._fake_agent_with_plan(self._two_step_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)

        def fail_input(_prompt=""):
            raise AssertionError("input() should not be called without -i")

        monkeypatch.setattr("builtins.input", fail_input)

        self._run_main(["run", "do something"])
        assert agent.executor.execute_plan.call_count == 2

    def test_run_prints_full_stdout_without_truncation(self, monkeypatch, capsys):
        """run has no follow-up prompt like chat's /output, so it must never
        truncate step stdout the way chat's default preview does."""
        plan = {
            "status": "ok",
            "plan": [
                {"id": "step_0", "description": "list", "tool": "command", "args": {"cmd": "ls"}}
            ],
        }
        full_output = "\n".join(f"line{i}" for i in range(1, 30))

        class FakeAgent:
            executor = MagicMock()
            executor.execute_plan = MagicMock(
                return_value={
                    "status": "ok",
                    "results": {"step_0": {"stdout": full_output, "returncode": 0}},
                }
            )

            def handle_user_message(self, msg, on_chunk=None):
                return plan

            def is_powerful(self, step):
                return False

            def confirmation_reason(self, step):
                return _fake_confirmation_reason(self, step)

            def close(self):
                pass

        agent = FakeAgent()
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)

        self._run_main(["run", "do something"])
        captured = capsys.readouterr()
        assert "line1" in captured.err
        assert "line29" in captured.err
        assert "more line" not in captured.err


# ---------------------------------------------------------------------------
# plan subcommand
# ---------------------------------------------------------------------------


class TestPlanSubcommand:
    def _run_main(self, argv):
        from codebud.cli import main as run_agent

        with patch.object(sys, "argv", ["codebud"] + argv):
            try:
                run_agent.main()
            except SystemExit as exc:
                return exc.code
        return 0

    def _fake_agent_with_plan(self, plan, stdout=""):
        class FakeAgent:
            executor = MagicMock()
            executor.execute_plan = MagicMock(
                return_value={
                    "status": "ok",
                    "results": {"step_0": {"stdout": stdout, "returncode": 0}},
                }
            )

            def handle_user_message(self, msg, on_chunk=None):
                return plan

            def is_powerful(self, step):
                return False

            def confirmation_reason(self, step):
                return _fake_confirmation_reason(self, step)

            def close(self):
                pass

        return FakeAgent()

    def test_plan_default_does_not_execute(self, monkeypatch):
        """Plain `codebud plan` never touches the executor — dry-run only."""
        agent = self._fake_agent_with_plan(_good_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)

        self._run_main(["plan", "do something"])
        assert not agent.executor.execute_plan.called

    def test_plan_execute_runs_steps_and_shows_full_output(self, monkeypatch, capsys):
        full_output = "\n".join(f"line{i}" for i in range(1, 30))
        agent = self._fake_agent_with_plan(_good_plan(), stdout=full_output)
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)

        self._run_main(["plan", "--execute", "do something"])
        assert agent.executor.execute_plan.called
        captured = capsys.readouterr()
        assert "line1" in captured.err
        assert "line29" in captured.err
        assert "more line" not in captured.err

    def test_plan_execute_and_json_rejected(self, monkeypatch, capsys):
        agent = self._fake_agent_with_plan(_good_plan())
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)

        rc = self._run_main(["plan", "--execute", "--json", "do something"])
        assert rc == 2
        assert not agent.executor.execute_plan.called


# ---------------------------------------------------------------------------
# history subcommand
# ---------------------------------------------------------------------------


def _write_entries(path, entries):
    import json as _json

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(_json.dumps(e) + "\n" for e in entries))


def _entry(desc, tool="command", returncode=0, timestamp="2026-01-01T00:00:00"):
    return {
        "timestamp": timestamp,
        "data": {
            "step": {"id": "step_0", "description": desc, "tool": tool, "args": {}},
            "result": {"stdout": "", "stderr": "", "returncode": returncode},
        },
    }


class TestHistorySubcommand:
    def _run_main(self, argv):
        from codebud.cli import main as run_agent

        with patch.object(sys, "argv", ["codebud"] + argv):
            try:
                run_agent.main()
            except SystemExit as exc:
                return exc.code
        return 0

    def test_no_entries(self, tmp_path, monkeypatch, capsys):
        from codebud import memory as memory_mod

        monkeypatch.setattr(memory_mod, "MEMORY_PATH", str(tmp_path / "entries.jsonl"))
        self._run_main(["history"])
        captured = capsys.readouterr()
        assert "No history yet" in captured.out

    def test_lists_entries_newest_relevant_fields(self, tmp_path, monkeypatch, capsys):
        from codebud import memory as memory_mod

        path = tmp_path / "entries.jsonl"
        _write_entries(path, [_entry("first"), _entry("second", returncode=1)])
        monkeypatch.setattr(memory_mod, "MEMORY_PATH", str(path))

        self._run_main(["history"])
        captured = capsys.readouterr()
        assert "first" in captured.out
        assert "second" in captured.out
        assert "exit 1" in captured.out
        assert "ok" in captured.out

    def test_limit_option(self, tmp_path, monkeypatch, capsys):
        from codebud import memory as memory_mod

        path = tmp_path / "entries.jsonl"
        _write_entries(path, [_entry("one"), _entry("two"), _entry("three")])
        monkeypatch.setattr(memory_mod, "MEMORY_PATH", str(path))

        self._run_main(["history", "-n", "1"])
        captured = capsys.readouterr()
        assert "three" in captured.out
        assert "one" not in captured.out
        assert "two" not in captured.out

    def test_limit_zero_shows_all(self, tmp_path, monkeypatch, capsys):
        from codebud import memory as memory_mod

        path = tmp_path / "entries.jsonl"
        _write_entries(path, [_entry("one"), _entry("two")])
        monkeypatch.setattr(memory_mod, "MEMORY_PATH", str(path))

        self._run_main(["history", "-n", "0"])
        captured = capsys.readouterr()
        assert "one" in captured.out
        assert "two" in captured.out

    def test_json_output(self, tmp_path, monkeypatch, capsys):
        from codebud import memory as memory_mod

        path = tmp_path / "entries.jsonl"
        _write_entries(path, [_entry("first")])
        monkeypatch.setattr(memory_mod, "MEMORY_PATH", str(path))

        self._run_main(["history", "--json"])
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert parsed[0]["data"]["step"]["description"] == "first"


# ---------------------------------------------------------------------------
# chat subcommand (interactive REPL)
# ---------------------------------------------------------------------------


def _fake_confirmation_reason(agent, step):
    """What AgentCore.confirmation_reason() says, for the fakes."""
    from codebud.safety import POWERFUL_REASON, egress_reason

    return POWERFUL_REASON if agent.is_powerful(step) else egress_reason(step)


def _web_search_plan(query="latest ruff release"):
    return {
        "status": "ok",
        "plan": [
            {
                "id": "step_0",
                "description": "search",
                "tool": "web_search",
                "args": {"query": query},
            }
        ],
    }


class FakeChatAgent:
    """Stand-in for AgentCore driven entirely through its public API."""

    def __init__(
        self, plan_by_message=None, model="fake-model", set_model_result=None, usage=(500, 8192)
    ):
        self._history = []
        self._usage = usage
        self.executor = MagicMock()
        self.executor.execute_plan = MagicMock(
            return_value={
                "status": "ok",
                "results": {"step_0": {"stdout": "", "returncode": 0}},
            }
        )
        self._plan_by_message = plan_by_message or {}
        self.closed = False
        self._model = model
        self._set_model_result = set_model_result

    def get_model(self):
        return self._model

    def reload_policy(self):
        self.policy_reloads = getattr(self, "policy_reloads", 0) + 1

    def get_mode(self):
        return getattr(self, "_mode", "ask")

    def set_mode(self, mode):
        from codebud.modes import MODES

        if mode not in MODES:
            return False, f"Unknown mode '{mode}'. Choose one of: {', '.join(MODES)}."
        self._mode = mode
        return True, f"Mode: {mode}"

    def set_model(self, model, on_chunk=None):
        if self._set_model_result is not None:
            return self._set_model_result
        self._model = model
        return True, f"Switched to '{model}'."

    def handle_user_message(self, msg, on_chunk=None):
        self._history.append({"role": "user", "content": msg})
        plan = self._plan_by_message.get(msg, _good_plan())
        summary = "planned" if plan.get("status") == "ok" else plan.get("status")
        self._history.append({"role": "assistant", "content": summary})
        return plan

    def clear_history(self):
        self._history.clear()

    def get_history(self):
        return list(self._history)

    def restore_history(self, history):
        self._history[:] = list(history)

    def context_usage(self):
        return self._usage

    def is_powerful(self, step):
        return step.get("args", {}).get("cmd", "").startswith("pytest")

    def confirmation_reason(self, step):
        return _fake_confirmation_reason(self, step)

    def compact_history(self, keep_last=2, on_chunk=None):
        older = self._history[:-keep_last]
        if len(older) < 2:
            return False, "Nothing to compact yet."
        self._history[:] = [{"role": "summary", "content": "S"}] + self._history[-keep_last:]
        return True, f"Compacted {len(older)} entries into a summary."

    last_context = []
    _rag = True

    def get_rag(self):
        return self._rag

    def set_rag(self, on):
        self._rag = on

    def undo_last_turn(self):
        if len(self._history) >= 2:
            self._history.pop()
            self._history.pop()
            return True
        self._history.clear()
        return False

    def close(self):
        self.closed = True


class _ChatHarness:
    """Shared fixture and helpers for driving the chat REPL in tests."""

    @pytest.fixture(autouse=True)
    def _isolated_sessions(self, tmp_path, monkeypatch):
        """Keep session checkpoints out of the repo's data/ directory."""
        self.sessions_dir = tmp_path / "sessions"
        monkeypatch.setattr("codebud.sessions.SESSIONS_DIR", str(self.sessions_dir))

    def _saved_sessions(self):
        if not self.sessions_dir.exists():
            return []
        return [json.loads(p.read_text()) for p in sorted(self.sessions_dir.glob("*.json"))]

    def _run_chat(self, monkeypatch, inputs, agent=None, extra_argv=None):
        from codebud.cli import main as run_agent

        agent = agent or FakeChatAgent()
        monkeypatch.setattr("codebud.cli.common.make_agent", lambda: agent)
        monkeypatch.setattr("codebud.cli.common.no_progress", lambda _: True)

        it = iter(inputs)

        def fake_input(_prompt=""):
            try:
                return next(it)
            except StopIteration:
                raise EOFError from None

        monkeypatch.setattr("builtins.input", fake_input)

        with patch.object(sys, "argv", ["codebud", "chat", *(extra_argv or [])]):
            run_agent.main()

        return agent


class TestChatSubcommand(_ChatHarness):
    def test_quit_closes_agent_without_sending_message(self, monkeypatch):
        agent = self._run_chat(monkeypatch, ["quit"])
        assert agent.closed
        assert agent.get_history() == []

    @pytest.mark.parametrize("word", ["/exit", "/quit", "/EXIT", "/quit now"])
    def test_slash_exit_and_quit_end_the_session(self, monkeypatch, capsys, word):
        """/exit and /quit used to answer "Unknown command" and keep going."""
        agent = self._run_chat(monkeypatch, [word, "do something", "n"])
        assert agent.closed
        assert agent.get_history() == []
        assert "Unknown command" not in capsys.readouterr().err

    def test_empty_line_does_not_end_the_session(self, monkeypatch):
        """Pressing Enter on an empty line used to end the chat."""
        agent = self._run_chat(monkeypatch, ["", "   ", "do something", "n"])
        assert [t["content"] for t in agent.get_history() if t["role"] == "user"] == [
            "do something"
        ]

    def test_plan_accept_executes_all_steps(self, monkeypatch, capsys):
        agent = self._run_chat(monkeypatch, ["do something", "y", "y"])
        assert agent.executor.execute_plan.called
        captured = capsys.readouterr()
        assert "step" in captured.err.lower() or "step" in captured.out.lower()

    def test_plan_skip_does_not_execute(self, monkeypatch):
        agent = self._run_chat(monkeypatch, ["do something", "n"])
        assert not agent.executor.execute_plan.called

    def test_chat_reply_fallback_printed(self, monkeypatch, capsys):
        agent = FakeChatAgent(
            plan_by_message={"hello": {"status": "chat_reply", "reply": "hi there"}}
        )
        self._run_chat(monkeypatch, ["hello"], agent=agent)
        captured = capsys.readouterr()
        # Interactive chat output (including the assistant reply) goes to
        # stderr, matching every other prompt/status line in chat mode, so
        # stdout stays clean for piping.
        assert "hi there" in captured.err

    def test_slash_help(self, monkeypatch, capsys):
        self._run_chat(monkeypatch, ["/help"])
        captured = capsys.readouterr()
        assert "/undo" in captured.err
        assert "/output" in captured.err
        assert "/models" in captured.err

    def test_slash_clear_resets_history_and_turn(self, monkeypatch, capsys):
        agent = self._run_chat(monkeypatch, ["do something", "n", "/clear"])
        captured = capsys.readouterr()
        assert "History cleared." in captured.err
        assert agent.get_history() == []

    def test_slash_history_empty(self, monkeypatch, capsys):
        self._run_chat(monkeypatch, ["/history"])
        captured = capsys.readouterr()
        assert "no history yet" in captured.err

    def test_slash_history_lists_turns(self, monkeypatch, capsys):
        self._run_chat(monkeypatch, ["do something", "n", "/history"])
        captured = capsys.readouterr()
        assert "USER: do something" in captured.err
        assert "ASSISTANT:" in captured.err

    def test_slash_undo_removes_last_turn(self, monkeypatch, capsys):
        agent = self._run_chat(monkeypatch, ["do something", "n", "/undo"])
        captured = capsys.readouterr()
        assert "Last turn removed." in captured.err
        assert agent.get_history() == []

    def test_slash_undo_with_no_history_clears(self, monkeypatch, capsys):
        agent = self._run_chat(monkeypatch, ["/undo"])
        captured = capsys.readouterr()
        assert "nothing left to undo" in captured.err
        assert agent.get_history() == []

    def test_slash_model_prints_active_model(self, monkeypatch, capsys):
        agent = FakeChatAgent(model="qwen2.5-coder:3b")
        self._run_chat(monkeypatch, ["/model"], agent=agent)
        captured = capsys.readouterr()
        assert "qwen2.5-coder:3b" in captured.err

    def test_slash_model_with_arg_switches_model(self, monkeypatch, capsys):
        agent = FakeChatAgent(model="old-model")
        self._run_chat(monkeypatch, ["/model new-model"], agent=agent)
        captured = capsys.readouterr()
        assert agent.get_model() == "new-model"
        assert "Switched to 'new-model'" in captured.err

    def test_slash_model_switch_failure_shown(self, monkeypatch, capsys):
        agent = FakeChatAgent(
            model="old-model",
            set_model_result=(False, "Model 'bogus' is not downloaded. Run: ollama pull bogus"),
        )
        self._run_chat(monkeypatch, ["/model bogus"], agent=agent)
        captured = capsys.readouterr()
        assert "not downloaded" in captured.err
        assert agent.get_model() == "old-model"

    def test_slash_models_lists_and_marks_active(self, monkeypatch, capsys):
        agent = FakeChatAgent(model="model-b")
        monkeypatch.setattr("codebud.llm_stub.list_models", lambda: ["model-a", "model-b"])
        self._run_chat(monkeypatch, ["/models"], agent=agent)
        captured = capsys.readouterr()
        assert "model-a" in captured.err
        assert "model-b" in captured.err
        assert "active" in captured.err

    def test_slash_plan_before_any_plan(self, monkeypatch, capsys):
        self._run_chat(monkeypatch, ["/plan"])
        captured = capsys.readouterr()
        assert "no plan yet" in captured.err

    def test_slash_output_before_any_step(self, monkeypatch, capsys):
        self._run_chat(monkeypatch, ["/output"])
        captured = capsys.readouterr()
        assert "no step output yet" in captured.err

    def test_slash_output_shows_full_stdout_after_step(self, monkeypatch, capsys):
        agent = FakeChatAgent()
        full_output = "\n".join(f"line{i}" for i in range(1, 30))
        agent.executor.execute_plan = MagicMock(
            return_value={
                "status": "ok",
                "results": {"step_0": {"stdout": full_output, "returncode": 0}},
            }
        )
        self._run_chat(monkeypatch, ["do something", "y", "y", "/output"], agent=agent)
        captured = capsys.readouterr()
        assert "line1" in captured.err
        assert "line29" in captured.err

    def test_unknown_slash_command(self, monkeypatch, capsys):
        self._run_chat(monkeypatch, ["/bogus"])
        captured = capsys.readouterr()
        assert "Unknown command" in captured.err

    def test_revise_then_accept(self, monkeypatch):
        agent = self._run_chat(monkeypatch, ["do something", "r: use a different tool", "y", "y"])
        # Two handle_user_message calls: original + revision.
        assert len(agent.get_history()) == 4
        assert "revision note: use a different tool" in agent.get_history()[2]["content"]
        assert agent.executor.execute_plan.called

    def test_plan_error_from_planner_shown_and_loop_continues(self, monkeypatch, capsys):
        agent = FakeChatAgent(plan_by_message={"bad": _plan_error("LLM returned invalid JSON")})
        self._run_chat(monkeypatch, ["bad"], agent=agent)
        captured = capsys.readouterr()
        assert "LLM returned invalid JSON" in captured.err

    def test_rule_printed_before_every_prompt(self, monkeypatch, capsys):
        from codebud.cli import display

        monkeypatch.setattr(display, "_IS_TTY", True)
        monkeypatch.setenv("COLUMNS", "20")
        self._run_chat(monkeypatch, ["/help", "/history"])
        # Three prompts: two slash commands, then EOF ends the session.
        assert capsys.readouterr().err.count("\u2500" * 20) == 3

    def test_compact_command_summarises_history(self, monkeypatch, capsys):
        agent = self._run_chat(monkeypatch, ["one", "y", "y", "two", "y", "y", "/compact"])
        history = agent.get_history()
        assert history[0] == {"role": "summary", "content": "S"}
        assert len(history) == 3
        assert "Compacted 2 entries" in capsys.readouterr().err

    def test_compact_command_with_nothing_to_compact(self, monkeypatch, capsys):
        self._run_chat(monkeypatch, ["/compact"])
        assert "Nothing to compact" in capsys.readouterr().err

    def test_help_lists_compact(self, monkeypatch, capsys):
        self._run_chat(monkeypatch, ["/help"])
        assert "/compact" in capsys.readouterr().err

    def test_warns_when_context_seventy_percent_full(self, monkeypatch, capsys):
        agent = FakeChatAgent(usage=(5800, 8192))
        self._run_chat(monkeypatch, ["one", "n", "/help", "/history"], agent=agent)
        err = capsys.readouterr().err
        assert err.count("71% full") == 1  # once after the turn, not per slash command
        assert "/compact" in err

    def test_no_warning_below_seventy_percent(self, monkeypatch, capsys):
        agent = FakeChatAgent(usage=(5000, 8192))
        self._run_chat(monkeypatch, ["one", "n"], agent=agent)
        assert "% full" not in capsys.readouterr().err

    def test_session_autosaved_after_a_turn(self, monkeypatch, capsys):
        self._run_chat(monkeypatch, ["one", "n"])
        saved = self._saved_sessions()
        assert len(saved) == 1
        assert [t["content"] for t in saved[0]["history"]] == ["one", "planned"]
        assert saved[0]["model"] == "fake-model"
        err = capsys.readouterr().err
        assert f"codebud chat --resume {saved[0]['id']}" in err

    def test_no_session_file_when_nothing_was_said(self, monkeypatch):
        self._run_chat(monkeypatch, ["/help", "quit"])
        assert self._saved_sessions() == []

    def test_save_command_names_the_session(self, monkeypatch, capsys):
        self._run_chat(monkeypatch, ["one", "n", "/save my-work"])
        saved = self._saved_sessions()
        assert saved[0]["name"] == "my-work"
        err = capsys.readouterr().err
        assert "my-work" in err
        assert "codebud chat --resume my-work" in err

    def test_save_command_rejects_bad_name_and_chat_continues(self, monkeypatch, capsys):
        agent = self._run_chat(monkeypatch, ["/save bad/name", "two", "n"])
        assert "Invalid session name" in capsys.readouterr().err
        assert [t["content"] for t in agent.get_history()] == ["two", "planned"]

    def test_clear_is_checkpointed(self, monkeypatch):
        self._run_chat(monkeypatch, ["one", "n", "/clear"])
        assert self._saved_sessions()[0]["history"] == []

    # --- resume (backlog item 9) ---

    def _seed_session(self, history, model="saved-model", name=None):
        from codebud import sessions

        s = sessions.Session.create()
        if name:
            s.set_name(name)
        s.save(history, model=model)
        s.close()
        return s.id

    _OLD = [
        {"role": "user", "content": "list files"},
        {"role": "assistant", "content": "planned: command(list files)"},
    ]

    def test_resume_by_id_restores_history_and_keeps_saving_there(self, monkeypatch, capsys):
        monkeypatch.setattr("codebud.llm_stub.list_models", lambda: ["saved-model"])
        sid = self._seed_session(self._OLD)
        agent = self._run_chat(monkeypatch, ["two", "n"], extra_argv=["--resume", sid])

        assert [t["content"] for t in agent.get_history()][:2] == [
            "list files",
            "planned: command(list files)",
        ]
        saved = self._saved_sessions()
        assert len(saved) == 1  # same file, no new session
        assert [t["content"] for t in saved[0]["history"]][-2:] == ["two", "planned"]
        err = capsys.readouterr().err
        assert f"Resumed session {sid}" in err
        assert "list files" in err  # last exchange shown

    def test_resume_by_name(self, monkeypatch):
        monkeypatch.setattr("codebud.llm_stub.list_models", lambda: ["saved-model"])
        self._seed_session(self._OLD, name="work")
        agent = self._run_chat(monkeypatch, [], extra_argv=["--resume", "work"])
        assert agent.get_history() == self._OLD

    def test_resume_without_argument_picks_most_recent(self, monkeypatch):
        monkeypatch.setattr("codebud.llm_stub.list_models", lambda: ["saved-model"])
        stamps = iter(["2026-10-25T09:00:00+00:00", "2026-10-25T10:00:00+00:00"])
        monkeypatch.setattr(
            "codebud.sessions._now_iso", lambda: next(stamps, "2026-10-25T11:00:00+00:00")
        )
        self._seed_session([{"role": "user", "content": "older"}])
        self._seed_session([{"role": "user", "content": "newer"}])
        agent = self._run_chat(monkeypatch, [], extra_argv=["--resume"])
        assert agent.get_history() == [{"role": "user", "content": "newer"}]

    def test_resume_restores_saved_model_when_downloaded(self, monkeypatch):
        monkeypatch.setenv("OLLAMA_MODEL", "current-model")
        monkeypatch.setattr(
            "codebud.llm_stub.list_models", lambda: ["current-model", "saved-model"]
        )
        sid = self._seed_session(self._OLD, model="saved-model")
        self._run_chat(monkeypatch, [], extra_argv=["--resume", sid])
        import os

        assert os.environ["OLLAMA_MODEL"] == "saved-model"

    def test_resume_keeps_current_model_when_saved_one_missing(self, monkeypatch, capsys):
        monkeypatch.setenv("OLLAMA_MODEL", "current-model")
        monkeypatch.setattr("codebud.llm_stub.list_models", lambda: ["current-model"])
        sid = self._seed_session(self._OLD, model="gone-model")
        self._run_chat(monkeypatch, [], extra_argv=["--resume", sid])
        import os

        assert os.environ["OLLAMA_MODEL"] == "current-model"
        assert "gone-model" in capsys.readouterr().err

    def test_resume_unknown_session_fails(self, monkeypatch, capsys):
        with pytest.raises(SystemExit) as exc:
            self._run_chat(monkeypatch, [], extra_argv=["--resume", "nope"])
        assert exc.value.code == 1
        assert "No session" in capsys.readouterr().err

    def test_resume_session_in_use_fails(self, monkeypatch, capsys):
        from codebud import sessions

        busy = sessions.Session.create()
        busy.save(self._OLD, model="m")
        try:
            with pytest.raises(SystemExit) as exc:
                self._run_chat(monkeypatch, [], extra_argv=["--resume", busy.id])
            assert exc.value.code == 1
            assert "open in another" in capsys.readouterr().err
        finally:
            busy.close()

    def test_resume_with_nothing_saved_fails(self, monkeypatch, capsys):
        with pytest.raises(SystemExit):
            self._run_chat(monkeypatch, [], extra_argv=["--resume"])
        assert "No saved session" in capsys.readouterr().err


class TestSessionsSubcommand:
    @pytest.fixture(autouse=True)
    def _isolated_sessions(self, tmp_path, monkeypatch):
        monkeypatch.setattr("codebud.sessions.SESSIONS_DIR", str(tmp_path / "sessions"))

    def _run(self, argv):
        from codebud.cli import main as run_agent

        with patch.object(sys, "argv", ["codebud", "sessions", *argv]):
            run_agent.main()

    def test_no_sessions(self, capsys):
        self._run([])
        assert "No saved sessions" in capsys.readouterr().out

    def test_lists_sessions_with_resume_reference(self, capsys):
        from codebud import sessions

        s = sessions.Session.create()
        s.set_name("work")
        s.save([{"role": "user", "content": "list the python files"}], model="m")
        s.close()

        self._run([])
        out = capsys.readouterr().out
        assert s.id in out
        assert "work" in out
        assert "list the python files" in out

    def test_json_output(self, capsys):
        from codebud import sessions

        s = sessions.Session.create()
        s.save([{"role": "user", "content": "x"}], model="m")
        s.close()

        self._run(["--json"])
        parsed = json.loads(capsys.readouterr().out)
        assert parsed[0]["id"] == s.id
        assert parsed[0]["turns"] == 1


def _two_step_plan():
    plan = _good_plan()
    plan["plan"].append(
        {"id": "step_1", "description": "show cwd", "tool": "command", "args": {"cmd": "pwd"}}
    )
    return plan


class TestChatModes(_ChatHarness):
    """Mode commands and per-mode chat flow (backlog item 6)."""

    def test_mode_command_shows_current_and_choices(self, monkeypatch, capsys):
        self._run_chat(monkeypatch, ["/mode"])
        err = capsys.readouterr().err
        assert "Mode: ask" in err
        assert "ask, auto, full-auto, plan, edit" in err

    def test_mode_command_switches(self, monkeypatch, capsys):
        agent = self._run_chat(monkeypatch, ["/mode edit"])
        assert agent.get_mode() == "edit"
        assert "Mode: edit" in capsys.readouterr().err

    def test_unknown_mode_is_rejected(self, monkeypatch, capsys):
        agent = self._run_chat(monkeypatch, ["/mode yolo"])
        assert agent.get_mode() == "ask"
        assert "Unknown mode 'yolo'" in capsys.readouterr().err

    def test_mode_flag_sets_initial_mode(self, monkeypatch, capsys):
        agent = self._run_chat(monkeypatch, [], extra_argv=["--mode", "plan"])
        assert agent.get_mode() == "plan"
        assert "mode: plan" in capsys.readouterr().err

    def test_auto_mode_runs_all_steps_after_one_approval(self, monkeypatch):
        agent = FakeChatAgent(plan_by_message={"do it": _two_step_plan()})
        agent.set_mode("auto")
        self._run_chat(monkeypatch, ["do it", "y"], agent=agent)
        assert agent.executor.execute_plan.call_count == 2

    def test_plan_mode_never_offers_to_run(self, monkeypatch, capsys):
        agent = FakeChatAgent()
        agent.set_mode("plan")
        # If chat asked "Run this plan?", it would consume "y" and execute.
        self._run_chat(monkeypatch, ["do it", "y"], agent=agent)
        assert not agent.executor.execute_plan.called
        assert "plan mode" in capsys.readouterr().err

    def test_plan_refused_is_shown_as_an_error(self, monkeypatch, capsys):
        refused = {"status": "plan_refused", "error": "not allowed in edit mode: command."}
        agent = FakeChatAgent(plan_by_message={"run tests": refused})
        self._run_chat(monkeypatch, ["run tests"], agent=agent)
        assert "not allowed in edit mode: command." in capsys.readouterr().err

    def test_mode_is_saved_and_restored_on_resume(self, monkeypatch):
        monkeypatch.setattr("codebud.llm_stub.list_models", lambda: ["fake-model"])
        self._run_chat(monkeypatch, ["/mode edit", "one", "n"])
        saved = self._saved_sessions()[0]
        assert saved["mode"] == "edit"

        agent = self._run_chat(monkeypatch, [], extra_argv=["--resume", saved["id"]])
        assert agent.get_mode() == "edit"

    def test_auto_mode_stops_for_powerful_steps(self, monkeypatch, capsys):
        plan = _two_step_plan()
        plan["plan"][1]["args"]["cmd"] = "pytest -q"
        agent = FakeChatAgent(plan_by_message={"do it": plan})
        agent.set_mode("auto")
        # "y" approves the plan; "n" declines the powerful pytest step.
        self._run_chat(monkeypatch, ["do it", "y", "n"], agent=agent)
        assert agent.executor.execute_plan.call_count == 1
        assert "Powerful command" in capsys.readouterr().err

    def test_auto_mode_runs_powerful_step_when_confirmed(self, monkeypatch):
        plan = _two_step_plan()
        plan["plan"][1]["args"]["cmd"] = "pytest -q"
        agent = FakeChatAgent(plan_by_message={"do it": plan})
        agent.set_mode("auto")
        self._run_chat(monkeypatch, ["do it", "y", "y"], agent=agent)
        assert agent.executor.execute_plan.call_count == 2

    def test_auto_mode_asks_before_a_web_search(self, monkeypatch, capsys):
        agent = FakeChatAgent(plan_by_message={"find it": _web_search_plan()})
        agent.set_mode("auto")
        # "y" approves the plan; "n" declines the search.
        self._run_chat(monkeypatch, ["find it", "y", "n"], agent=agent)
        assert agent.executor.execute_plan.call_count == 0
        assert 'sends "latest ruff release" to DuckDuckGo' in capsys.readouterr().err

    def test_full_auto_searches_unattended_but_shows_the_query(self, monkeypatch, capsys):
        """User decision 2026-10-27: allowed and unattended in full-auto."""
        agent = FakeChatAgent(plan_by_message={"find it": _web_search_plan()})
        agent.set_mode("full-auto")
        self._run_chat(monkeypatch, ["find it"], agent=agent)
        assert agent.executor.execute_plan.call_count == 1
        assert 'query: "latest ruff release"' in capsys.readouterr().err

    def test_full_auto_runs_everything_without_any_prompt(self, monkeypatch):
        plan = _two_step_plan()
        plan["plan"][1]["args"]["cmd"] = "pytest -q"  # powerful, still no prompt
        agent = FakeChatAgent(plan_by_message={"do it": plan})
        agent.set_mode("full-auto")
        # No answers after the request: any prompt would hit EOF and skip/quit.
        self._run_chat(monkeypatch, ["do it"], agent=agent)
        assert agent.executor.execute_plan.call_count == 2


def test_error_hints_are_current():
    """Fix hints must not name removed paths or outdated timings/commands."""
    from codebud.cli import errors

    for _fragment, explanation, fix in errors._RULES:
        text = explanation + fix
        assert "config/harmless_commands.json" not in text
        assert "5 minutes" not in text


class TestChatProjectContext(_ChatHarness):
    """/rag, /sources, /index and --no-rag (phase 9, D4/D5)."""

    @staticmethod
    def _hit():
        from codebud.rag.chunker import Chunk
        from codebud.rag.search import Hit

        return Hit(Chunk("src/a.py", 10, 11, "alpha", "python", "def alpha(): ..."), 0.05)

    def test_help_lists_the_new_commands(self, monkeypatch, capsys):
        self._run_chat(monkeypatch, ["/help"])
        err = capsys.readouterr().err
        for cmd in ("/rag", "/sources", "/index"):
            assert cmd in err

    def test_sources_before_any_context(self, monkeypatch, capsys):
        self._run_chat(monkeypatch, ["/sources"])
        assert "no project context" in capsys.readouterr().err

    def test_sources_lists_the_last_requests_chunks(self, monkeypatch, capsys):
        agent = FakeChatAgent()
        agent.last_context = [self._hit()]
        self._run_chat(monkeypatch, ["/sources"], agent=agent)
        assert "src/a.py:10-11 (alpha)" in capsys.readouterr().err

    def test_rag_off_and_on(self, monkeypatch, capsys):
        agent = self._run_chat(monkeypatch, ["/rag off"])
        assert agent.get_rag() is False
        assert "Project context: off" in capsys.readouterr().err
        agent = self._run_chat(monkeypatch, ["/rag off", "/rag on"])
        assert agent.get_rag() is True

    def test_rag_rejects_other_arguments(self, monkeypatch, capsys):
        agent = self._run_chat(monkeypatch, ["/rag maybe"])
        assert agent.get_rag() is True
        assert "/rag on" in capsys.readouterr().err

    def test_rag_status_without_an_index(self, monkeypatch, tmp_path, capsys):
        monkeypatch.chdir(tmp_path)
        self._run_chat(monkeypatch, ["/rag"])
        err = capsys.readouterr().err
        assert "Project context: on" in err
        assert "No index" in err and "/index" in err

    def test_index_from_chat_builds_and_reports(self, monkeypatch, tmp_path, capsys):
        (tmp_path / "app.py").write_text("def main():\n    return 0\n")
        monkeypatch.chdir(tmp_path)
        self._run_chat(monkeypatch, ["/index", "/index status"])
        err = capsys.readouterr().err
        assert "Indexed 1 file (1 chunk)" in err
        assert "1 file, 1 chunk" in err

    def test_index_errors_do_not_end_the_chat(self, monkeypatch, tmp_path, capsys):
        from codebud.tools import embeddings

        monkeypatch.chdir(tmp_path)
        (tmp_path / "app.py").write_text("x = 1\n")
        monkeypatch.delenv("CODEBUD_EMBED_STUB", raising=False)

        def down(*_a, **_k):
            raise embeddings.requests.ConnectionError("refused")

        monkeypatch.setattr(embeddings.requests, "post", down)
        self._run_chat(monkeypatch, ["/index", "/help"])
        err = capsys.readouterr().err
        assert "Embedding model unavailable" in err
        assert "/sources" in err  # /help still ran afterwards

    def test_no_rag_flag_turns_context_off(self, monkeypatch):
        import os

        monkeypatch.setenv("CODEBUD_RAG", "1")
        self._run_chat(monkeypatch, ["quit"], extra_argv=["--no-rag"])
        assert os.environ["CODEBUD_RAG"] == "0"

    def test_chat_start_suggests_indexing_a_git_project(self, monkeypatch, tmp_path, capsys):
        import subprocess

        subprocess.run(["git", "-C", str(tmp_path), "init", "-q"], check=True)
        monkeypatch.chdir(tmp_path)
        self._run_chat(monkeypatch, ["quit"])
        assert "Tip: /index" in capsys.readouterr().err

    def test_hint_after_steps_wrote_files(self, monkeypatch, tmp_path, capsys):
        from codebud.rag import indexer

        (tmp_path / "app.py").write_text("x = 1\n")
        indexer.index_project(tmp_path)
        monkeypatch.chdir(tmp_path)
        write = {
            "status": "ok",
            "plan": [
                {
                    "id": "step_0",
                    "description": "write",
                    "tool": "file_write",
                    "args": {"path": "b.py", "content": "y = 2\n"},
                }
            ],
        }
        agent = FakeChatAgent(plan_by_message={"write it": write})
        self._run_chat(monkeypatch, ["write it", "y", "y"], agent=agent)
        assert "Files changed: /index" in capsys.readouterr().err


class TestChatPrivacy(_ChatHarness):
    def test_privacy_command_shows_and_switches(self, monkeypatch, capsys):
        import os

        monkeypatch.setenv("CODEBUD_PRIVACY", "private")
        self._run_chat(monkeypatch, ["/privacy", "/privacy locked", "/privacy open"])
        err = capsys.readouterr().err
        assert "Privacy: private" in err
        assert "Privacy: locked" in err
        assert "Unknown privacy level 'open'" in err
        assert os.environ["CODEBUD_PRIVACY"] == "locked"

    def test_help_lists_privacy(self, monkeypatch, capsys):
        self._run_chat(monkeypatch, ["/help"])
        assert "/privacy" in capsys.readouterr().err


class TestChatProfile(_ChatHarness):
    def test_profile_command_shows_and_switches(self, monkeypatch, capsys):
        import os

        agent = self._run_chat(monkeypatch, ["/profile", "/profile unattended", "/profile nope"])
        err = capsys.readouterr().err
        assert "Profile: default (ask, privacy private)" in err
        assert "Profile: unattended (full-auto, privacy trusted)" in err
        assert "no profile 'nope'" in err
        assert agent.get_mode() == "full-auto" and agent.policy_reloads == 1
        assert os.environ["CODEBUD_PRIVACY"] == "trusted"

    def test_profile_flag_sets_the_starting_mode(self, monkeypatch, capsys):
        # The fake agent is built before main() runs, so make_agent's
        # set_mode is replaced by this check of the active profile.
        from codebud import profiles

        self._run_chat(monkeypatch, ["quit"], extra_argv=[])
        assert profiles.active().name == "default"
        with patch.object(sys, "argv", ["codebud", "--profile", "unattended", "profiles"]):
            from codebud.cli import main as run_agent

            run_agent.main()
        assert profiles.active().name == "unattended"
        out = capsys.readouterr().out
        assert "* unattended" in out and "full-auto, privacy trusted" in out
