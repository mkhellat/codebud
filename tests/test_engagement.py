"""
tests/test_engagement.py

The engagement file (phase 10; D14d): authorized, scoped security work.
A TOML file names the client, ticket, in/out-of-scope targets and a time
window. Scope is checked warn-only -- out-of-scope or out-of-window raises
a reason the caller shows and confirms, it never blocks here.
"""

from datetime import datetime, timedelta, timezone

import pytest

from codebud_sec.engagement import EngagementError, Target, load


def _write(tmp_path, text):
    path = tmp_path / "acme.toml"
    path.write_text(text)
    return path


SAMPLE = """\
client = "Acme"
ticket = "PT-42"
in_scope = ["10.0.0.0/24", "app.acme.test"]
out_of_scope = ["10.0.0.1", "admin.acme.test"]
"""


# --- target matching --------------------------------------------------------


def test_cidr_matches_addresses_inside_it():
    t = Target.parse("10.0.0.0/24")
    assert t.matches("10.0.0.5")
    assert not t.matches("10.0.1.5")
    assert not t.matches("app.acme.test")  # a name is never inside a CIDR


def test_single_ip_is_a_host_route():
    assert Target.parse("10.0.0.1").matches("10.0.0.1")
    assert not Target.parse("10.0.0.1").matches("10.0.0.2")


def test_name_matches_itself_and_subdomains():
    t = Target.parse("acme.test")
    assert t.matches("acme.test")
    assert t.matches("app.acme.test")
    assert t.matches("App.Acme.Test.")  # case- and trailing-dot-insensitive
    assert not t.matches("notacme.test")
    assert not t.matches("acme.test.evil.com")
    assert not t.matches("10.0.0.5")  # an IP is never matched by a name


# --- loading and validation -------------------------------------------------


def test_load_reads_all_fields(tmp_path):
    e = load(_write(tmp_path, SAMPLE + 'operator = "me"\nstarts = 2026-10-01T00:00:00Z\n'))
    assert (e.client, e.ticket, e.operator) == ("Acme", "PT-42", "me")
    assert len(e.in_scope) == 2 and len(e.out_of_scope) == 2
    assert e.starts == datetime(2026, 10, 1, tzinfo=timezone.utc)
    assert "Acme PT-42" in e.header()


def test_in_scope_is_required(tmp_path):
    with pytest.raises(EngagementError, match="in_scope.*required"):
        load(_write(tmp_path, 'client = "Acme"\n'))


@pytest.mark.parametrize(
    "text, problem",
    [
        ('in_scope = ["x"]\nfoo = 1\n', "unknown key"),
        ('in_scope = ["x"]\nclient = 5\n', "'client' must be a string"),
        ('in_scope = "x"\n', "'in_scope' must be a list"),
        ("in_scope = [1, 2]\n", "must be a string"),
        ('in_scope = ["x"]\nstarts = "not-a-date"\n', "not a valid date"),
        ("in_scope = [\n", "not valid TOML"),
    ],
)
def test_bad_files_are_refused(tmp_path, text, problem):
    with pytest.raises(EngagementError, match=problem):
        load(_write(tmp_path, text))


def test_missing_file_is_an_error(tmp_path):
    with pytest.raises(EngagementError, match="cannot read"):
        load(tmp_path / "nope.toml")


# --- scope decisions (warn-only) --------------------------------------------


def test_covers_respects_inclusion_and_exclusion(tmp_path):
    e = load(_write(tmp_path, SAMPLE))
    assert e.covers("10.0.0.5")
    assert e.covers("app.acme.test")
    assert not e.covers("10.0.0.1")  # excluded host wins over the CIDR
    assert not e.covers("admin.acme.test")
    assert not e.covers("8.8.8.8")  # not listed at all


def test_out_of_scope_reason_names_the_target(tmp_path):
    e = load(_write(tmp_path, SAMPLE))
    assert e.out_of_scope_reason("10.0.0.5") is None
    reason = e.out_of_scope_reason("8.8.8.8")
    assert reason and "8.8.8.8" in reason and "acme.toml" in reason


def test_time_window_is_checked(tmp_path):
    now = datetime(2026, 10, 15, tzinfo=timezone.utc)
    e = load(
        _write(
            tmp_path,
            SAMPLE + "starts = 2026-10-10T00:00:00Z\nends = 2026-10-20T00:00:00Z\n",
        )
    )
    assert e.in_window(now)
    assert e.out_of_scope_reason("10.0.0.5", when=now) is None
    before = now - timedelta(days=10)
    assert not e.in_window(before)
    assert "window" in e.out_of_scope_reason("10.0.0.5", when=before)


def test_naive_times_are_treated_as_utc(tmp_path):
    e = load(_write(tmp_path, SAMPLE + "starts = 2026-10-01T00:00:00\n"))
    assert e.starts.tzinfo is not None


def test_no_window_is_always_open(tmp_path):
    assert load(_write(tmp_path, SAMPLE)).in_window()


def test_a_fully_qualified_exclusion_still_excludes(tmp_path):
    # Security review (low): "prod.example.com." in out_of_scope was ignored.
    path = _write(
        tmp_path,
        'in_scope = ["example.com."]\nout_of_scope = ["prod.example.com."]\n',
    )
    eng = load(path)
    assert eng.covers("app.example.com")
    assert not eng.covers("prod.example.com")
    assert not eng.covers("prod.example.com.")
    assert eng.out_of_scope_reason("prod.example.com")
