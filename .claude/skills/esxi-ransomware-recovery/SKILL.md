---
name: esxi-ransomware-recovery
description: Recover data and running VMs from VMware ESXi hosts hit by Babuk-family ransomware. Use when someone reports an ESXi host encrypted by ransomware, VMs that will not boot after an incident, .babyk or similar extensions on VMDK files, a ransom note in datastore folders, a hypervisor showing a ransom banner, or asks whether encrypted virtual disks can be recovered. Covers triage, measuring what survived, rebuilding partition tables from backup GPT, LVM and ext4 recovery, repairing a whole fleet in one pass, and bringing the guests back up.
---

# ESXi ransomware recovery

Babuk-derived ESXi lockers encrypt the first 512 MiB of each file and stop. On
multi-gigabyte virtual disks that leaves well over 99% of the data as readable
plaintext. This is data forensics, not cryptography.

Whoever is asking is mid-incident and under pressure. Lead with what is
recoverable, give them something to run in the first message, and do not open
with the cryptography.

## Establish these four things early

1. **The data is very probably fine.** Say it first. In the documented incident
   99.754% of the encrypted bytes were untouched.
2. **Decryption is not happening.** Curve25519 ECDH, per-file ephemeral keys
   from `/dev/urandom`, Sosemanuk. Correctly implemented, no key available. Do
   not let them burn days on it. The one exception worth ten minutes is the free
   No More Ransom Babuk decryptor, which tests the fifteen publicly recovered
   keys — run it against a *copy*.
3. **Nothing gets powered on until the host is clean.** The encryptor is
   usually still in `/var/run/`, executable. A restored VM is a fresh target and
   this time the writes land in the first 512 MiB, where nothing survives.
4. **Preserve the samples before deleting them** and report the incident with
   the master public key from the binary. That is how victims get matched if the
   actor is ever arrested.

## Read rather than reconstruct

| For | File |
|---|---|
| Damage model, crypto assessment, hardening | `docs/analysis.md` |
| The procedure, both paths, the gotchas | `docs/recovery-runbook.md` |
| Doing thirty guests instead of one | `docs/batch-recovery.md` |
| ESXi shell limits, SSH and workstation traps | `docs/environment-gotchas.md` |
| A hard-path recovery start to finish | `docs/case-media-server.md` |
| Rescue VM, click by click, then console | `docs/rescue-vm-guide.md` |
| Hashes, paths, behaviour to alert on | `docs/iocs.md` |

## The shape of the job

```
recon  ->  descriptors  ->  classify  ->  A: register and boot
                                      ->  B: rescue VM -> repair -> boot
                                      ->  C: testdisk, or rebuild from config
```

```sh
sh tools/esxi-recon.sh              # IOCs, datastores, pass counts
sh tools/make-descriptors.sh --write # creates files, never modifies
sh tools/remaining-report.sh        # classify, power-state aware
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

**Where the root filesystem starts** — past 512 MiB (Ubuntu guided install with
LVM) the filesystem is untouched and only the bootloader needs rebuilding,
minutes. At 1 MiB (plain-ext4 Debian) the root inode and the first ~131k inodes
are gone, ext4 keeps no backup of inode tables, and it is `e2fsck` plus
reassembly from `lost+found`.

## Tools, and which of them write

Read-only: `babuk_triage.py`, `babuk_mapdisk.py`, `babuk_fleetscan.py`,
`find_fs.py`, `find-backup-gpt.sh`, `remaining-report.sh`, `esxi-recon.sh`,
`final-status.sh`.

Creates files only: `make-descriptors.sh`, `make_descriptors.py`.

**Writes** — say so before proposing them:

| Tool | Writes to |
|---|---|
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
| repair succeeded, caller recorded FAILED | script's last statement was a conditional | check for a positive marker, not the exit code |
| application still sees no data after mounting | Docker bind mounts resolve at container start | `docker restart $(docker ps -q)` |
| VM never reports Tools, shows powered on | suspended when the encryptor ran, `.vmem` encrypted | `fix-suspended-vms.sh --commit` |

## Rules that prevent making it worse

- Attach damaged disks **Independent — non-persistent** while exploring. Writes
  go to a redo log discarded at power off. They still *succeed*, so a successful
  write proves nothing about persistence — confirm the setting, do not test it.
  Switch to **Dependent** only to commit, and once a layout is proven on a fleet
  use `--dependent` from the start to save a power cycle and a console trip.
- Use **SystemRescue**, not an Ubuntu live ISO. Without `lvm2` an LVM root reads
  as unformatted space, which looks exactly like total loss. That is the most
  common false negative in this work.
- **Never conclude data is gone** before `vgscan`, `vgchange -ay`, `lvs`, and —
  if `e2fsck` has run — before trying `tune2fs -O ^orphan_file`.
- On an irreplaceable volume too large to copy, repair through a **dm snapshot**
  with its COW on a scratch disk, verify, then `snapshot-merge` to commit. It
  turns `e2fsck` from a one-way door into a stage.
- **Never suggest running the encryptor**, for any reason.
- Sanitise before writing anything into this repository: hashes, extensions,
  ransom notes and attacker keys are the point; host addresses, VM names,
  hostnames, MACs, datastore UUIDs and credentials are not.
