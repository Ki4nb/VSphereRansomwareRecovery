---
name: esxi-ransomware-recovery
description: Recover data and running VMs from VMware ESXi hosts hit by Babuk-family ransomware. Use when someone reports an ESXi host encrypted by ransomware, VMs that will not boot after an incident, .babyk or similar extensions on VMDK files, a ransom note in datastore folders, a hypervisor showing a ransom banner, an NTFS volume reading as RAW after an incident, or asks whether encrypted virtual disks can be recovered. Covers triage, measuring what survived, rebuilding partition tables from backup GPT, LVM and ext4 recovery, NTFS and Windows guest recovery, repairing a whole fleet in one pass, and bringing the guests back up.
---

# ESXi ransomware recovery

Babuk-derived ESXi lockers destroy a few hundred megabytes at the head of each
file and stop. On multi-gigabyte virtual disks that leaves well over 99% of the
data as readable plaintext. This is data forensics, not cryptography.

Whoever is asking is mid-incident and under pressure. Lead with what is
recoverable, give them something to run in the first message, and do not open
with the cryptography.

## Establish these four things early

1. **The data is very probably fine.** Say it first. In the documented incidents
   99.754% of the "encrypted" bytes were untouched.
2. **Decryption is not happening.** Curve25519 ECDH, per-file ephemeral keys
   from `/dev/urandom`, Sosemanuk. Correctly implemented, no key available. Do
   not let them burn days on it. The one exception worth ten minutes is the free
   No More Ransom Babuk decryptor, which tests the fifteen publicly recovered
   keys — run it against a *copy*.
3. **Nothing gets powered on until the host is clean.** The encryptor is
   usually still in `/var/run/`, executable. A restored VM is a fresh target and
   this time the writes land in the damaged head, where nothing survives. On two
   hosts it re-ran *days after* the attack, while responders were working.
4. **Preserve the samples before deleting them** and report the incident with
   the master public key from the binary. That is how victims get matched if the
   actor is ever arrested.

## Measure the damage boundary. Never assume it.

The repository was originally written around `0x20000000` (512 MiB). A later
build from the same actor — byte-identical `run.sh`, same note, same keys — uses
`0x20800000` (**520 MiB**). Nothing on the host tells you which you have.

```sh
python3 tools/measure-boundary.py /vmfs/volumes/*/*/*-flat.vmdk.babyk
```

Assuming the smaller value against the larger build reports 8 MiB of real damage
as intact plaintext — the one direction that loses data, because it marks a
damaged database file as good. Every offset you compute afterwards depends on
this number. Get it first.

## Read rather than reconstruct

| For | File |
|---|---|
| Damage model, crypto assessment, hardening | `docs/analysis.md` |
| The procedure, both paths, the gotchas | `docs/recovery-runbook.md` |
| **Windows and NTFS guests** | `docs/windows-recovery.md` |
| Doing thirty guests instead of one | `docs/batch-recovery.md` |
| ESXi shell limits, SSH and workstation traps | `docs/environment-gotchas.md` |
| A hard-path recovery start to finish | `docs/case-media-server.md` |
| Rescue VM, click by click, then console | `docs/rescue-vm-guide.md` |
| Hashes, paths, behaviour to alert on | `docs/iocs.md` |

## The shape of the job

```
measure boundary -> recon -> descriptors -> classify -> A: register and boot
                                                     -> B: rescue VM -> repair -> boot
                                                     -> C: NTFS -> extract via MFT
                                                     -> D: testdisk, or rebuild from config
```

```sh
python3 tools/measure-boundary.py ...   # the constant, before anything else
sh tools/esxi-recon.sh                  # IOCs, datastores, pass counts
sh tools/make-descriptors.sh --write    # creates files, never modifies
sh tools/remaining-report.sh            # classify, power-state aware
```

`remaining-report.sh` is the one to trust for "what is left". A powered-on VM
holds a VMFS lock that makes its flat file read as **empty rather than as an
error**, so `babuk_fleetscan.py` silently drops recovered guests from its CSV as
you go.

## Two numbers decide everything

**`size % 512`** — VMFS flat files are 512-aligned and each pass appends a
32-byte key:

| Remainder | Meaning |
|---|---|
| `0` | never encrypted. Renamed only. Needs a descriptor and nothing else. |
| `32` / `64` | encrypted once / twice. Same damage region either way. |

Check it first, on every host. The orchestration script runs the encryptor
several times and any disk held open by a running VM gets renamed without being
touched. On one host that was 33 guests.

**Where the filesystem starts** — past the boundary (Ubuntu guided install with
LVM at 2–3 GiB; NTFS `$MFT` at ~3 GiB) the important structures are untouched
and only the bootloader needs rebuilding. At 1 MiB (plain-ext4 Debian) the root
inode and the first ~131k inodes are gone, ext4 keeps no backup of inode tables,
and it is `e2fsck` plus reassembly from `lost+found`.

## Windows guests

Fully covered now — see `docs/windows-recovery.md`. The essentials:

**`RAW` is not a diagnosis.** Windows says it for every kind of NTFS damage. Ask
Linux for the real reason:

```sh
ntfsfix -n /dev/sdXN            # -n checks, writes nothing
```

`Corrupt index block signature: vcn 0 inode 5` means the **root directory
index** died. A following `Failed to open $Secure` is a *consequence*, not a
second fault — ntfs-3g resolves system files by name through that index.

**You do not need the index.** Every file's `$FILE_NAME` records its parent, so
the tree rebuilds from the MFT alone, and `$MFT` normally starts ~3 GiB in.

```sh
python3 tools/ntfs_triage.py  <image> <boundary>                  # what died
python3 tools/ntfs_extract.py <image> <poff> <pend> <dmg> /out --list
```

`ntfs_extract.py` reads through the *backup* boot sector, so it never writes to
the damaged disk, and runs on the ESXi host's own Python 3 — no rescue VM needed
to get data off. Its final "these are NOT intact" list is the deliverable.

The damage argument is the boundary **relative to the volume** (boundary minus
partition offset). Getting it wrong corrupts the one report you cannot afford to
have wrong.

**Invariant vs volume-specific.** `$UpCase` (128 KiB) and `$AttrDef` (2560 B)
are identical on every NTFS volume of the same Windows version, so an intact
volume donates them — and Windows puts a recovery partition at the *end* of the
disk, past the damage. `$MFTMirr` rebuilds from the volume's own `$MFT`. The
root index, `$Secure` and `$Extend/$TxfLog` cannot be donated; only `chkdsk`
rebuilds those, and `chkdsk` will not run on a volume that will not mount. That
loop is why extraction beats repair.

**Registry:** `SYSTEM` and `SOFTWARE` survive, `SAM` and `SECURITY` die, and
`\Windows\System32\config\RegBack\` usually holds copies of all four — check
their MFT timestamps, since RegBack has been empty by default since Win10 1803.

**SQL Server:** `tempdb` is free (rebuilt at startup). Verify a data file is
real by checking page 0 is `m_type = 15`. Never reach for
`REPAIR_ALLOW_DATA_LOSS` before exhausting backups and replicas — **an AlwaysOn
synchronous-commit secondary is transactionally identical to the primary at the
moment of the attack**, and in one incident the secondary was completely
undamaged where the primary lost 477 MiB of a live data file.

## Tools, and which of them write

Read-only: `measure-boundary.py`, `babuk_triage.py`, `babuk_mapdisk.py`,
`babuk_fleetscan.py`, `ntfs_triage.py`, `ntfs_extract.py`, `find_fs.py`,
`find-backup-gpt.sh`, `remaining-report.sh`, `esxi-recon.sh`, `final-status.sh`.

Creates files only: `make-descriptors.sh`, `make_descriptors.py`.

**Writes** — say so before proposing them:

| Tool | Writes to |
|---|---|
| `repair-disk-head.py --commit` | protective MBR, primary GPT, NTFS boot sectors |
| `repair-ntfs-meta.py --commit` | `$UpCase`, `$AttrDef`, `$MFTMirr`, `$Boot` 1–15 |
| `recover-easy-path.sh --commit` | GPT, the ESP, `/boot`, bootloader |
| `repair-ubuntu-efi.sh --commit` | same, by hand, as a worked example |
| `rebuild-bootable.sh` | a fresh target disk |
| `bringup-recovered-vm.sh --commit` | the guest's `.vmx` (backs it up first) |
| `bringup-sequential.sh --commit` | the same, for a list |
| `make-rescue-vm.sh`, `make-batch-rescue-vm.sh` | creates a rescue VM |
| `fix-suspended-vms.sh --commit` | discards an unusable suspended state |

Everything with a `--commit` flag is dry-run by default. Run the dry pass, read
it, then commit.

## The easy path

```sh
# one disk, from within a SystemRescue VM
bash tools/recover-easy-path.sh                 # dry run: reports what it found
bash tools/recover-easy-path.sh --commit        # repair
```

It reads the layout from the disk's own backup GPT and the guest's `fstab` and
netplan rather than being told: EFI vs BIOS, LVM vs plain ext4, the ESP's
original volume ID, the pinned MAC. It refuses to write unless `LABELONE` or the
ext4 magic is exactly where the GPT says it should be.

Then put the guest back:

```sh
sh tools/bringup-recovered-vm.sh <vm-folder> "VM Network" --commit
```

which repoints the `.vmx` at the recovered descriptor, strips vCenter
`dvportgroup` bindings that no longer resolve, pins the original MAC, registers
and boots it.

## A fleet

One rescue VM with many disks attached, not one per disk — SystemRescue runs
from RAM, so every boot costs a console trip to reset the password, firewall and
IP. See `docs/batch-recovery.md`.

```sh
sh tools/make-batch-rescue-vm.sh "VM Network" --list /tmp/vms.txt --commit
python3 tools/batch_driver.py --esxi <host> --rescue <ip> --key ~/.ssh/ir_key
python3 tools/batch_driver.py --esxi <host> --rescue <ip> --key ~/.ssh/ir_key --commit
sh tools/bringup-sequential.sh "VM Network" --list /tmp/vms.txt --commit
```

Do all the `size % 512 == 0` guests first. They need no rescue VM and no repair.

## Failure modes, symptom first

This table is the highest-value thing here. Every row was hours the first time.

| Symptom | Cause | Fix |
|---|---|---|
| a file past 512 MiB reads as ciphertext but "should be intact" | this is the 520 MiB build, not the 512 MiB one | `tools/measure-boundary.py`; never assume the constant |
| Windows says the volume is `RAW` | says nothing about which structure died | `ntfsfix -n` names it; usually `vcn 0 inode 5`, the root index |
| `Corrupt index block signature: vcn 0 inode 5` | root directory index was in the damaged head | do not repair — extract with `ntfs_extract.py`, which needs no index |
| `Failed to open $Secure: No such file or directory` | a *consequence* of the broken root index, not a second fault | same fix; do not chase `$Secure` |
| NTFS boot sector restored, volume still `RAW` | `$UpCase`/`$AttrDef` also died, and so did the root index | donate the first two; the index needs `chkdsk`, which needs a mount |
| extracted database file is short, no error | `$ATTRIBUTE_LIST` runs not followed | use `ntfs_extract.py`; a hand-rolled parser truncates silently |
| `tar` mangles a >8 GiB file on ESXi | busybox `tar` writes ustar, capped at 8 GiB per member | Python `tarfile` with `format=tarfile.GNU_FORMAT` |
| copying tens of GB off ESXi crawls | ESXi's sshd runs ~10 MB/s vs ~370 MB/s local disk | compress on the *host* side; the link is the bottleneck, not the CPU |
| `make-descriptors.sh --write` "fails" but worked | its last statement was a conditional, so it exited 1 | fixed; in general check for a positive marker, not an exit code |
| `make-descriptors.sh` finds nothing on a real host | datastore name contains spaces and got word-split | fixed; quote or read paths line by line |
| `esxcli system settings kernel set -s execInstalledOnly` does nothing | the option does not exist on this build (e.g. 7.0.3 build-19482537) | re-read with `kernel list -o`; confirm `Configured` actually changed |
| `orphan file block N: bad magic`, mount fails after a clean `e2fsck` | `e2fsck` rebuilt ext4's `orphan_file` from garbage bitmaps | `tune2fs -O ^orphan_file` then `e2fsck -fy`. **The filesystem is intact.** |
| `Cannot activate LVs ... PVs appear on duplicate devices` | cloned guests share VG name *and* UUID, or a loop is stacked over a visible partition | scope every LVM call: `vgchange -ay --devices /dev/sdXN` |
| `unknown filesystem type 'LVM2_member'` | Ubuntu puts the PV on a `8300` partition, not `8e00` | detect LVM with `pvs`, never by GPT type code |
| `gdisk` finds no backup GPT | disk was expanded in VMware; backup is at the *original* end | `tools/find-backup-gpt.sh`, then cap a loop at that size |
| `Problem: MBR partitions 3 and 4 overlap`, sgdisk will not write | parsing the garbage MBR in the encrypted head | `sgdisk --zap-all` first |
| `dd` on a flat file returns nothing, no error | VMFS lock — a VM using it is powered on | power off the rescue VM |
| guest drops to an emergency shell, system disk healthy | `fstab` requires a damaged data volume | add `nofail`, set fsck pass `0` |
| a 10 GiB disk reported as under 512 MiB | ESXi `test` is 32-bit; `$(( ))` is 64-bit | `[ "$(( a <= b ))" = "1" ]` |
| `line 1: #!/bin/sh: not found` | UTF-8 BOM or CRLF in the script | write LF, no BOM |
| long job dies part-way, no error either end | SSH channel dropped, remote process got SIGHUP | `setsid nohup`, and make the job resumable |
| VM boots the installer ISO then "no bootable device" | the ISO's "press any key" prompt timed out after ~3 s | `bios.forceSetupOnce = "TRUE"` for an untimed boot menu |
| application still sees no data after mounting | Docker bind mounts resolve at container start | `docker restart $(docker ps -q)` |
| VM never reports Tools, shows powered on | suspended when the encryptor ran, `.vmem` encrypted | `fix-suspended-vms.sh --commit` |

## Rules that prevent making it worse

- Attach damaged disks **Independent — non-persistent** while exploring. Writes
  go to a redo log discarded at power off. They still *succeed*, so a successful
  write proves nothing about persistence — confirm the setting, do not test it.
  Switch to **Dependent** only to commit, and once a layout is proven on a fleet
  use `--dependent` from the start to save a power cycle and a console trip.
- For a repair you intend to keep, a **VMware snapshot** turns the whole thing
  into a stage rather than a one-way door. Take it after the descriptors exist
  and before anything writes inside the guest.
- Use **SystemRescue**, not an Ubuntu live ISO. Without `lvm2` an LVM root reads
  as unformatted space, which looks exactly like total loss. That is the most
  common false negative in this work.
- **Never conclude data is gone** before `vgscan`, `vgchange -ay`, `lvs`, and —
  if `e2fsck` has run — before trying `tune2fs -O ^orphan_file`. On NTFS, never
  before `ntfsfix -n` has named the actual structure.
- On an irreplaceable volume too large to copy, repair through a **dm snapshot**
  with its COW on a scratch disk, verify, then `snapshot-merge` to commit.
- **Never suggest running the encryptor**, for any reason.
- Sanitise before writing anything into this repository: hashes, extensions,
  ransom notes and attacker keys are the point; host addresses, VM names,
  hostnames, MACs, datastore UUIDs and credentials are not.
