# Getting your data back after Babuk-family ransomware hits ESXi

If you found this because your ESXi host is showing a ransom banner, every VM is
powered off, and your virtual disks now end in `.babyk`: the situation is much
better than it looks right now. Take the next five minutes to read this before
you do anything irreversible.

Two things are true at once.

**You will not decrypt these files.** The crypto is Curve25519 ECDH with a
per-file ephemeral key from `/dev/urandom` and Sosemanuk for the bulk. It is
implemented correctly. There is no flaw to attack and no key to find. Anyone who
tells you otherwise is selling something.

**You have probably lost almost nothing.** The encryptor stops after 512 MiB per
file. In the incident this repository documents, that meant 6,911.95 GiB of
"encrypted" virtual disks contained 6,894.93 GiB of perfectly readable
plaintext. 99.754% of the bytes were never touched. Most Linux guests came back
in under ten minutes each.

The rest of this is how.

**What this covers:** ESXi ransomware recovery and VMware ransomware recovery
end to end, from `.babyk` VMDK recovery on VMFS datastores through GPT partition
table rebuild, LVM recovery and ext4 recovery inside the guest, plus the
ransomware incident response steps around all of it (evidence, IOCs, cleaning
the host). Babuk ransomware and its ESXi descendants specifically, though the
damage model applies to any locker that stops early in a file.

---

## Why 512 MiB matters so much

Whoever built this made a speed choice. On a hypervisor you don't need to encrypt
a 2 TB disk to take it hostage. You only need to destroy the front of it, where
the partition table and the bootloader live, and then the VM won't boot and the
disk looks like garbage. So the encryptor writes `0x20000000` bytes, appends a
32-byte key, and moves to the next file.

Here is a 100 GiB disk after it ran:

```
[ 512 MiB destroyed ][ --------------- 99.5 GiB, untouched --------------- ]
  partition table      /boot, LVM, databases, home directories, everything
  EFI bootloader
```

You can watch the boundary in the file itself. Sample zero-byte density every
64 KiB: encrypted data is uniform random and lands near 0.4% zeros, while real
filesystem data is much higher and sparse regions are almost all zeros. These
are real readings from one of the recovered disks:

```
offset      0 MiB : 252 zeros per 64 KiB   ciphertext
offset    128 MiB : 252                    ciphertext
offset    256 MiB : 276                    ciphertext
offset    511 MiB : 261                    ciphertext
offset    512 MiB : 246                    boundary
offset    600 MiB : 16                      sparse, never touched
offset   1024 MiB : 16                      sparse, never touched
offset  16384 MiB : 27702                   plaintext data
offset  98304 MiB : 23895                   plaintext data
```

It stops dead at 512 MiB, every time, on every file.

## Filesystems keep spare copies of the parts that died

This is the part that turns a bad day into a long afternoon. The structures
sitting in that first 512 MiB are exactly the ones filesystem designers decided
were too important to keep only one copy of, and the spares live at the *end* of
the volume, where the encryptor never reaches.

| What died | Where the spare is | Usable? |
|---|---|---|
| GPT partition table (LBA 1) | last sector of the disk | yes, `gdisk` rebuilds from it |
| NTFS boot sector | last sector of the partition | yes |
| ext2/3/4 superblock | block groups 1, 3, 5, 7, 9, 25… | yes, though the first two are usually inside the damage |
| LVM2 PV label | no spare, but PVs normally start at 2–3 GiB | intact if the PV starts past 512 MiB |
| ext4 inode tables | **nowhere** | this is the one that hurts |

So recovery is mechanical: read the backup structure, rebuild the primary from
it, mount, copy the data out.

## One number tells you how bad each VM is

Where does the root filesystem start?

If it starts past 512 MiB, which is what you get from Ubuntu's default guided
install with LVM (root PV around 2–3 GiB in), the filesystem is completely
untouched. Nothing is corrupt. You lost the bootloader and nothing else, and you
are about five commands from having your files back.

If it starts at 1 MiB, which is what a plain-ext4 Debian install gives you, then
the root inode and the first ~131,000 inodes were inside the blast radius. ext4's
`flex_bg` packs the inode tables for 16 block groups together at the front of the
filesystem, and unlike superblocks, **inode tables have no backup copies
anywhere**. The file data is still on the disk. The names and the directory tree
are not. That guest needs `e2fsck` and then manual reassembly out of
`lost+found`, which took about two hours in practice.

Both are recoverable. One is coffee, the other is an afternoon.

---

## Where to go next

- **[docs/analysis.md](docs/analysis.md)** — what the malware actually does, step
  by step, plus the full cryptographic assessment and why the answer on
  decryption is final.
- **[docs/recovery-runbook.md](docs/recovery-runbook.md)** — the working
  procedure. Triage, both recovery paths, and the mistakes that cost hours the
  first time.
- **[docs/rescue-vm-guide.md](docs/rescue-vm-guide.md)** — click-by-click in the
  ESXi web UI, then the console commands. Start here if you want the shortest
  path to seeing your files listed.
- **[docs/iocs.md](docs/iocs.md)** — hashes, paths and behavioural indicators.

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
script runs the encryptor three times in a row, and any disk still held open by a
running VM gets renamed without being encrypted. Five disks on the host in this
incident, around 325 GiB, turned out to be completely intact. They needed a new
descriptor file and nothing else.

```sh
# Classify a whole datastore and total up the surviving plaintext
python3 tools/babuk_triage.py /vmfs/volumes/<uuid> --csv /tmp/triage.csv

# Map one disk: partition table from the backup GPT, filesystems found,
# and the exact losetup/mount commands for what survived
python3 tools/babuk_mapdisk.py /vmfs/volumes/<uuid>/<vm>/<vm>-flat.vmdk.babyk

# Generate the descriptors ESXi needs to attach these files as normal disks
sh tools/make-descriptors.sh            # dry run first
sh tools/make-descriptors.sh --write
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
renamed, copied or modified. `tools/make-descriptors.sh` does the arithmetic for
every damaged file on the host and refuses to overwrite anything.

## Four rules that keep this safe

**Attach damaged disks as `Independent — non-persistent`.** Every write the
rescue guest makes goes into a redo log that gets thrown away at power off. The
original file physically cannot change, which means you can experiment, get it
wrong, and start over. Worth knowing: writes in this mode still *succeed*, they
are just discarded later, so a successful write proves nothing about
persistence. Confirm the setting in the UI.

**Clean the host before you power anything on.** The encryptor is usually still
sitting in `/var/run/`, executable. A VM you just restored is a fresh target, and
this time the new writes land in the first 512 MiB where nothing survives.

**Never run the encryptor to "check" something.** On its final pass in this
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

## Questions people ask at hour one

### Can Babuk ransomware be decrypted?

Not this variant, and almost certainly not yours. Curve25519 ECDH with a
per-file ephemeral key drawn from `/dev/urandom`, SHA-256 to derive, Sosemanuk
to encrypt. It is implemented correctly and the master private key sits with the
attacker. Fifteen Babuk private keys *are* public, from the 2021 source leak plus
the Tortilla key Cisco Talos recovered, so run the No More Ransom decryptor
against a copy of one file before you accept that answer. It takes ten minutes.

### What is a .babyk file, and can I open it?

It is one of your files with the first 512 MiB overwritten and 32 bytes of key
appended. Nothing opens it directly. On a virtual disk, everything past the
512 MiB mark is still your original data, which is why the recovery works.

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

### How do I recover an LVM volume after ransomware?

If the physical volume starts past 512 MiB, which is normal on an Ubuntu guided
install, the LVM label and metadata are untouched. `vgscan --mknodes`,
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
before anyone quotes you a price for it. In this case the answer was 0.246%.

## About the tools

Python 3 standard library and POSIX shell only, because they need to run on the
ESXi host itself, where you cannot install anything. Everything is read-only
except the two descriptor generators, which only ever create new files, and the
two repair scripts, which say so plainly.

| Tool | What it does |
|---|---|
| `babuk_triage.py` | Walks a datastore, classifies every file, totals the surviving plaintext, confirms the damage boundary by entropy |
| `babuk_mapdisk.py` | Rebuilds one disk's layout from the backup GPT and backup boot sectors, finds `$MFT` and ext4 backup superblocks |
| `babuk_fleetscan.py` | Runs the mapper across the whole host, one line per partition, CSV out |
| `find_fs.py` | Signature scan for ext4/XFS/btrfs/LUKS/LVM when the head is gone and you have no idea where anything starts |
| `make_descriptors.py`, `make-descriptors.sh` | Generate `-recovered.vmdk` descriptors |
| `repair-ubuntu-efi.sh` | The easy path, scripted: rebuild GPT, recreate the ESP, reinstall GRUB |
| `rebuild-bootable.sh` | The hard path, worked through: reassemble `lost+found` onto a fresh disk |

## Scope

Guest recovery here is written against Ubuntu and Debian, since that is what the
fleet ran. The disk-level work applies to any guest. Where a VM was Windows, the
notes send you to R-Studio, DMDE or UFS Explorer rather than pretending Linux
tooling will read a damaged NTFS volume properly.

Hashes, contacts and the attacker's master public key are published on purpose;
that is what makes an IOC list useful. Host addresses, VM names and credentials
from the incident are not, for reasons that should be obvious.

There is no malware here and nothing that helps anyone write any. This is the
other half of the problem: work out what survived, and go get it.

## License

MIT. If it gets someone's data back, it did its job.
