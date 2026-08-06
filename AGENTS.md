# Instructions for AI agents

This repository recovers data from VMware ESXi hosts encrypted by Babuk-family
ransomware. It is defensive work: analysis, indicators, and a procedure for
getting data back. There is no malware sample here and none should be added.

Any agent — Claude, Codex, Cursor, Copilot, Gemini, an autonomous harness —
should be able to run this. The procedure is in ordinary files, not in an
agent-specific format:

| Read this | For |
|---|---|
| **`.claude/skills/esxi-ransomware-recovery/SKILL.md`** | the condensed procedure. Start here. It is plain Markdown with a YAML header; ignore the header if your runtime does not use it. |
| `docs/recovery-runbook.md` | the full procedure for one guest |
| `docs/batch-recovery.md` | doing a fleet in one pass |
| `docs/environment-gotchas.md` | ESXi shell limits, SSH and workstation traps |
| `docs/case-media-server.md` | a hard-path recovery start to finish |
| `docs/analysis.md` | why any of this works, and why decryption does not |
| `CLAUDE.md` | contributor rules if you are editing the repository |

## What you are being asked to do

Someone's hypervisor is encrypted and they are under pressure. Three things
matter in the first message:

1. **Their data is very probably fine.** The encryptor stops after 512 MiB per
   file; on multi-gigabyte virtual disks that leaves well over 99% intact. Say
   this before anything else.
2. **Decryption is not happening**, and they should not spend days on it.
3. **Nothing gets powered on until the host is clean**, because the encryptor is
   usually still sitting on it.

Then give them something to run. This is free and read-only, and it sometimes
ends the job — disks held open by a running VM were renamed but never encrypted:

```sh
ls -l /vmfs/volumes/*/*/*-flat.vmdk.babyk | awk '{print $5 % 512, $NF}'
```

`0` means never encrypted. `32` and `64` mean one and two passes.

## Operating rules

These are not style preferences. Each one exists because ignoring it destroyed
something or wasted a day.

- **Never propose running the encryptor.** On a second pass it reports zero
  files encrypted, which looks safe and is not: it skipped them only because
  they already carried the ransomware extension. Restored data will not be.
- **State whether a command writes, and to what,** before proposing it. About
  half the tools here modify something. Everything with a `--commit` flag is
  dry-run by default; run the dry pass and read it first.
- **Attach damaged disks Independent — non-persistent** while exploring. Writes
  in that mode still succeed and are discarded at power off, so a successful
  write proves nothing about persistence. Never use a write as a test.
- **Do not conclude that data is lost** until `vgscan`/`vgchange -ay`/`lvs` have
  been tried, and — if `e2fsck` has run and the mount fails with
  `orphan file block N: bad magic` — until `tune2fs -O ^orphan_file` has been
  tried. Both of those look exactly like total loss and are not.
- **Verify before you believe a number.** Free-block counts from a backup
  superblock are stale; ESXi's `test` is 32-bit and silently wraps above 2 GiB;
  `pgrep -f` matches its own command line. `docs/environment-gotchas.md` is a
  list of things that return a confident wrong answer rather than an error.
- **Sanitize anything you write into this repository.** Attacker-side detail —
  hashes, extensions, ransom notes, embedded keys — is the point. Victim-side
  detail — host addresses, VM names, hostnames, MACs, datastore and filesystem
  UUIDs, credentials — must not land in git history, which is permanent.

## If you are running the recovery

The arc is: recon, descriptors, classify, then split the fleet.

```sh
sh tools/esxi-recon.sh                  # host state and IOCs
sh tools/make-descriptors.sh --write    # creates files, modifies nothing
sh tools/remaining-report.sh            # classify; power-state aware
```

Guests with `size % 512 == 0` need no rescue VM and no repair — register and
boot them. Everything else with an intact backup GPT and a root filesystem past
512 MiB goes through `tools/recover-easy-path.sh`, in batches if there are many
(`docs/batch-recovery.md`). Anything else is the hard path.

The failure-mode table in the skill file is the fastest way to turn a symptom
into a fix. Consult it before theorising.
