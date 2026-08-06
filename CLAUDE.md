# Working in this repository

Recovery documentation and tooling for Babuk-family ESXi ransomware. Defensive
work: analysis, indicators, and a procedure for getting data back. There is no
malware sample here and none should ever be added.

If you are helping someone mid-incident, start from `docs/recovery-runbook.md`
for one guest, `docs/windows-recovery.md` if the guest is Windows, or
`docs/batch-recovery.md` for a fleet. `docs/analysis.md` is the reasoning
underneath all three. The agent skill in
`.claude/skills/esxi-ransomware-recovery/` is the condensed version, and
`AGENTS.md` points other assistants at the same material.

## The premise everything rests on

The encryptor damages only the head of each file and stops. Everything past that
offset is untouched plaintext. Every tool, every procedure and every claim here
follows from that one property. If you are changing something and it stops being
consistent with it, the change is wrong.

**The offset itself is not a constant, and treating it as one is a bug.** Two
builds are known from the same actor, with a byte-identical `run.sh`, the same
ransom note and the same master public key:

| Build | Damage per file |
|---|---|
| A | `0x20000000` — 512 MiB |
| B | `0x20800000` — 520 MiB |

The repository was originally written entirely around build A, so `512 MiB` and
`0x20000000` still appear as worked examples throughout. That is fine where the
text is describing a specific documented incident. It is **not** fine in a tool,
a default, or a sentence that reads as a general claim about the family — those
must take the boundary as input. `tools/measure-boundary.py` derives it by
entropy without being told what to look for; run it first on every host.

Assuming the smaller value against build B silently reports 8 MiB of ciphertext
as surviving plaintext, which marks a damaged file intact. That is the only
direction of error that costs someone their data.

## Non-negotiables

**Never suggest running the encryptor.** Not to test, not to confirm scope. On a
repeat pass it reports zero files encrypted, which looks harmless until you
notice it skipped everything only because the files already carried the `.babyk`
extension. Restored data will not be skipped.

**Say what writes.** Roughly half the tools here now modify something. Before
proposing one, say whether it writes and to what. The table below is the
reference; `--commit` flags are the convention, and everything carrying one is
dry-run by default.

**Damaged disks get attached Independent — non-persistent** while exploring.
Guest writes go to a redo log discarded at power off, so the original flat file
cannot change and the procedure becomes repeatable. Writes in that mode still
succeed, they are just thrown away, so a successful write is not evidence of
persistence. Switch to Dependent only to commit.

**Do not tell anyone their data is gone until the checks are done.** Four
false negatives have each looked exactly like total loss:

- a rescue environment without `lvm2` shows an LVM root as unformatted space;
- a filesystem that refuses to mount with `orphan file block N: bad magic` after
  `e2fsck` is intact, and one `tune2fs -O ^orphan_file` from readable;
- a disk with "no backup GPT" may have been expanded in VMware, with its
  secondary table stranded mid-device rather than absent;
- an NTFS volume reading as `RAW` is usually refusing over its root directory
  index while every file sits intact behind an untouched `$MFT`. Windows says
  `RAW` for all NTFS damage; `ntfsfix -n` names the actual structure.

**Decryption is closed.** Curve25519 ECDH with per-file ephemeral keys from
`/dev/urandom`, Sosemanuk for bulk, no implementation flaw, master private key
held by the attacker. The only avenue worth ten minutes is the No More Ransom
Babuk decryptor against a copy, because it tests the fifteen public keys.

## Order of work

Cheapest checks first, because two of them sometimes end the job:

1. `size % 512` per file. `0` means renamed but never encrypted.
2. `tools/measure-boundary.py` for the damage constant. Everything downstream
   is computed from it, so it is wrong to skip and cheap to run.
3. `tools/esxi-recon.sh` for host state, IOCs and pass counts.
4. `tools/make-descriptors.sh --write` so ESXi will attach the files at all.
5. `tools/remaining-report.sh` to classify the fleet.
6. Recover every `size % 512 == 0` guest — no rescue VM, no repair.
7. Only then build a rescue VM. One VM with many disks, not one per disk.

The number that predicts difficulty is where the filesystem starts. Past the
boundary means only the bootloader needs rebuilding. Before it means the root
inode and early inode tables are gone, ext4 keeps no backup of those, and the
guest needs `e2fsck` plus reassembly from `lost+found`.

**Windows guests do not follow this arc.** NTFS damage is not repaired here, it
is routed around: `tools/ntfs_triage.py` reports what died, and
`tools/ntfs_extract.py` reads the files out through the MFT without the volume
ever mounting. Both run on the ESXi host's own Python 3, so a Windows guest
often needs no rescue VM at all. See `docs/windows-recovery.md`.

## Tools

| File | Writes? | Purpose |
|---|---|---|
| `tools/measure-boundary.py` | no | find the damage constant by entropy, no assumption |
| `tools/esxi-recon.sh` | no | host triage: IOCs, datastores, pass counts, descriptors |
| `tools/babuk_triage.py` | no | classify files, measure surviving plaintext |
| `tools/ntfs_triage.py` | no | NTFS layout, which metadata died, which files are damaged |
| `tools/ntfs_extract.py` | no | NTFS: extract files via the MFT, no mount, no repair |
| `tools/babuk_mapdisk.py` | no | map one disk from backup GPT, backup VBR, ext4 backups |
| `tools/babuk_fleetscan.py` | no | run the mapper host-wide, CSV out |
| `tools/find_fs.py` | no | signature scan when the head is gone |
| `tools/find-backup-gpt.sh` | no | locate the backup GPT on a disk expanded in VMware |
| `tools/remaining-report.sh` | no | what is left; checks power state before reading a disk |
| `tools/final-status.sh` | no | per-VM power/tools/IP table |
| `tools/make-descriptors.sh` | creates only | descriptors, pure shell, nothing to upload |
| `tools/make_descriptors.py` | creates only | the same in Python |
| `tools/repair-disk-head.py` | **yes** | protective MBR, GPT and NTFS boot sectors, from the backups |
| `tools/repair-ntfs-meta.py` | **yes** | donate `$UpCase`/`$AttrDef`, rebuild `$MFTMirr` |
| `tools/recover-easy-path.sh` | **yes** | the easy path end to end; dry-run by default |
| `tools/repair-ubuntu-efi.sh` | **yes** | the same by hand, as a worked example |
| `tools/rebuild-bootable.sh` | **yes** | hard path: reassemble `lost+found` onto a fresh disk |
| `tools/make-rescue-vm.sh` | **yes** | provision one rescue VM around one disk |
| `tools/make-batch-rescue-vm.sh` | **yes** | provision one rescue VM around many disks |
| `tools/batch-repair.sh` | **yes** | drive the repair across every attached disk |
| `tools/bringup-recovered-vm.sh` | **yes** | repoint the `.vmx`, register, boot |
| `tools/bringup-sequential.sh` | **yes** | the same for a list, one at a time |
| `tools/fix-suspended-vms.sh` | **yes** | discard an unusable suspended state |
| `tools/esxi_run.py` | n/a | run a command or script on a host |
| `tools/batch_driver.py` | n/a | push manifest and scripts, run the batch, poll |
| `tools/windows/` | n/a | PowerShell, for password-only access from Windows |

Host-side tools target **Python 3 standard library and POSIX shell only**,
because they run on the ESXi host where nothing can be installed. Keep it that
way: no third-party imports, no bashisms in `.sh` files, nothing assuming a
package manager. Two consequences that are easy to forget:

- ESXi's busybox has **no `tr` and no `base64`**. It does have `awk`, `sed` and
  `python3`.
- ESXi's `test`/`[` is **32-bit** while `$(( ))` is 64-bit, so any byte-size
  comparison above 2 GiB must be written `[ "$(( a <= b ))" = "1" ]`. Getting
  this wrong reports large disks as total losses.

Workstation-side tools (`esxi_run.py`, `batch_driver.py`) may assume a normal
Python 3 and the system `ssh`/`scp`, but still no third-party packages.

Anything that runs on a target must be **LF with no BOM** — `.gitattributes`
enforces it. A CRLF or BOM in a shebang makes `/bin/sh` report "not found" for
an interpreter that is plainly there.

## Writing style here

Linux guest procedures are written against Ubuntu and Debian. Windows procedures
are written against Windows Server 2022 with NTFS, and live in
`docs/windows-recovery.md`. The disk-level technique is OS-independent.

For filesystems still not covered — ReFS, XFS, btrfs, BitLocker-protected
volumes — the docs point at R-Studio, DMDE or UFS Explorer rather than
pretending to a procedure nobody has run. Keep that habit: naming the gap is
more useful than inventing a method.

Every gotcha cost someone hours. When you add one, **describe the symptom as
well as the fix**, because the symptom is what the next person will be searching
for at 2am. Prose over bullet fragments. Concrete numbers over adjectives.

## Adding material from another incident

Publish the attacker side and keep the victim side out. Hashes, extensions,
ransom note text, Tox and onion contacts, embedded keys: all fine, that is what
makes an indicator list useful. Host addresses, VM names, hostnames, MAC
addresses, datastore UUIDs, filesystem UUIDs, credentials (including expired
ones) and full fleet inventories: not fine. A complete inventory maps a victim's
infrastructure even after the addresses are swapped out.

Use `192.0.2.0/24` (RFC 5737) for addresses, `00:00:5E:00:53:xx` (RFC 7042) for
MACs, and neutral names like `ubuntu-01` for guests.

Sanitize before the first commit rather than after. Anything that lands in git
history is public permanently, and cleaning it means rebuilding the repository.
Before pushing, grep the diff for addresses, hostnames, MACs and UUIDs.
