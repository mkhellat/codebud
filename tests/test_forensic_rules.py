"""
tests/test_forensic_rules.py

Argument rules for the imaging and metadata tools (phase 10, step 5
plan, PR 3b): dc3dd and ewfacquire work only on image files inside the
project, hashing and logging what they do (NIST SP 800-86 section 4.2.2,
ACPO principle 3); exiftool may only read -- an allow-list of read-only
options, since its format strings, -if and -config run Perl code.
"""

import pytest

from codebud.forensic import argument_refusal


@pytest.fixture
def project(tmp_path, monkeypatch):
    root = tmp_path / "case"
    (root / "evidence").mkdir(parents=True)
    (root / "evidence" / "disk.raw").write_bytes(b"\0" * 512)
    monkeypatch.chdir(root)
    return root


def _refused(cmd):
    import shlex

    words = shlex.split(cmd)
    return argument_refusal(words[0], words[1:])


# --- dc3dd ---------------------------------------------------------------


GOOD_DC3DD = "dc3dd if=evidence/disk.raw of=evidence/copy.raw hash=sha256 log=evidence/copy.log"


def test_dc3dd_on_an_image_in_the_project_with_hash_and_log(project):
    assert _refused(GOOD_DC3DD) is None
    assert _refused(GOOD_DC3DD.replace("log=", "hlog=")) is None
    assert _refused(GOOD_DC3DD + " hof=evidence/copy2.raw verb=on") is None


@pytest.mark.parametrize(
    ("cmd", "why"),
    [
        ("dc3dd if=/dev/sda of=evidence/x.raw hash=sha256 log=evidence/x.log", "a device"),
        ("dc3dd if=evidence/disk.raw of=/dev/sdb hash=sha256 log=evidence/x.log", "a device"),
        ("dc3dd if=evidence/disk.raw of=/tmp/x.raw hash=sha256 log=evidence/x.log", "outside"),
        ("dc3dd if=evidence/disk.raw of=../x.raw hash=sha256 log=evidence/x.log", "outside"),
        ("dc3dd if=evidence/disk.raw of=evidence/x.raw hash=sha256 log=/var/log/x", "outside"),
        ("dc3dd if=/etc/shadow of=evidence/x.raw hash=sha256 log=evidence/x.log", "outside"),
        ("dc3dd if=evidence/disk.raw of=evidence/x.raw log=evidence/x.log", "no hash"),
        ("dc3dd if=evidence/disk.raw of=evidence/x.raw hash=sha256", "no log"),
        ("dc3dd wipe=/dev/sda", "wipes"),
        ("dc3dd hwipe=evidence/disk.raw hash=sha256 log=evidence/x.log", "wipes"),
        ("dc3dd pat=00 of=evidence/x.raw hash=sha256 log=evidence/x.log", "pattern write"),
        (
            "dc3dd if=evidence/disk.raw of=evidence/x.raw hash=sha256 log=evidence/x.log rec=on",
            "unknown",
        ),
        ("dc3dd --help-me", "unknown"),
    ],
)
def test_dc3dd_refusals(project, cmd, why):
    assert _refused(cmd) is not None, f"{cmd} ({why})"


def test_dc3dd_rejects_a_symlink_out_of_the_project(project, tmp_path):
    (tmp_path / "outside").mkdir()
    (project / "evidence" / "link").symlink_to(tmp_path / "outside")
    cmd = "dc3dd if=evidence/disk.raw of=evidence/link/x.raw hash=sha256 log=evidence/x.log"
    assert _refused(cmd) is not None


# --- ewfacquire ----------------------------------------------------------


GOOD_EWF = (
    "ewfacquire -u -C CASE-1 -e analyst -E EV-7 -D 'web server disk' "
    "-d sha256 -l evidence/acq.log -t evidence/disk evidence/disk.raw"
)


def test_ewfacquire_unattended_with_metadata_hash_and_log(project):
    assert _refused(GOOD_EWF) is None
    assert _refused(GOOD_EWF + " -N 'imaged from a copy' -f encase6 -c fast") is None


@pytest.mark.parametrize(
    ("change", "why"),
    [
        (lambda c: c.replace(" -u", ""), "interactive: cannot answer in a step"),
        (lambda c: c.replace("-C CASE-1 ", ""), "no case number"),
        (lambda c: c.replace("-e analyst ", ""), "no examiner"),
        (lambda c: c.replace("-E EV-7 ", ""), "no evidence number"),
        (lambda c: c.replace("-D 'web server disk' ", ""), "no description"),
        (lambda c: c.replace("-d sha256 ", ""), "no sha256"),
        (lambda c: c.replace("-l evidence/acq.log ", ""), "no log"),
        (lambda c: c.replace("evidence/disk.raw", "/dev/sda"), "a device"),
        (lambda c: c.replace("-t evidence/disk", "-t /tmp/disk"), "outside"),
        (lambda c: c.replace("-l evidence/acq.log", "-l /tmp/acq.log"), "outside"),
        (lambda c: c + " -2 /mnt/usb/copy", "secondary target outside"),
        (lambda c: c + " -w", "zeroes on read error: writes"),
        (lambda c: c + " -Z", "unknown"),
    ],
)
def test_ewfacquire_refusals(project, change, why):
    assert _refused(change(GOOD_EWF)) is not None, why


# --- exiftool ------------------------------------------------------------


@pytest.mark.parametrize(
    "cmd",
    [
        "exiftool evidence/disk.raw",
        "exiftool -a -u -G1 -s evidence/",
        "exiftool -j -n -r -ext jpg evidence/",
        "exiftool -csv -r evidence/",
    ],
)
def test_exiftool_reading_is_allowed(project, cmd):
    assert _refused(cmd) is None, cmd


@pytest.mark.parametrize(
    ("cmd", "why"),
    [
        ("exiftool -Artist=x evidence/a.jpg", "writes a tag"),
        ("exiftool -all= evidence/a.jpg", "deletes tags"),
        ("exiftool -overwrite_original -Artist=x evidence/a.jpg", "overwrites"),
        ("exiftool -tagsFromFile a.jpg b.jpg", "copies tags"),
        ("exiftool -o out.jpg a.jpg", "writes a file"),
        ("exiftool -p '${filename;system(\"id\")}' a.jpg", "runs Perl"),
        ("exiftool -if '$Make eq 1' a.jpg", "runs Perl"),
        ("exiftool -config evil.pl a.jpg", "loads Perl"),
        ("exiftool -@ args.txt", "arguments from a file"),
        ("exiftool '-filename<CreateDate' a.jpg", "renames"),
        ("exiftool -geotag track.gpx a.jpg", "writes"),
        ("exiftool -ARTIST+=x a.jpg", "writes, any case"),
    ],
)
def test_exiftool_anything_but_reading_is_refused(project, cmd, why):
    assert _refused(cmd) is not None, f"{cmd} ({why})"


def test_other_programs_have_no_rules_here():
    assert argument_refusal("ls", ["-la"]) is None


# --- through the safety engine ---------------------------------------------


def _engine():
    from codebud.safety import SafetyEngine

    engine = SafetyEngine()
    engine.harmless_commands = ["exiftool"]
    engine.powerful_commands = ["dc3dd", "ewfacquire", "timeout"]
    return engine


def test_the_engine_applies_the_rules(project):
    engine = _engine()
    step = lambda cmd: {"tool": "command", "args": {"cmd": cmd}}  # noqa: E731
    assert engine.refusal_reason(step(GOOD_DC3DD)) is None
    reason = engine.refusal_reason(
        step("dc3dd if=/dev/sda of=evidence/x hash=sha256 log=evidence/l")
    )
    assert reason is not None and "device" in reason
    assert engine.refusal_reason(step("exiftool -Artist=x evidence/a.jpg")) is not None


def test_the_rules_hold_behind_a_wrapper(project):
    cmd = "timeout 600 dc3dd if=/dev/sda of=evidence/x hash=sha256 log=evidence/l"
    assert _engine().refusal_reason({"tool": "command", "args": {"cmd": cmd}}) is not None
