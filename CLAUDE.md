# Working in this repository

This is recovery documentation for Babuk-family ESXi ransomware. Defensive
work: analysis, indicators, and a procedure for getting data back. There is no
malware sample here and none should ever be added.

## The premise everything rests on

The encryptor damages only the first 512 MiB (`0x20000000` bytes) of each file.
Everything past that offset is untouched plaintext. Every tool, every procedure
and every claim in these docs follows from that one measurement, so if you are
changing something and it stops being consistent with it, the change is wrong.

If you are helping someone mid-incident, start from `docs/recovery-runbook.md`.
It is the operational procedure. `docs/analysis.md` is the reasoning behind it.

## Non-negotiables

**Never suggest running the encryptor.** Not to test, not to confirm scope. On a
repeat pass it reports zero files encrypted, which looks harmless until you
notice it skipped everything only because the files already carried the `.babyk`
extension. Restored data will not be skipped.

**Assume read-only until told otherwise.** Everything in `tools/` is read-only
except the two descriptor generators, which only create new files, and
`repair-ubuntu-efi.sh` / `rebuild-bootable.sh`, which write. When proposing a
command that writes, say so and say what it writes to.

**Damaged disks get attached Independent — non-persistent.** Guest writes go to
a redo log that is discarded at power off, so the original flat file cannot be
modified and the whole procedure becomes repeatable. Writes in that mode still
succeed, they are just thrown away afterwards, so a successful write is not
evidence of persistence.

**Do not tell anyone their data is gone until the checks are done.** The most
common false negative in this work: a rescue environment without `lvm2` shows an
LVM root as unformatted space. It looks identical to total loss. Confirm with
`vgscan`, `vgchange -ay`, `lvs` before concluding anything.

**Decryption is closed.** Curve25519 ECDH with per-file ephemeral keys from
`/dev/urandom`, Sosemanuk for bulk, no implementation flaw, master private key
held by the attacker. Recovering it means solving ECDLP at roughly 2^126
operations. The only decryption avenue worth ten minutes is the No More Ransom
Babuk decryptor against a copy, because it tests the fifteen keys that are
public.

## Order of work

Cheapest checks first, because two of them sometimes end the job:

1. `size % 512` per file. A `0` means the file was renamed but never encrypted.
2. `tools/babuk_triage.py` over a datastore for classification and surviving-byte
   totals.
3. `tools/babuk_mapdisk.py` on one disk for its partition table, filesystems and
   mount commands.
4. Only then build a rescue VM and start mounting things.

The number that predicts difficulty is where the root filesystem starts. Past
512 MiB means the filesystem is clean and only the bootloader needs rebuilding.
Before it means the root inode and early inode tables are destroyed, ext4 keeps
no backup of those, and the guest needs `e2fsck` plus reassembly from
`lost+found`.

## Tools

| File | Writes? | Purpose |
|---|---|---|
| `tools/babuk_triage.py` | no | classify files, measure surviving plaintext, confirm the damage boundary |
| `tools/babuk_mapdisk.py` | no | rebuild one disk's map from backup GPT, backup VBR, ext4 backup superblocks |
| `tools/babuk_fleetscan.py` | no | run the mapper host-wide, CSV out; imports `babuk_mapdisk` |
| `tools/find_fs.py` | no | signature scan for ext4/XFS/btrfs/LUKS/LVM when the head is gone |
| `tools/make_descriptors.py` | creates only | generate `-recovered.vmdk` descriptors |
| `tools/make-descriptors.sh` | creates only | same in POSIX shell, so nothing needs uploading to the host |
| `tools/repair-ubuntu-efi.sh` | yes | easy path: rebuild GPT, recreate the ESP, reinstall GRUB |
| `tools/rebuild-bootable.sh` | yes | hard path: reassemble `lost+found` onto a fresh disk |

They target Python 3 standard library and POSIX shell only, because they run on
the ESXi host where you cannot install anything. Keep it that way: no
third-party imports, no bashisms in the `.sh` files, nothing that assumes a
package manager.

## Writing style here

Guest procedures are written against Ubuntu and Debian. The disk-level technique
is OS-independent, and where a guest is NTFS the docs point at R-Studio, DMDE or
UFS Explorer instead of pretending Linux tooling will read a damaged NTFS volume
properly.

Every gotcha in the runbook cost someone hours. When you add one, describe the
symptom as well as the fix, because the symptom is what the next person will be
searching for at 2am. Prose over bullet fragments. Concrete numbers over
adjectives.

## Adding material from another incident

Publish the attacker side and keep the victim side out. Hashes, extensions,
ransom note text, Tox and onion contacts, embedded keys: all fine, that is what
makes an indicator list useful. Host addresses, VM names, hostnames, MAC
addresses, datastore UUIDs, credentials (including expired ones) and full fleet
inventories: not fine. A complete inventory maps a victim's infrastructure even
after the addresses are swapped out.

Sanitize before the first commit rather than after. Anything that lands in git
history is public permanently, and cleaning it means rebuilding the repository.
