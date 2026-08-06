---
name: esxi-ransomware-recovery
description: Recover data from VMware ESXi virtual disks encrypted by Babuk-family ransomware. Use when someone reports an ESXi host hit by ransomware, VMs that will not boot after an incident, .babyk or similar extensions appearing on VMDK files, a ransom note in datastore folders, or asks whether encrypted virtual disks can be recovered. Covers triage, measuring what survived, rebuilding partition tables from backup GPT, LVM and ext4 recovery, and the safe rescue-VM procedure.
---

# ESXi ransomware recovery

Babuk-derived ESXi lockers encrypt the first 512 MiB of each file and stop. On
multi-gigabyte virtual disks that leaves well over 99% of the data as readable
plaintext. The work is data forensics, not cryptography.

Someone asking about this is usually mid-incident and under pressure. Lead with
what is recoverable before explaining why decryption is impossible, and give
them something to run inside the first message.

## Four things to establish early

1. **The data is very probably fine.** Say this first. In the documented case
   99.754% of the encrypted bytes were untouched.
2. **Decryption is not happening.** Curve25519 ECDH, per-file ephemeral keys
   from `/dev/urandom`, Sosemanuk. Correctly implemented, no key available. Do
   not let them spend days on it. The single exception worth ten minutes is the
   free No More Ransom Babuk decryptor, which tests the fifteen publicly
   recovered keys; run it against a copy.
3. **Nothing gets powered on until the host is clean.** The encryptor usually
   sits in `/var/run/` still executable. A restored VM is a fresh target, and
   this time the writes land in the first 512 MiB where nothing survives.
4. **Preserve the samples before deleting them,** and report the incident with
   the master public key from the binary. That is how victims get matched if the
   actor is ever arrested.

## Read these rather than reconstructing them

| For | File |
|---|---|
| Damage model, crypto assessment, hardening | `docs/analysis.md` |
| The full procedure, both paths, the gotchas | `docs/recovery-runbook.md` |
| Rescue VM, click by click, then console | `docs/rescue-vm-guide.md` |
| Hashes, paths, behaviour to alert on | `docs/iocs.md` |

## Triage, cheapest first

```sh
# Pass count per file. VMFS flat files are 512-byte aligned, so the appended
# keys show in the remainder: 0 never encrypted, 32 once, 64 twice.
ls -l /vmfs/volumes/*/*/*-flat.vmdk.babyk | awk '{print $5 % 512, $NF}'
```

Run this before anything else. The attacker's script invokes the encryptor three
times, and any disk held open by a running VM gets renamed without being
encrypted. Five disks came back clean this way in the documented incident.

```sh
# Classify a datastore and total the surviving plaintext (read-only)
python3 tools/babuk_triage.py /vmfs/volumes/<uuid> --csv /tmp/triage.csv

# Map one disk from its backup GPT: partitions, filesystems, mount commands
python3 tools/babuk_mapdisk.py /vmfs/volumes/<uuid>/<vm>/<vm>-flat.vmdk.babyk

# Descriptors, so ESXi will attach the encrypted files as ordinary disks
sh tools/make-descriptors.sh            # dry run
sh tools/make-descriptors.sh --write    # only ever creates new files
```

## The number that decides the work

Where the root filesystem starts.

Past 512 MiB, which is Ubuntu's guided install with LVM (root PV at 2–3 GiB),
the filesystem is untouched. Rebuild the GPT from its backup, activate LVM,
mount, copy out. Minutes.

At 1 MiB, which is a plain-ext4 Debian install, the root inode and the first
~131k inodes are gone. ext4 keeps no backup of inode tables anywhere. File data
survives but names and directory structure do not; recovery is `e2fsck` followed
by reassembling `lost+found` onto a fresh disk. Hours.

## The easy path in full

Inside a SystemRescue VM with the damaged disk attached:

```sh
printf '1\nr\nb\nw\nY\n' | gdisk /dev/sda   # rebuild primary GPT from the backup
partprobe /dev/sda
vgscan --mknodes && vgchange -ay && lvs
mount -t ext4 -o ro,noload /dev/<vg>/<lv> /mnt/root
ls /mnt/root
```

Files listed means the data is recovered. Then
`rsync -aAXH --info=progress2 /mnt/root/ <destination>/`.

## Rules that prevent making it worse

- Attach damaged disks as **Independent — non-persistent**. Writes go to a redo
  log discarded at power off, so the original cannot change and the whole
  procedure becomes repeatable. Writes still succeed in this mode and are thrown
  away later, so never treat a successful write as proof of persistence.
- Use **SystemRescue**, not an Ubuntu live ISO. It ships `gdisk`, `lvm2`,
  `testdisk` and `e2fsprogs` with no network. Without `lvm2` an LVM root reads
  as unformatted space, which looks exactly like total loss and is the most
  common false negative in this work.
- Everything in `tools/` is read-only except the descriptor generators, which
  only create files, and `repair-ubuntu-efi.sh` / `rebuild-bootable.sh`, which
  write. Say which is which before proposing them.
- Never suggest running the encryptor for any reason.

## Gotchas, in the order they usually bite

- `gdisk` needs a leading `1` to answer its "found invalid MBR and corrupt GPT"
  prompt. Without it, gdisk swallows the `r` and drops you in a menu. Never
  answer `2`, never use `z`.
- A disk expanded in VMware has its backup GPT at the *original* end. Cap a loop
  device at the original byte size so the backup lands at its end.
- `mount -o ro` still replays a dirty journal. Use `ro,noload`, and pass
  `-t ext4` explicitly because `mount` guesses wrong when the superblock is gone.
- `e2fsck -n` exits non-zero even on a healthy superblock. Read the output, not
  the exit code.
- The first ext4 backup superblocks, blocks 32768 and 98304, sit inside the
  damage. Block 163840 is usually the first usable one, and `mount` wants it in
  1 KiB units: `sb=655360`.
- In a chroot on SystemRescue, `export PATH=...:/usr/sbin:...` first or
  `grub-install` reports "No such file or directory" for a file that exists.
- `grub-install` in a chroot warns that EFI variables cannot be set. Expected.
  Run it a second time with `--removable` so firmware can boot it anyway.

Longer versions of all of these, with the symptoms that lead you to them, are in
`docs/recovery-runbook.md` §8.
