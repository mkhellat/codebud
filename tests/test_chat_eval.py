"""
tests/test_chat_eval.py

scripts/chat_eval.py: the scenario file is well formed, and each check kind
passes and fails correctly on synthetic turn records.  No model, no network.
"""

import importlib.util
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
_spec = importlib.util.spec_from_file_location("chat_eval", SCRIPTS / "chat_eval.py")
chat_eval = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(chat_eval)


def _passed(check, turn, project=Path(".")):
    ((_, ok),) = chat_eval.evaluate([check], turn, project)
    return ok


def _turn(text="", steps=(), status="chat_reply"):
    return {
        "final_text": text,
        "status": status,
        "steps": [{"tool": "command", "target": s} for s in steps],
    }


class TestScenario:
    def test_valid_and_complete(self):
        scenario = chat_eval.load_scenario()
        assert scenario["fixture"]
        assert len(scenario["turns"]) == 10

    def test_every_turn_has_known_checks(self):
        for turn in chat_eval.load_scenario()["turns"]:
            assert turn["request"]
            assert turn["checks"]

            def kinds(checks):
                for check in checks:
                    for kind, arg in check.items():
                        yield kind
                        if kind == "any_of":
                            for sub in arg:
                                yield from kinds([sub])

            for kind in kinds(turn["checks"]):
                assert kind in chat_eval.KNOWN_CHECKS

    def test_fixture_is_created(self, tmp_path):
        chat_eval.make_fixture(tmp_path, chat_eval.load_scenario()["fixture"])
        assert (tmp_path / "abdul" / "notes.txt").is_file()
        assert (tmp_path / "zz" / "todo.txt").is_file()

    def test_unknown_check_raises(self):
        with pytest.raises(ValueError):
            chat_eval.evaluate([{"bogus": 1}], _turn(), Path("."))


class TestChecks:
    def test_text_not_empty(self):
        assert _passed({"text_not_empty": True}, _turn("hello"))
        assert not _passed({"text_not_empty": True}, _turn("  "))

    def test_text_contains_any_is_case_insensitive(self):
        check = {"text_contains_any": ["GPU", "budget"]}
        assert _passed(check, _turn("A gpu purchase"))
        assert not _passed(check, _turn("nothing"))

    def test_text_contains_all(self):
        check = {"text_contains_all": ["abdul", "Prof"]}
        assert _passed(check, _turn("ABDUL and prof"))
        assert not _passed(check, _turn("abdul only"))

    def test_text_excludes(self):
        check = {"text_excludes": ["Qwen"]}
        assert _passed(check, _turn("I am codebud"))
        assert not _passed(check, _turn("I'm qwen"))

    def test_step_mentions_any(self):
        check = {"step_mentions_any": ["notes.txt"]}
        assert _passed(check, _turn(steps=["cat abdul/NOTES.txt"]))
        assert not _passed(check, _turn(steps=["ls"]))
        assert not _passed(check, _turn())

    def test_step_excludes(self):
        check = {"step_excludes": [".py"]}
        assert _passed(check, _turn(steps=["cat log_calc.c"]))
        assert not _passed(check, _turn(steps=["python fact.py"]))

    def test_no_step_contains(self):
        check = {"no_step_contains": "|"}
        assert _passed(check, _turn(steps=["ls"]))
        assert not _passed(check, _turn(steps=["echo 2+2 | bc"]))

    def test_not_refused(self):
        assert _passed({"not_refused": True}, _turn(status="chat_reply"))
        assert not _passed({"not_refused": True}, _turn(status="plan_refused"))

    def test_any_of(self):
        check = {"any_of": [{"step_mentions_any": ["x.c"]}, {"text_contains_any": ["main"]}]}
        assert _passed(check, _turn("the main function"))
        assert _passed(check, _turn(steps=["cat x.c"]))
        assert not _passed(check, _turn("nothing"))

    def test_file_exists_with(self, tmp_path):
        check = {"file_exists_with": {"path": "log_calc.c", "contains": ["main", "log"]}}
        assert not _passed(check, _turn(), tmp_path)
        (tmp_path / "log_calc.c").write_text("int MAIN() { return 0; }")
        assert not _passed(check, _turn(), tmp_path)
        (tmp_path / "log_calc.c").write_text("int main() { return log(2); }")
        assert _passed(check, _turn(), tmp_path)


class TestStepRecord:
    def test_command_and_file_targets(self):
        cmd = {"tool": "command", "args": {"cmd": "ls"}}
        read = {"tool": "file_read", "args": {"path": "a.txt"}}
        assert chat_eval._step_record(cmd)["target"] == "ls"
        assert chat_eval._step_record(read)["target"] == "a.txt"
