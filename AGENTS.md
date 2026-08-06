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
| `docs/windows-recovery.md` | Windows and NTFS guests |
| `docs/batch-recovery.md` | doing a fleet in one pass |
| `docs/environment-gotchas.md` | ESXi shell limits, SSH and workstation traps |
| `docs/case-media-server.md` | a hard-path recovery start to finish |
| `docs/analysis.md` | why any of this works, and why decryption does not |
| `CLAUDE.md` | contributor rules if you are editing the repository |

## What you are being asked to do

Someone's hypervisor is encrypted and they are under pressure. Three things
matter in the first message:

1. **Their data is very probably fine.** The encryptor stops a few hundred
   megabytes into each file; on multi-gigabyte virtual disks that leaves well
   over 99% intact. Say this before anything else.
2. **Decryption is not happening**, and they should not spend days on it.
3. **Nothing gets powered on until the host is clean**, because the encryptor is
   usually still sitting on it — on two hosts it re-ran days after the attack,
   while responders were already working the incident.

Then give them something to run. This is free and read-only, and it sometimes
ends the job — disks held open by a running VM were renamed but never encrypted:

```sh
ls -l /vmfs/volumes/*/*/*-flat.vmdk.babyk | awk '{print $5 % 512, $NF}'
```

`0` means never encrypted. `32` and `64` mean one and two passes.

Then measure the damage boundary, because **it is not a fixed constant**. Two
builds exist from this actor with a byte-identical `run.sh`: one stops at
`0x20000000` (512 MiB), the other at `0x20800000` (520 MiB). Assuming the
smaller reports 8 MiB of real damage as intact plaintext, which is the only
direction that loses data.

```sh
python3 tools/measure-boundary.py /vmfs/volumes/*/*/*-flat.vmdk.babyk
```

Every offset computed afterwards depends on that number.

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
- **On NTFS, `RAW` is not a diagnosis.** Windows reports it for every kind of
  damage. Run `ntfsfix -n` and read the real reason before concluding anything;
  `Corrupt index block signature: vcn 0 inode 5` means only the root directory
  index died, and the files are reachable through the MFT without it. See
  `docs/windows-recovery.md`.
- **Verify before you believe a number.** Free-block counts from a backup
  superblock are stale; ESXi's `test` is 32-bit and silently wraps above 2 GiB;
  `pgrep -f` matches its own command line. `docs/environment-gotchas.md` is a
  list of things that return a confident wrong answer rather than an error.
- **Sanitize anything you write into this repository.** Attacker-side detail —
  hashes, extensions, ransom notes, embedded keys — is the point. Victim-side
  detail — host addresses, VM names, hostnames, MACs, datastore and filesystem
  UUIDs, credentials — must not land in git history, which is permanent.

## If you are running the recovery

The arc is: measure, recon, descriptors, classify, then split the fleet.

```sh
python3 tools/measure-boundary.py ...   # the constant, before anything else
sh tools/esxi-recon.sh                  # host state and IOCs
sh tools/make-descriptors.sh --write    # creates files, modifies nothing
sh tools/remaining-report.sh            # classify; power-state aware
```

Guests with `size % 512 == 0` need no rescue VM and no repair — register and
boot them. Everything else with an intact backup GPT and a filesystem starting
past the boundary goes through `tools/recover-easy-path.sh`, in batches if there
are many (`docs/batch-recovery.md`).

**Windows guests take a different route.** Do not try to make the volume mount —
the root directory index is in the damaged head and cannot be rebuilt without
`chkdsk`, which will not run on an unmounted volume. Extract instead:
`tools/ntfs_triage.py` says what died, `tools/ntfs_extract.py` pulls the files
out through the MFT, read-only, without mounting anything. Both run on the ESXi
host's own Python 3.

Anything else is the hard path.

The failure-mode table in the skill file is the fastest way to turn a symptom
into a fix. Consult it before theorising.
