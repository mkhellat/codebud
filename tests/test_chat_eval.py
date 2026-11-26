"""
tests/test_chat_eval.py

scripts/chat_eval.py: the scenario file is well formed, and each check kind
passes and fails correctly on synthetic turn records.  No model, no network.
"""

import os
import sys
import time
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
# On sys.path so that the spawned child process of run_model can import it.
sys.path.insert(0, str(SCRIPTS))
import chat_eval  # noqa: E402


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

    def test_turn_timeout_default_and_override(self):
        parser = chat_eval.build_parser()
        assert parser.parse_args(["m"]).turn_timeout == 300
        assert parser.parse_args(["--turn-timeout", "2.5", "m"]).turn_timeout == 2.5

    def test_sampling_and_repeat_defaults_and_overrides(self):
        parser = chat_eval.build_parser()
        args = parser.parse_args(["m"])
        assert (args.temperature, args.seed, args.repeat) == (0.0, 42, 1)
        args = parser.parse_args(["--temperature", "0.7", "--seed", "5", "--repeat", "3", "m"])
        assert (args.temperature, args.seed, args.repeat) == (0.7, 5, 3)

    def test_repeat_must_be_positive(self):
        with pytest.raises(SystemExit):
            chat_eval.main(["--repeat", "0", "m"])


def env_turn(agent, request):
    """Fake play_turn: reports the sampling environment it was started with."""
    text = f"{os.environ.get('CODEBUD_TEMPERATURE')}/{os.environ.get('CODEBUD_SEED')}"
    return {"plan": {"status": "chat_reply", "reply": text}, "steps": []}


class TestRepeat:
    scenario = {
        "fixture": {"a.txt": "x"},
        "turns": [{"request": "only", "checks": [{"text_contains_any": ["0.5/9"]}]}],
    }

    def test_child_gets_temperature_and_seed(self):
        records = chat_eval.run_model(
            "m", self.scenario, 30, turn_runner=env_turn, temperature=0.5, seed=9, run=3
        )
        assert records[0]["final_text"] == "0.5/9"
        assert records[0]["run"] == 3
        assert all(c["passed"] for c in records[0]["checks"])

    def test_unset_leaves_the_environment_alone(self, monkeypatch):
        monkeypatch.delenv("CODEBUD_TEMPERATURE", raising=False)
        monkeypatch.delenv("CODEBUD_SEED", raising=False)
        records = chat_eval.run_model("m", self.scenario, 30, turn_runner=env_turn)
        assert records[0]["final_text"] == "None/None"
        assert records[0]["run"] == 1

    def test_main_repeats_with_run_indices_and_summary(self, tmp_path, monkeypatch, capsys):
        import json

        path = tmp_path / "scenario.json"
        path.write_text(json.dumps(self.scenario))
        real = chat_eval.run_model
        seen = []

        def fake(m, sc, t, progress=None, **kw):
            seen.append(kw)
            records = real(m, sc, t, turn_runner=env_turn, progress=progress, **kw)
            if kw["run"] != 2:  # runs 1 and 3 fail their check
                for c in records[0]["checks"]:
                    c["passed"] = False
            return records

        monkeypatch.setattr(chat_eval, "run_model", fake)
        argv = ["--repeat", "3", "--temperature", "0.5", "--seed", "9", "--scenario", str(path)]
        assert chat_eval.main([*argv, "--json", "m"]) == 0
        out, _ = capsys.readouterr()
        rows = [json.loads(line) for line in out.splitlines()]
        assert [r["run"] for r in rows] == [1, 2, 3]
        assert [k["run"] for k in seen] == [1, 2, 3]
        assert all(k["temperature"] == 0.5 and k["seed"] == 9 for k in seen)
        assert chat_eval.main([*argv, "m"]) == 0
        out, _ = capsys.readouterr()
        assert "m: runs 0/1 1/1 0/1 (min 0, mean 0.3, max 1)" in out

    def test_summarize_single_run_keeps_the_old_line(self):
        rec = {"checks": [{"passed": True}, {"passed": False}], "seconds": 4, "status": "ok"}
        assert chat_eval.summarize("m", [[rec]]) == "m: 1/2 checks passed, 4 s"

    def test_summarize_many_runs(self):
        def run(passed, total, seconds):
            checks = [{"passed": i < passed} for i in range(total)]
            return [{"checks": checks, "seconds": seconds, "status": "ok"}]

        line = chat_eval.summarize("M", [run(15, 18, 200), run(15, 18, 210), run(14, 18, 220)])
        assert line == "M: runs 15/18 15/18 14/18 (min 14, mean 14.7, max 15), 630 s"


def slow_second_turn(agent, request):
    """Fake play_turn: records where it runs; turn 2 never finishes in time."""
    Path(os.environ["EVAL_MARK"], "cwd").write_text(os.getcwd())
    if request == "second":
        Path(os.environ["EVAL_MARK"], "pid").write_text(str(os.getpid()))
        time.sleep(60)
    return {"plan": {"status": "chat_reply", "reply": "ok " + request}, "steps": []}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class TestTurnTimeout:
    scenario = {
        "fixture": {"a.txt": "x"},
        "turns": [
            {"request": "first", "checks": [{"text_contains_any": ["ok first"]}]},
            {"request": "second", "checks": [{"text_not_empty": True}, {"no_steps": True}]},
            {"request": "third", "checks": [{"text_not_empty": True}]},
            {"request": "fourth", "checks": [{"text_not_empty": True}]},
        ],
    }

    def test_timeout_skips_rest_and_cleans_up(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EVAL_MARK", str(tmp_path))
        seen = []
        records = chat_eval.run_model(
            "m:1b", self.scenario, 3, turn_runner=slow_second_turn, progress=seen.append
        )
        assert [r["status"] for r in records] == ["chat_reply", "timeout", "skipped", "skipped"]
        assert [r["turn"] for r in records] == [1, 2, 3, 4]
        assert all(c["passed"] for c in records[0]["checks"])
        for rec in records[1:]:
            assert rec["final_text"] == ""
            assert rec["checks"] and not any(c["passed"] for c in rec["checks"])
        assert len(records[1]["checks"]) == 2
        assert records[1]["seconds"] == 3
        assert seen == records
        pid = int(Path(tmp_path, "pid").read_text())
        deadline = time.monotonic() + 5
        while _alive(pid) and time.monotonic() < deadline:
            time.sleep(0.1)
        assert not _alive(pid)
        cwd = Path(Path(tmp_path, "cwd").read_text())
        assert not cwd.exists()
        assert not cwd.parent.exists()

    def test_no_timeout_plays_everything(self, tmp_path, monkeypatch):
        monkeypatch.setenv("EVAL_MARK", str(tmp_path))
        scenario = {
            **self.scenario,
            "turns": self.scenario["turns"][:1] + self.scenario["turns"][2:],
        }
        records = chat_eval.run_model("m", scenario, 30, turn_runner=slow_second_turn)
        assert [r["status"] for r in records] == ["chat_reply"] * 3

    def test_main_reports_timeout_and_progress_on_stderr(self, tmp_path, monkeypatch, capsys):
        import json

        monkeypatch.setenv("EVAL_MARK", str(tmp_path))
        path = tmp_path / "scenario.json"
        path.write_text(json.dumps(self.scenario))
        real = chat_eval.run_model
        monkeypatch.setattr(
            chat_eval,
            "run_model",
            lambda m, sc, t, progress=None, **kw: real(
                m, sc, t, turn_runner=slow_second_turn, progress=progress, **kw
            ),
        )
        monkeypatch.setattr(
            sys, "argv", ["chat_eval", "--turn-timeout", "3", "--scenario", str(path), "m"]
        )
        assert chat_eval.main() == 0
        out, err = capsys.readouterr()
        assert "m: 1/5 checks passed" in out
        assert "1 turn timed out" in out
        assert "turn 1: chat_reply" in err
        assert "turn 2: timeout" in err
        assert "turn 3: skipped" in err
        assert "turn 1" not in out
