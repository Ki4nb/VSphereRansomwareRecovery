# Getting your data back after Babuk-family ransomware hits ESXi

If you found this because your ESXi host is showing a ransom banner, every VM is
powered off, and your virtual disks now end in `.babyk`: the situation is much
better than it looks. Take five minutes to read this before you do anything
irreversible.

Two things are true at once.

**You will not decrypt these files.** The crypto is Curve25519 ECDH with a
per-file ephemeral key from `/dev/urandom` and Sosemanuk for the bulk. It is
implemented correctly. There is no flaw to attack and no key to find. Anyone who
tells you otherwise is selling something.

**You have probably lost almost nothing.** The encryptor stops a few hundred
megabytes into each file and moves on. In the first incident documented here,
6,911.95 GiB of "encrypted" virtual disks contained 6,894.93 GiB of perfectly
readable plaintext — 99.754% of the bytes were never touched. Most Linux guests
came back in under ten minutes each.

Since then the same procedure has recovered **six hosts and around 150 damaged
disks**: a fleet of thirty guests brought back in a single pass, a 6.7 TB media
volume whose root directory had been destroyed, and — as of August 2026 —
**Windows Server guests running SQL Server, with zero data loss**.

The rest of this is how.

**What this covers:** ESXi ransomware recovery and VMware ransomware recovery
end to end, from `.babyk` VMDK recovery on VMFS datastores through GPT partition
table rebuild, then **either** LVM and ext4 recovery inside a Linux guest **or**
NTFS recovery inside a Windows one, plus the ransomware incident response steps
around all of it (evidence, IOCs, cleaning the host). Babuk ransomware and its
ESXi descendants specifically, though the damage model applies to any locker
that stops early in a file.

---

## Run this with an AI agent

**This repository ships an agent skill.** Point Claude Code — or any coding
agent — at it and you get the whole procedure: triage, the damage model, the
tooling, and a symptom-to-fix table for the failures that look like total data
loss and are not.

That last part is why it is worth doing. Several failure modes in this work look
identical to "your data is gone" while being one command away from readable, and
an agent that has read the table will not tell you to give up.

### Claude Code

```bash
git clone https://github.com/Ki4nb/VSphereRansomwareRecovery.git
cd VSphereRansomwareRecovery
claude
```

The skill in `.claude/skills/` is picked up automatically while you work inside
the repository. To make it available everywhere instead:

```bash
mkdir -p ~/.claude/skills
cp -r VSphereRansomwareRecovery/.claude/skills/esxi-ransomware-recovery ~/.claude/skills/
```

Then just describe the situation — "our ESXi host got hit, the vmdks are all
.babyk now" — and it will load.

### Any other agent

Codex, Cursor, Copilot, Gemini, or your own harness: clone the repository and
point the agent at **[`AGENTS.md`](AGENTS.md)**. Many read it automatically.
Everything is plain Markdown and plain POSIX shell — there is no runtime, no
plugin and no dependency to install.

```
You are helping with an ESXi ransomware recovery.
Read AGENTS.md in this repository first, then
.claude/skills/esxi-ransomware-recovery/SKILL.md.
```

### What to expect

It will ask for read-only output before suggesting anything that writes, tell
you which commands modify what, and refuse to declare data lost before the
checks that routinely disprove it. Everything with a `--commit` flag is dry-run
by default.

> An agent is a good pair of hands here and a bad decision-maker. It will move
> faster than you can check it. Keep damaged disks attached
> **Independent — non-persistent** until you have seen a dry run you believe.

---

## Measure the boundary. Do not assume it.

Whoever built this made a speed choice. On a hypervisor you don't need to
encrypt a 2 TB disk to take it hostage. You only need to destroy the front of
it, where the partition table and the bootloader live, and then the VM won't
boot and the disk looks like garbage. So the encryptor writes a fixed number of
bytes, appends a 32-byte key, and moves to the next file.

Here is a 100 GiB disk after it ran:

```
[ ~520 MiB destroyed ][ -------------- 99.5 GiB, untouched -------------- ]
  partition table       /boot, LVM, NTFS $MFT, databases, everything
  EFI bootloader
```

**How many bytes is not a constant.** This actor has shipped at least two builds
with a byte-identical `run.sh`, the same ransom note, the same contacts and the
same master public key, differing only here:

| Build | Bytes destroyed per file | |
|---|---|---|
| A | `0x20000000` | 512 MiB |
| B | `0x20800000` | **520 MiB** |

Eight megabytes sounds like nothing. It is the difference between correctly
reporting a database file as damaged and confidently reporting it as intact —
an error in the only direction that costs you data. So the first thing you
establish is the number, not the procedure:

```sh
python3 tools/measure-boundary.py /vmfs/volumes/*/*/*-flat.vmdk.babyk
```

It finds the transition by entropy, without being told what to look for, and
refines to the sector. Encrypted output is uniform random — high entropy, almost
no zero bytes — while real filesystem data is structured, sparse or both. On
build B the last encrypted sector begins at `0x207FFE00` and clean plaintext
resumes at `0x20800000`, on every file, on every disk, on two separate hosts.

It stops dead at the boundary, every time. It just is not always the same
boundary.

## Filesystems keep spare copies of the parts that died

This is the part that turns a bad day into a long afternoon. The structures
sitting in that damaged head are exactly the ones filesystem designers decided
were too important to keep only one copy of, and the spares live at the *end* of
the volume, where the encryptor never reaches.

| What died | Where the spare is | Usable? |
|---|---|---|
| GPT partition table (LBA 1) | last sector of the disk | yes, `gdisk` rebuilds from it |
| NTFS boot sector | last sector of the partition | yes |
| NTFS `$MFT` | not a spare — it just starts ~3 GiB in | yes, and it is the whole ballgame |
| NTFS `$UpCase`, `$AttrDef` | identical on every NTFS volume | yes, donate from an intact one |
| NTFS root directory index | **nowhere** | no — and this is why Windows says RAW |
| ext2/3/4 superblock | block groups 1, 3, 5, 7, 9, 25… | yes, though the first two are usually inside the damage |
| LVM2 PV label | no spare, but PVs normally start at 2–3 GiB | intact if the PV starts past the boundary |
| ext4 inode tables | **nowhere** | this is the one that hurts |

So recovery is mechanical: read the backup structure, rebuild the primary from
it, mount, copy the data out. Where there is no spare, work around the missing
structure instead of trying to recreate it.

## One number tells you how bad each VM is

Where does the filesystem start?

If it starts past the boundary — Ubuntu's default guided install with LVM puts
the root PV around 2–3 GiB in, and NTFS puts `$MFT` around 3 GiB in — then the
important structures are completely untouched. You lost the bootloader and
metadata that has spares, and you are close to having your files back.

If it starts at 1 MiB, which is what a plain-ext4 Debian install gives you, then
the root inode and the first ~131,000 inodes were inside the blast radius.
ext4's `flex_bg` packs the inode tables for 16 block groups together at the front
of the filesystem, and unlike superblocks, **inode tables have no backup copies
anywhere**. The file data is still on the disk. The names and the directory tree
are not. That guest needs `e2fsck` and then reassembly out of `lost+found`.

Both are recoverable. One is coffee, the other is an afternoon.

---

## First 15 minutes

Everything here reads and never writes.

```sh
# How many times was each file encrypted? VMFS flat files are 512-byte aligned,
# so the appended keys show up in the remainder.
#   0  = renamed but NEVER encrypted
#   32 = encrypted once
#   64 = encrypted twice
ls -l /vmfs/volumes/*/*/*-flat.vmdk.babyk | awk '{print $5 % 512, $NF}'
```

Do that one first. It is free, and it sometimes ends the job: the attacker's
script runs the encryptor several times in a row, and any disk still held open by
a running VM gets renamed without being encrypted. On one host that was five
disks and 325 GiB; on another, 33 guests. They needed a new descriptor file and
nothing else.

```sh
python3 tools/measure-boundary.py /vmfs/volumes/*/*/*-flat.vmdk.babyk
sh tools/esxi-recon.sh                  # host state, IOCs, datastores, pass counts
sh tools/make-descriptors.sh --write    # only ever creates new files
sh tools/remaining-report.sh            # classify the fleet
```

## The descriptor problem, and the trick that solves it

You cannot attach a `.babyk` file to a VM. ESXi wants a descriptor file, and the
original descriptor was a 360-byte text file, so it sat entirely inside the
damage and is gone for good.

Writing a new one takes four numbers, and it fixes a second problem at the same
time. A VMDK descriptor declares its extent length in sectors. Declare
`original_size / 512` and two things happen: the 32 or 64 appended key bytes fall
outside the declared extent so nothing reads them, and the extent goes back to
being 512-byte aligned, which those appended bytes had broken and which quietly
confuses every recovery tool you will otherwise reach for.

After that, ESXi treats the encrypted file as an ordinary flat disk. Nothing gets
renamed, copied or modified.

## Windows guests

For a long time this repository said no Windows guest had been recovered here.
That is no longer true, and the procedure is in
**[docs/windows-recovery.md](docs/windows-recovery.md)**.

The disk-level work is identical. What is different is NTFS, and one thing about
it will waste your afternoon if nobody tells you:

**Windows reports the volume as `RAW`, and that word means nothing.** A volume
missing its boot sector, a volume missing every file, and a volume that is
perfectly readable except for one 4 KiB index block all look the same in Disk
Management. Ask Linux instead and you get the actual reason in one command:

```sh
ntfsfix -n /dev/sdb2        # -n checks, writes nothing
```

```
Corrupt index block signature: vcn 0 inode 5
Failed to open $Secure: No such file or directory
```

Inode 5 is the root directory. Its index block is in the damaged head. The
`$Secure` line is not a second fault — ntfs-3g finds system files by name
*through that index*, so one broken structure produces two alarming messages.

The important part: **you do not need the index.** Every file's `$FILE_NAME`
attribute records its parent directory, so the whole tree can be rebuilt from the
MFT alone — and `$MFT` normally starts around 3 GiB into the volume, far past any
early-stop encryptor.

```sh
python3 tools/ntfs_triage.py  disk-flat.vmdk.babyk 545259520          # what died
python3 tools/ntfs_extract.py disk-flat.vmdk.babyk <off> <end> <dmg> /out --list
```

`ntfs_extract.py` reads through the *backup* boot sector, so it never writes to
the damaged disk, and it runs on the ESXi host's own Python 3 — no rescue VM
required to get the data off. It verifies every file against the size recorded in
its MFT record and lists any file whose clusters overlapped the destroyed head,
which is the one output you actually have to read.

Two more things worth knowing before you start: `$UpCase` and `$AttrDef` are
byte-identical on every NTFS volume of the same Windows version, so an intact
volume can donate them — and Windows helpfully puts a recovery partition at the
*end* of the disk, hundreds of gigabytes past the damage. And `SAM` and
`SECURITY` die while `SYSTEM` and `SOFTWARE` survive, with copies of all four
usually sitting in `\Windows\System32\config\RegBack\`.

## Four rules that keep this safe

**Attach damaged disks as `Independent — non-persistent`.** Every write the
rescue guest makes goes into a redo log that gets thrown away at power off. The
original file physically cannot change, which means you can experiment, get it
wrong, and start over. Worth knowing: writes in this mode still *succeed*, they
are just discarded later, so a successful write proves nothing about
persistence. Confirm the setting in the UI.

**Clean the host before you power anything on.** The encryptor is usually still
sitting in `/var/run/`, executable. A VM you just restored is a fresh target, and
this time the new writes land in the damaged head where nothing survives. On both
August 2026 hosts the encryptor re-ran *days later*, while responders were
already working the incident.

**Never run the encryptor to "check" something.** On its final pass in the first
incident it reported 0 files encrypted and 916 skipped, which looks harmless
until you understand it skipped them only because they already had the `.babyk`
extension. Anything you restore is fair game.

**Assume every credential on those guests is burned.** SSH keys, `.env` files,
database passwords, `.git-credentials`. They survived the encryption, which means
the attacker had hours of root access to read them.

## Before you write anything off

Run the free [No More Ransom](https://www.nomoreransom.org/) Babuk decryptor
against a *copy* of one encrypted file. Fifteen Babuk private keys are public:
fourteen from the 2021 source-code leak, plus the Tortilla key that Cisco Talos
recovered and handed to Avast in 2024. The tool tries all of them in about ten
minutes. If you are facing a private fork it will fail, but ten minutes is
cheap and assuming is not.

Then report the incident, and include the master public key from the binary.
Every key in that public decryptor exists because someone reported. When an
actor is eventually arrested and their keys are recovered, victims get matched by
exactly that value. If yours was never filed, nobody can match it to you.

**And do not trust "unrecoverable" until you have checked four specific
things.** Each of these looks exactly like total data loss and is not:

- an LVM root in a rescue environment that lacks `lvm2` reads as unformatted
  space;
- a filesystem that refuses to mount with `orphan file block N: bad magic` after
  a clean `e2fsck` is intact, and one `tune2fs -O ^orphan_file` from readable;
- a disk reporting "no backup GPT" may have been expanded in VMware, leaving its
  secondary table stranded mid-device rather than absent;
- an NTFS volume reading as `RAW` is usually refusing over its root directory
  index, while `$MFT` and every file sit intact 3 GiB further in.

## Questions people ask at hour one

### Can Babuk ransomware be decrypted?

Not this variant, and almost certainly not yours. Curve25519 ECDH with a
per-file ephemeral key drawn from `/dev/urandom`, SHA-256 to derive, Sosemanuk
to encrypt. It is implemented correctly and the master private key sits with the
attacker. Fifteen Babuk private keys *are* public, from the 2021 source leak plus
the Tortilla key Cisco Talos recovered, so run the No More Ransom decryptor
against a copy of one file before you accept that answer. It takes ten minutes.

### What is a .babyk file, and can I open it?

It is one of your files with the first few hundred megabytes overwritten and 32
bytes of key appended. Nothing opens it directly. On a virtual disk, everything
past the boundary is still your original data, which is why the recovery works.

### My ESXi VMs won't boot after ransomware. Is the data gone?

Very probably not. What died is the partition table and the bootloader, both of
which sit in the first megabytes of the disk. The filesystem and your files are
further in. Follow the [rescue VM guide](docs/rescue-vm-guide.md) and you will
usually be looking at a directory listing within the hour.

### How do I recover a VMDK encrypted by ransomware?

Generate a fresh descriptor so ESXi will attach the encrypted flat file as an
ordinary disk, attach it to a rescue VM in non-persistent mode, rebuild the GPT
from the backup copy at the end of the disk, activate LVM, and mount read-only.
`tools/make-descriptors.sh` handles the first step for every damaged file on the
host.

### How do I rebuild a GPT partition table after ransomware?

The primary table at LBA 1 is destroyed, but GPT keeps a full backup in the last
sector of the disk and `gdisk` restores one from the other:
`printf '1\nr\nb\nw\nY\n' | gdisk /dev/sda`. The leading `1` answers gdisk's
"found invalid MBR" prompt. If there is no backup header at all, the disk is
MBR-partitioned and you want `testdisk` with a deeper search instead.
`tools/repair-disk-head.py` does the same thing offline against a flat file, and
restores the NTFS boot sectors in the same pass.

### My Windows volume shows as RAW. Is it gone?

Almost certainly not. Run `ntfsfix -n` against it from a Linux rescue and read
the actual error. If it names `vcn 0 inode 5`, that is the root directory index,
and your files are fine — see [docs/windows-recovery.md](docs/windows-recovery.md).
Extract with `tools/ntfs_extract.py`, which does not need the volume to mount.

### How do I recover an LVM volume after ransomware?

If the physical volume starts past the boundary, which is normal on an Ubuntu
guided install, the LVM label and metadata are untouched. `vgscan --mknodes`,
`vgchange -ay`, `lvs`, then mount. If the rescue environment has no `lvm2`
installed, the volume shows up as unformatted space and looks exactly like total
loss. That mistake is the single most common false negative in this work.

### What if ext4 says bad superblock?

The primary superblock at offset 1024 was inside the damage. ext4 keeps backups
at block groups 1, 3, 5, 7, 9, 25 and so on, but the first two usually sit inside
the blast radius too. Block 163840 is normally the first usable one:
`e2fsck -b 163840 -B 4096 /dev/sdaN`. `tools/find_fs.py` will scan and tell you
rather than making you guess.

### Should I pay?

Nobody can make that call for you, but do the measurement first. Run the triage
in the section above and find out how much of your data is actually damaged
before anyone quotes you a price for it. In the first incident the answer was
0.246%. In the most recent one, the entire loss on one host landed on a `tempdb`
file that SQL Server rebuilds at every startup.

### e2fsck finished but the filesystem still will not mount

If the error is `orphan file block N: bad magic`, the filesystem is intact.
`e2fsck` rebuilt ext4's optional `orphan_file` using block bitmaps that are
garbage in the destroyed groups, so it allocated blocks past the end of the
device. Drop the feature and re-check:
`tune2fs -O ^orphan_file /dev/sdaN && e2fsck -fy /dev/sdaN`. This one is worth
knowing because at that moment the volume looks completely destroyed and is one
command from readable.

---

## Documentation

- **[docs/recovery-runbook.md](docs/recovery-runbook.md)** — the working
  procedure. Triage, both recovery paths, and the mistakes that cost hours.
- **[docs/windows-recovery.md](docs/windows-recovery.md)** — Windows and NTFS:
  why the volume reads as RAW, what can be donated, and how to extract without
  mounting anything.
- **[docs/batch-recovery.md](docs/batch-recovery.md)** — thirty guests instead
  of one. One rescue VM, many disks, and how to tell identical clones apart.
- **[docs/environment-gotchas.md](docs/environment-gotchas.md)** — the ESXi
  shell, SSH and the workstation. Everything here returns a confident wrong
  answer rather than an error.
- **[docs/case-media-server.md](docs/case-media-server.md)** — a hard-path
  recovery start to finish, including the wrong turns.
- **[docs/rescue-vm-guide.md](docs/rescue-vm-guide.md)** — click-by-click in the
  ESXi web UI, then the console commands.
- **[docs/analysis.md](docs/analysis.md)** — what the malware does, the full
  cryptographic assessment, and hardening.
- **[docs/iocs.md](docs/iocs.md)** — hashes, paths and behavioural indicators.

## Tools

Host-side tooling is Python 3 standard library and POSIX shell only, because it
runs on the ESXi host where you cannot install anything.

| Tool | Writes? | What it does |
|---|---|---|
| `measure-boundary.py` | no | find where the encryptor stopped, without assuming |
| `esxi-recon.sh` | no | host triage: IOCs, datastores, pass counts |
| `babuk_triage.py` | no | classify files, total the surviving plaintext |
| `babuk_mapdisk.py` | no | rebuild one disk's layout from its backup structures |
| `babuk_fleetscan.py` | no | the mapper across the whole host, CSV out |
| `ntfs_triage.py` | no | NTFS: what died, what survived, which files are damaged |
| `ntfs_extract.py` | no | NTFS: pull files out via the MFT, no mount needed |
| `find_fs.py` | no | signature scan when the head is gone entirely |
| `find-backup-gpt.sh` | no | find the backup GPT on a disk expanded in VMware |
| `remaining-report.sh` | no | what is left; checks power state before reading |
| `final-status.sh` | no | per-VM power/tools/IP table |
| `make-descriptors.sh` / `.py` | creates only | the `-recovered.vmdk` descriptors |
| `repair-disk-head.py` | **yes** | protective MBR, GPT and NTFS boot sectors from backups |
| `repair-ntfs-meta.py` | **yes** | donate `$UpCase`/`$AttrDef`, rebuild `$MFTMirr` |
| `recover-easy-path.sh` | **yes** | the easy path end to end; dry-run by default |
| `repair-ubuntu-efi.sh` | **yes** | the same by hand, as a worked example |
| `rebuild-bootable.sh` | **yes** | hard path: reassemble `lost+found` onto a fresh disk |
| `make-rescue-vm.sh` | **yes** | provision a rescue VM around one disk |
| `make-batch-rescue-vm.sh` | **yes** | provision one rescue VM around many disks |
| `batch-repair.sh` | **yes** | drive the repair across every attached disk |
| `bringup-recovered-vm.sh` | **yes** | repoint the `.vmx`, register, boot |
| `bringup-sequential.sh` | **yes** | the same for a list, one at a time |
| `fix-suspended-vms.sh` | **yes** | guests suspended with an encrypted `.vmem` |
| `esxi_run.py`, `batch_driver.py` | n/a | run things on a host from your workstation |
| `windows/` | n/a | PowerShell, for password-only access from Windows |

## Scope

Linux guest recovery here is written against Ubuntu and Debian, since that is
what the fleets ran. Windows guest recovery is written against Windows Server
2022 with NTFS and SQL Server. The disk-level work applies to any guest
regardless.

**Still untested:** XFS and btrfs, where the backup-superblock story differs;
ReFS, dynamic disks, Storage Spaces and BitLocker, none of which the NTFS work
touches; and NTFS with 4 KiB physical sectors. For those, **R-Studio**, **DMDE**
and **UFS Explorer** remain the sensible commercial fallback. See
[docs/analysis.md](docs/analysis.md) for the full list of what this does and does
not cover.

**Contributions welcome.** The gaps above are the obvious places to start, as is
any locker with a different damage size — the technique holds for anything that
stops early in a file, only the constant changes, and this actor already shipped
two builds that differ by 8 MiB. Two conditions, both in
[CLAUDE.md](CLAUDE.md): host-side tools stay POSIX shell and Python standard
library, and victim-side detail never reaches git history. CI enforces the
mechanical half of that.

Hashes, contacts and the attacker's master public key are published on purpose;
that is what makes an IOC list useful. Host addresses, VM names and credentials
from the incidents are not, for reasons that should be obvious.

There is no malware here and nothing that helps anyone write any. This is the
other half of the problem: work out what survived, and go get it.

## License

MIT. If it gets someone's data back, it did its job.
