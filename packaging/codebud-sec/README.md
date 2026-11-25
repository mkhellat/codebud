# codebud-sec

The security layer for [codebud](https://github.com/mkhellat/codebud), a
privacy-first local coding agent. It adds engagements, a scope guard, a
network fence and a tamper-evident evidence log.

For authorized security work only: incident response on hosts you own, and
penetration tests you are contracted to run.

    pip install codebud-sec

This also installs the matching version of codebud.

    codebud-sec chat|run|plan --engagement FILE
    codebud-sec evidence add|verify|show|close
    codebud-sec pin

Two profiles ship with it:

- `security-ir`: incident response on your own hosts.
- `security-pentest`: reconnaissance and scanning only.

The network fence needs bubblewrap (`bwrap`), pasta (from passt) and `nft`
installed on the host.

See the chapter "Security Work" in the manual:
https://github.com/mkhellat/codebud

Licensed under the GNU GPL, version 3 or later.
