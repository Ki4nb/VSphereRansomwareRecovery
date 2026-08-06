# The environment will lie to you

Filesystem problems are in [`recovery-runbook.md`](recovery-runbook.md) §8.
This file is about the layer underneath: the ESXi shell, the workstation, and
the SSH connection between them. Everything here produced a **confidently wrong
answer** rather than an error, which is what makes it worth writing down.

Symptom first, because that is what you will be searching for.

---

## ESXi's shell is busybox, and smaller than you think

### A size comparison silently gives the wrong answer above 2 GiB

**Symptom.** A 10 GiB disk is reported as entirely encrypted and written off.

```sh
[ "$size" -le 536870912 ] && echo "under 512 MiB - total loss"
```

`test`/`[` on ESXi is **32-bit**. `$(( ))` is 64-bit. Any byte-size comparison
above 2^31 wraps, and the wrap is silent:

```sh
X=10737418240
[ "$X" -le 536870912 ] && echo LESS     # prints LESS. It is not less.
echo $(( X > 536870912 ))               # prints 1. Correct.
```

**Fix.** Do the comparison inside the arithmetic expansion:

```sh
[ "$(( size <= 536870912 ))" = "1" ] && echo "total loss"
```

This one is worth grepping your own scripts for. It decides whether a disk gets
recovered or abandoned.

### `tr` does not exist

**Symptom.** A variable that should hold `55aa` is empty, so a partition-table
check fails and reports the disk as damaged.

```sh
MBR=$(dd ... | od -An -tx1 -j 510 -N 2 | tr -d ' \n')   # tr: not found
```

**Fix.** `awk` and `sed` are both present:

```sh
MBR=$(dd ... | od -An -tx1 -j 510 -N 2 | awk '{printf "%s%s",$1,$2}')
```

### `base64` does not exist either

Pushing a script by base64-decoding it on the far end works on a Linux rescue
VM and fails on ESXi. Use `scp`, or `python3` — which *is* present, and is what
the Python tooling assumes.

Present and usable on a stock host: `sh` (busybox ash), `awk`, `sed`, `find`,
`od`, `dd`, `python3`, `vim-cmd`, `esxcli`, `vmkfstools`, `sgdisk`, `partedUtil`.

---

## The workstation

### `line 1: #!/bin/sh: not found`

For a file whose first line is obviously `#!/bin/sh`. Two causes, same message:

**A UTF-8 BOM.** Windows PowerShell 5.1 writes one from `Set-Content -Encoding
utf8` and `Out-File`. `/bin/sh` reads it as part of the interpreter path.

```powershell
[IO.File]::WriteAllText($path, $body, (New-Object Text.UTF8Encoding($false)))
```

**CRLF line endings.** Same message, same confusion. Normalise before upload;
`.gitattributes` in this repo forces LF on everything that runs on a target.

### Windows OpenSSH cannot take a password

And a freshly booted SystemRescue VM has a password and no key. Either paste a
key in on the first connection, or use `tools/windows/rsh.ps1`, which wraps
Posh-SSH. Prefer keys: generate a dedicated pair for the engagement and revoke
it afterwards.

---

## SSH itself

### A long job dies part-way with no error at either end

**Symptom.** A 32-disk repair batch stopped at disk 20. No error, no traceback,
no entry in any log. The log file simply stopped growing.

The job was tied to the SSH exec channel. When that channel dropped, the remote
process got SIGHUP and died. The client eventually reported
`An established connection was aborted by the server`, long after the fact.

**Fix.** Detach anything that runs for more than a few minutes, and poll it:

```sh
setsid nohup bash /tmp/batch-repair.sh /tmp/manifest.txt --commit \
    > /tmp/batch.out 2>&1 < /dev/null &
```

Make the job resumable too. `batch-repair.sh` skips disks whose log already
contains `=== DONE ===`, so a relaunch continues instead of redoing an hour.

### `pgrep -f` says the job is running when it is not

**Symptom.** You check whether a batch is still alive, are told yes, and wait
twenty minutes for a process that died before you asked.

```sh
pgrep -f batch-repair.sh      # matches the shell running your own check
```

The pattern appears in the command line of the SSH command doing the checking.

**Fix.** The bracket trick, which makes the pattern not match itself:

```sh
ps aux | grep -c "[b]atch-repair.sh"
```

Better: check for evidence of work — file timestamps, log line counts — rather
than for a process.

---

## Shell scripts

### The last record of a file is silently ignored

**Symptom.** A 36-line manifest processes 35 disks. Worse, in this case the
missing line also corrupted a controller's identity signature, so a further six
disks were skipped and the failure looked like a hardware mapping problem.

```sh
while read -r a b c; do ...; done < file    # drops the final line if the
                                            # file has no trailing newline
```

**Fix.**

```sh
while read -r a b c || [ -n "$a" ]; do ...; done < file
```

and make whatever generates the file end it with a newline.

### A script reports failure after succeeding

**Symptom.** A repair completes, the log ends in `=== DONE ===`, and the caller
records it as FAILED.

The script's exit status is the status of its last command. If that is a
conditional:

```sh
[ -n "$MACPIN" ] && say "Set the VM MAC to $MACPIN"
```

then on any guest without a MAC pin the test is false, and the whole script
exits non-zero after doing everything correctly.

**Fix.** End scripts that are called by other scripts with an explicit `exit 0`,
and prefer a positive marker in the output — `grep -l '=== DONE ==='` — over an
exit code as the record of success.

---

## The hypervisor

### A file on the datastore reads as empty

**Symptom.** `dd` against a flat file returns no data. Not an error — no data.
Every check that inspects the disk concludes it is unreadable and refuses to
continue.

The file is **VMFS-locked** because a VM that has it attached is powered on.

**Fix.** Power off whatever holds it — usually the rescue VM you were just
using. Then re-check.

The same effect quietly degrades your inventory. `babuk_fleetscan.py` cannot
open a locked file, so it drops that disk from the CSV without saying so, and
the fleet appears to shrink as you recover and power on more guests. Use
`remaining-report.sh`, which reads power state before it reads the disk.

### `sgdisk` refuses to write a partition table

**Symptom.**

```
Problem: MBR partitions 3 and 4 overlap!
Warning! An error was reported when writing the partition table!
```

`sgdisk` is parsing the garbage MBR left behind in the encrypted head and
refusing to proceed from it.

**Fix.** Clear the stale structures first:

```sh
sgdisk --zap-all /dev/sdX
sgdisk -n 1:2048:<end> -t 1:8300 /dev/sdX
```

Safe as long as the filesystem starts past LBA 34 and ends before the last 33
sectors, which is the normal layout. Verify the superblock immediately before
and after — `dumpe2fs -h` on the partition or on a loop device at its offset.

### The VM name in the folder is not unique

A guest whose disks are spread across three datastores has three folders with
the **same name**, one per datastore. Any tool that resolves a VM by folder name
will take whichever datastore it happens to walk first, which is not necessarily
the one holding the system disk.

Address those by full path, not by name. `make-batch-rescue-vm.sh` accepts
either.
