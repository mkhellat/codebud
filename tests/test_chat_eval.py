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


def _kinds(checks):
    for check in checks:
        for kind, arg in check.items():
            yield kind
            if kind == "any_of":
                for sub in arg:
                    yield from _kinds([sub])


class TestNewChecks:
    def test_text_contains_model(self):
        turn = {**_turn("I am Qwen2.5-Coder"), "model": "qwen2.5-coder:3b"}
        assert _passed({"text_contains_model": True}, turn)
        turn = {**_turn("I am the llama2 model"), "model": "llama2:7b"}
        assert _passed({"text_contains_model": True}, turn)
        turn = {**_turn("I am Codebud"), "model": "llama2:7b"}
        assert not _passed({"text_contains_model": True}, turn)

    def test_no_unread_claims(self):
        rule = {"file": "abdul/trac.analysis", "keywords": ["gateway", "recommend"]}
        check = {"no_unread_claims": rule}
        assert _passed(check, _turn("two files about a VPN"))
        assert not _passed(check, _turn("It says the Gateway is wrong"))
        assert _passed(check, _turn("the gateway is wrong", steps=["cat abdul/trac.analysis"]))
        assert not _passed(check, _turn("the gateway is wrong", steps=["cat abdul/trac.log"]))
        reads = _turn("gateway")
        reads["steps"] = [{"tool": "file_read", "target": "abdul/trac.analysis"}]
        assert _passed(check, reads)

    def test_no_unread_claims_list_of_rules(self):
        rules = [
            {"file": "a.txt", "keywords": ["alpha"]},
            {"file": "b.txt", "keywords": ["beta"]},
        ]
        check = {"no_unread_claims": rules}
        assert _passed(check, _turn("alpha", steps=["cat a.txt"]))
        assert not _passed(check, _turn("alpha and beta", steps=["cat a.txt"]))

    def test_no_steps(self):
        assert _passed({"no_steps": True}, _turn("sorry"))
        assert not _passed({"no_steps": True}, _turn("sorry", steps=["ls"], status="ok"))

    def test_no_shell_refused(self):
        check = {"no_shell_refused": True}
        assert _passed(check, _turn("8 files"))
        assert not _passed(check, _turn("steps run without a shell"))


class TestLogsScenario:
    path = SCRIPTS / "chat_eval_logs.json"

    def test_loads_with_known_checks(self):
        scenario = chat_eval.load_scenario(self.path)
        assert len(scenario["turns"]) == 7
        for turn in scenario["turns"]:
            assert turn["request"] and turn["checks"]
            for kind in _kinds(turn["checks"]):
                assert kind in chat_eval.KNOWN_CHECKS

    def test_generated_fixture(self, tmp_path):
        chat_eval.make_fixture(tmp_path, chat_eval.load_scenario(self.path)["fixture"])
        files = [p for p in tmp_path.rglob("*") if p.is_file()]
        assert len(files) == 8
        log = tmp_path / "abdul" / "trac.log"
        assert 200_000 <= log.stat().st_size <= 400_000
        lines = log.read_text().splitlines()
        tail = "\n".join(lines[-6:])
        assert "AuthError_t==3" in tail
        assert "TrBaseFlow::FinishStep: Step failed" in tail
        assert "certificate expired for vpn.example.org" in tail
        assert "AuthError" not in "\n".join(lines[:-6])

    def test_make_fixture_repeat(self, tmp_path):
        spec = {"x/f.log": {"repeat": "ab\n", "times": 3, "then": "end\n"}}
        chat_eval.make_fixture(tmp_path, spec)
        assert (tmp_path / "x" / "f.log").read_text() == "ab\nab\nab\nend\n"


class TestArguments:
    def test_scenario_default_and_override(self):
        parser = chat_eval.build_parser()
        assert parser.parse_args(["m"]).scenario == chat_eval.SCENARIO
        args = parser.parse_args(["--scenario", "x.json", "m"])
        assert args.scenario == Path("x.json")
