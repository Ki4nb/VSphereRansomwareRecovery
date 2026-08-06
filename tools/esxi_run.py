#!/usr/bin/env python3
"""
Run a command, or upload and run a script, on an ESXi host or a rescue VM.

Standard library only. SSH work is delegated to the system `ssh`/`scp`, which
exist on Windows 10+, macOS and every Linux, so nothing needs installing on the
workstation and nothing needs installing on the target.

    python3 esxi_run.py HOST --cmd 'uname -a'
    python3 esxi_run.py HOST --script tools/esxi-recon.sh
    python3 esxi_run.py HOST --script tools/recover-easy-path.sh --args '--commit /dev/sda'
    python3 esxi_run.py HOST --script tools/batch-repair.sh --args '/tmp/manifest.txt --commit' --detach

Authentication is by key. Generate a dedicated keypair for the engagement rather
than reusing an admin key, and revoke it when you are done:

    ssh-keygen -t ed25519 -f ~/.ssh/ir_key -C incident-response
    ssh-copy-id -i ~/.ssh/ir_key root@HOST

For a SystemRescue VM, where the root password is set by hand at the console on
every boot and there is no key yet, either paste the key in first or use
--password, which requires `sshpass` on the workstation.

Two behaviours here exist because of specific failures:

--detach runs the remote command under `setsid nohup`. A long job tied to the
SSH exec channel dies with SIGHUP if that channel drops, which killed a 32-disk
repair batch at disk 20 with no error on either end. Detached, it survives, and
its output goes to a log you can poll.

Uploads are normalised to LF and stripped of any UTF-8 BOM. A BOM in a shebang
makes /bin/sh report "not found" for an interpreter that is plainly there, and
PowerShell's `-Encoding utf8` writes one by default.
"""

import argparse
import os
import shlex
import subprocess
import sys
import tempfile

# Rescue ISOs regenerate their SSH host key on every boot, and a compromised
# host's key is not a trust anchor anyway, so host-key checking is off by design.
# The remote end is identified by the operator, not by a cached fingerprint.
SSH_OPTS = [
    "-o", "StrictHostKeyChecking=no",
    "-o", "UserKnownHostsFile=" + os.devnull,
    "-o", "GlobalKnownHostsFile=" + os.devnull,
    "-o", "LogLevel=ERROR",
    "-o", "ConnectTimeout=20",
    # Older ESXi builds offer only ssh-rsa; harmless to allow on newer ones.
    "-o", "PubkeyAcceptedKeyTypes=+ssh-rsa",
    "-o", "HostKeyAlgorithms=+ssh-rsa",
]


def _prefix(password):
    """sshpass wrapper, when a password is unavoidable."""
    if not password:
        return []
    from shutil import which
    if not which("sshpass"):
        sys.exit("--password needs `sshpass` on this machine; use key auth instead")
    return ["sshpass", "-p", password]


def ssh(host, command, user="root", key=None, port=22, password=None, timeout=None):
    cmd = _prefix(password) + ["ssh"] + SSH_OPTS
    if key:
        cmd += ["-i", key]
    cmd += ["-p", str(port), f"{user}@{host}", command]
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)


def upload(host, local, remote, user="root", key=None, port=22, password=None):
    """Copy a file up, normalised to LF and with any BOM removed."""
    with open(local, "rb") as fh:
        body = fh.read()
    if body.startswith(b"\xef\xbb\xbf"):
        body = body[3:]
    body = body.replace(b"\r\n", b"\n")

    tmp = tempfile.NamedTemporaryFile(delete=False)
    try:
        tmp.write(body)
        tmp.close()
        cmd = _prefix(password) + ["scp"] + SSH_OPTS
        if key:
            cmd += ["-i", key]
        cmd += ["-P", str(port), tmp.name, f"{user}@{host}:{remote}"]
        r = subprocess.run(cmd, capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"upload failed: {r.stderr.strip()}")
    finally:
        os.unlink(tmp.name)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("host")
    ap.add_argument("--user", default="root")
    ap.add_argument("--key", help="private key (recommended)")
    ap.add_argument("--password", help="needs sshpass; prefer --key")
    ap.add_argument("--port", type=int, default=22)
    ap.add_argument("--cmd", help="command to run")
    ap.add_argument("--script", help="local script to upload and run")
    ap.add_argument("--upload", nargs=2, metavar=("LOCAL", "REMOTE"),
                    help="copy a file up and stop")
    ap.add_argument("--args", default="", help="arguments for the uploaded script")
    ap.add_argument("--interp", default="sh", help="interpreter for the script (sh, bash, python3)")
    ap.add_argument("--detach", action="store_true",
                    help="run under setsid nohup so an SSH drop cannot kill it")
    ap.add_argument("--log", default=None, help="remote log path when detaching")
    ap.add_argument("--timeout", type=int, default=None, help="seconds")
    args = ap.parse_args()

    if not args.cmd and not args.script and not args.upload:
        ap.error("give --cmd, --script or --upload")

    conn = dict(user=args.user, key=args.key, port=args.port, password=args.password)

    if args.upload:
        upload(args.host, args.upload[0], args.upload[1], **conn)
        print(f"  uploaded {args.upload[1]}", file=sys.stderr)
        return 0

    if args.script:
        name = os.path.basename(args.script)
        remote = f"/tmp/{name}"
        upload(args.host, args.script, remote, **conn)
        print(f"  uploaded {remote}", file=sys.stderr)
        command = f"{args.interp} {shlex.quote(remote)} {args.args}".strip()
    else:
        command = args.cmd

    if args.detach:
        log = args.log or "/tmp/detached.out"
        command = (f"cd /tmp && setsid nohup {command} > {shlex.quote(log)} 2>&1 "
                   f"< /dev/null & echo started")
        # The channel may not close even with the job detached, so do not wait
        # on it indefinitely - the job is running regardless of what we see here.
        try:
            r = ssh(args.host, command, timeout=30, **conn)
            print(r.stdout, end="")
        except subprocess.TimeoutExpired:
            pass
        print(f"  detached; poll {log} on {args.host}", file=sys.stderr)
        return 0

    r = ssh(args.host, command, timeout=args.timeout, **conn)
    print(r.stdout, end="")
    if r.stderr.strip():
        print("--- stderr ---", file=sys.stderr)
        print(r.stderr, end="", file=sys.stderr)
    return r.returncode


if __name__ == "__main__":
    sys.exit(main())
