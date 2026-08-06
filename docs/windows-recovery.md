# Recovering a Windows guest

Until August 2026 this repository said no Windows guest had been recovered here
and pointed you at R-Studio, DMDE or UFS Explorer. That gap is now closed. Two
Windows Server 2022 guests running SQL Server were recovered from two separate
hosts, and the result was the whole database with zero data loss.

The disk-level work is the same as for Linux: measure the boundary, read the
partition table out of the backup GPT, and everything past the damage is
plaintext. What is different is NTFS, and specifically what NTFS does when the
first few hundred megabytes of a volume are gone. This page is about that.

The short version, if you only read one paragraph: **do not try to make the
volume mount.** Extract the files straight out of the MFT instead. Windows will
say RAW and Linux will refuse to mount, and both are refusing over a structure
you do not need in order to read the data.

## Why Windows says RAW, and why that is misleading

Windows has exactly one word for every kind of NTFS damage. A volume whose boot
sector is gone, a volume whose every file is gone, and a volume that is perfectly
readable except for one 4 KiB index block all present identically in Disk
Management: `RAW`. Mid-incident that is worse than useless, because it reads
like total loss.

Linux tells you the truth in one command:

```sh
ntfsfix -n /dev/sdb2        # -n checks, writes nothing
```

```
Mounting volume... ntfs_mst_post_read_fixup_warn: magic: 0x045241ac ...
Corrupt index block signature: vcn 0 inode 5
Failed to open $Secure: No such file or directory
FAILED
```

Inode 5 is the root directory. Its index block lives at the very start of the
volume, so an early-stop encryptor takes it. The `$Secure` line looks like a
second, separate fault and is not: ntfs-3g resolves system files *by name
through the root index*, so once that index is unreadable every system file
looks absent. One broken structure, two alarming error messages.

That is the whole reason the volume will not mount. It is not the boot sector,
which restores fine from its backup, and it is not the MFT, which normally
survives untouched.

## What actually dies

NTFS keeps twelve metadata files at the head of the volume, so they take the
hit. From a 150 GiB Windows system volume that started 117 MiB into the disk,
with the boundary at 520 MiB — meaning the first 403 MiB of the volume were
destroyed:

| Structure | Fate | Recoverable? |
|---|---|---|
| `$MFT` | **survived** | it starts ~3 GiB in — this is the whole ballgame |
| `$LogFile` | survived | |
| `$Bitmap` | survived | |
| `$Volume`, `$BadClus` | survived | |
| `$MFTMirr` | destroyed | yes — it is a copy of the first `$MFT` records |
| `$UpCase` | destroyed | yes — invariant, donate it |
| `$AttrDef` | destroyed | yes — invariant, donate it |
| `$Boot` | destroyed | sector 0 from the backup VBR; 1–15 invariant |
| **root directory index** | destroyed | **no** — only chkdsk rebuilds it |
| `$Secure` | volume-dependent | no |
| `$Extend/$TxfLog` | destroyed | no, and it does not matter |

`$MFT` surviving is the fact everything else rests on. A default NTFS format
puts it at LCN 786432 — 3 GiB into the volume with 4 KiB clusters — because
Windows deliberately reserves an MFT zone away from the front. No early-stop
encryptor reaches it. Check it before anything else:

```sh
python3 tools/ntfs_triage.py disk-flat.vmdk.babyk 545259520
```

```
  [3] Basic data (NTFS)    Basic data partition   bytes 122683392..161484898304 (150.28 GiB)
      first 422576128 bytes (403 MiB) of this partition are destroyed
      boot sector : backup (primary destroyed)  (cluster 4096, record 1024, ...)
      $MFT at LCN 786432 -> byte 3343908864 : SURVIVED
      -- NTFS metadata files --
        $MFT           intact
        $MFTMirr       DESTROYED  (rebuild from $MFT)
        $AttrDef       DESTROYED  (invariant - donate from an intact volume)
        . (root dir)   DESTROYED  (this is what makes Windows say RAW)
        $UpCase        DESTROYED  (invariant - donate from an intact volume)
```

## Two of them are invariant, and there is a donor on the same disk

`$UpCase` (131,072 bytes, the Unicode uppercase table) and `$AttrDef` (2,560
bytes, the attribute definitions) are byte-identical on every NTFS volume
created by the same Windows version. They are not volume-specific data; they are
constants that each volume happens to carry its own copy of.

So they can be donated — and the donor is usually already on the disk. A Windows
install creates a **recovery partition at the end of the disk**, several hundred
gigabytes past the damage on any large volume, and it is the same NTFS version
as C:. In the recovery above, that 618 MiB partition started at byte
161,484,898,304 and was completely untouched.

```sh
# donor = the intact recovery partition; targets = the damaged volumes
python3 tools/repair-ntfs-meta.py disk-flat.vmdk.babyk 161484898304 /path/to/backups \
        disk-flat.vmdk.babyk 122683392 \
        data-flat.vmdk.babyk 16777216            # dry run
```

The tool validates the donor before it trusts it — a real `$UpCase` maps a–z to
A–Z and leaves A–Z and 0–9 alone — because donating 128 KiB of whatever happened
to sit at that offset would be worse than leaving it destroyed.

`$MFTMirr` needs no donor: it is only a copy of the first records of `$MFT`, and
this volume's own `$MFT` is intact.

**Be clear about what this buys you.** It restores four structures correctly and
verifiably. It did **not** make Windows mount the volume, because the root
directory index is volume-specific and cannot be donated. Do it when you intend
to boot the guest and follow with `chkdsk /f`; skip it entirely if you only want
the data.

## The reliable path: read the MFT directly

Every file's `$FILE_NAME` attribute records its parent directory. So the whole
directory tree can be rebuilt from the MFT alone, with the directory indexes
never consulted. That is what `tools/ntfs_extract.py` does, and it is why the
destroyed root index stops mattering.

It reads through the **backup boot sector** when the primary is gone, so it
never writes to the damaged disk at all. No repair, no mount, no rescue VM
strictly required — it will run under the ESXi host's own Python 3 against the
flat file.

```sh
# list first; nothing is written
python3 tools/ntfs_extract.py data-flat.vmdk.babyk 16777216 860065103872 528482304 \
        /vmfs/volumes/DS01/recovered --list "\\Database"

# then extract
python3 tools/ntfs_extract.py data-flat.vmdk.babyk 16777216 860065103872 528482304 \
        /vmfs/volumes/DS01/recovered "\\Database\\Datafile\\" "\\Database\\Logfile\\"
```

The fourth argument is the damage boundary **relative to the volume**, not the
disk: boundary minus partition offset. Get that wrong and the report of which
files are damaged is wrong, which is the one output you cannot afford to have
wrong.

Every file is checked against the size recorded in its MFT record, and any file
whose clusters overlapped the destroyed head is listed explicitly at the end:

```
  1607860224  \Database\Datafile\Example\DF_Data05.ndf  [499974144 bytes from the destroyed head]
...
directories: 33   files: 84   bytes: 88861440609 (82.76 GiB)

1 file(s) overlapped the destroyed head - these are NOT intact:
      476.81 MiB damaged of    1533.38 MiB   \Database\Datafile\Example\DF_Data05.ndf
```

That list is the deliverable. Everything not on it is byte-complete.

### Things that will trip you up

**`$ATTRIBUTE_LIST`.** A large or fragmented file spills its run list into extra
MFT records. Miss those and the file is written short with no error at all — a
silently truncated database. The extractor follows them; anything you write
yourself must too.

**Reserved names on the destination.** Writing to an NTFS target, the Linux
`ntfs3` driver refuses to create `$Extend`, `$ObjId`, `$Quota`, `$Reparse` and
the `$TxfLog` containers, because NTFS reserves those names for itself. You lose
nothing — they are NTFS bookkeeping and a fresh volume builds its own — but the
file count will not match unless you know to expect it.

**ustar's 8 GiB limit.** ESXi's busybox `tar` writes ustar, which cannot
represent a member larger than 8 GiB. A 32 GiB transaction log goes through it
mangled. Use GNU or POSIX format, or stream from Python's `tarfile` with
`format=tarfile.GNU_FORMAT`.

**ESXi's SSH is slow.** Roughly 10 MB/s even streaming `/dev/zero`, against
370 MB/s local disk reads. If you are pulling tens of gigabytes off a host,
compress on the *host* side — the CPU is idle and the link is the bottleneck.
Doing that took one 70 GiB transfer from 97 minutes to 24.

## The registry, and how much of Windows survives

The system volume in that recovery lost 5,656 files totalling 402 MiB, which
sounds catastrophic and was not. Almost all of it was regenerable: .NET native
images under `\Windows\assembly\NativeImages_*`, browser caches, `.evtx` event
logs, print-spooler drivers, `SoftwareDistribution`.

The registry split in a way that matters:

| Hive | Fate |
|---|---|
| `SYSTEM` | intact |
| `SOFTWARE` | intact |
| `SAM` | **destroyed** |
| `SECURITY` | **destroyed** |
| `RegBack\{SYSTEM,SOFTWARE,SAM,SECURITY,DEFAULT}` | **all intact** |

`SYSTEM` and `SOFTWARE` are the two the machine needs to boot, and both live far
enough in to survive. `SAM` and `SECURITY` are small and sit early, so they die —
but `\Windows\System32\config\RegBack\` holds copies, and in this case they were
ten days old. Restoring them costs at most ten days of local-account and LSA
secret changes.

Worth checking before you rely on that: `RegBack` has been empty by default since
Windows 10 1803, so on many builds it is a zero-byte directory. Read the MFT
timestamps to find out how old the copies actually are rather than assuming.

DPAPI master keys under `\Windows\System32\Microsoft\Protect\S-1-5-18` survived,
which is what saves stored service-account credentials.

## Should you try to boot the original C:?

Usually not, and the reason is not the bootloader.

The EFI System Partition is destroyed outright — it is 100 MiB at 1 MiB into the
disk, entirely inside any early-stop damage — but that is a non-issue: format it
FAT32 and run `bcdboot C:\Windows /s S: /f UEFI` from a Windows installer's
recovery console, and it is rebuilt from scratch.

The real problem is what `chkdsk` would have to invent. The root directory index,
`$Secure` and `$Extend/$TxfLog` are all volume-specific and all destroyed, so
`chkdsk /f` has to rebuild the directory tree from `$FILE_NAME` parent
references — and it cannot run at all until the volume mounts, which is the thing
that is broken. That is a loop.

And even if you break it, the system volume in that recovery had **361 damaged
WinSxS component payloads**. WinSxS is Windows' servicing store: it is what every
future cumulative update reads from. A server that boots today and fails its next
update six months from now, for reasons nobody connects back to the incident, is
a bad foundation for a database host.

A fresh Windows install with the recovered data attached is faster, deterministic
and ends with a patchable OS. Very little is actually lost by doing it that way,
because the valuable state is extractable: `master.mdf` carries every SQL login
and server setting, `msdb` carries every Agent job and the backup history, and
the intact `SYSTEM` and `SOFTWARE` hives can be read offline to reproduce
services, scheduled tasks, firewall rules and installed software.

## SQL Server specifics

Everything above gets you files. Getting a *database* back has its own rules.

**Check what is damaged before you touch anything.** With 8 KiB pages, the first
477 MiB of a data file is roughly 61,000 pages — but the damage also takes page 0,
the file header, and the GAM/SGAM/PFS allocation bitmaps. On a file smaller than
about 4 GiB the entire allocation map lives in that first interval, so the intact
pages *after* the damage may be unreachable through normal recovery even though
the bytes are right there. Assume the whole file is at risk, not just the
destroyed portion.

**Verify the files really are databases.** Page 0 of a data file is a file header
page — `m_type = 15`. Reading it straight off the volume is a two-line check and
distinguishes a genuine intact file from a same-sized region of ciphertext.

**`tempdb` is free.** SQL Server recreates it at every startup. Never spend
bandwidth or worry on it. In one of these recoveries the entire 477 MiB of
destruction landed on a `tempdb` file, which meant the ransomware destroyed
nothing of value on that machine at all.

**Set SQL to manual start before the first boot.** Otherwise the instance comes
up, finds a damaged file and starts making decisions about a database you have
not finished assessing. Offline, from a recovery console:

```
reg load HKLM\TMP W:\Windows\System32\config\SYSTEM
reg add "HKLM\TMP\ControlSet001\Services\MSSQL$INSTANCE" /v Start /t REG_DWORD /d 3 /f
reg unload HKLM\TMP
```

**Do not reach for `REPAIR_ALLOW_DATA_LOSS` first.** It is irreversible and it
deallocates pages — which is data loss by design, not recovery. Exhaust the
alternatives: a file-level restore from backup plus the intact transaction log is
a zero-loss path, and so is a synchronous replica.

### The replica is worth more than any repair

If the database had an AlwaysOn synchronous-commit secondary, go and look at it
before you repair anything. A sync-commit secondary hardens every transaction to
its own log before the primary acknowledges the commit, so its copy is
transactionally identical to the primary at the moment of the attack.

Both hosts in this incident were hit by the same build with the same 520 MiB
boundary, two passes each, identical VM layouts. The only difference was which
file happened to occupy the front of the data volume:

| | Primary | Secondary |
|---|---|---|
| File hit by the 477 MiB | a live data file | a `tempdb` file |
| Production data lost | 477 MiB | **none** |

Same ransomware, same constant, opposite outcome. On the primary the answer
would have been emergency-mode repair and an estimated 2–8% of rows gone; from
the secondary it was a complete database.

Three checks confirm a replica copy is genuinely current, and all three are
worth doing before you trust it:

- every data file carries a valid `type=15` header page at page 0
- the MFT timestamps run right up to the attack — in this case files were
  written at the exact minute the encryptor ran, so the replica was live
- file sizes match the primary's exactly across every file, which is the
  signature of a real replica sharing the primary's file geometry

## What is still untested here

NTFS volumes with 4 KiB rather than 512-byte sectors. ReFS, which has no
equivalent of any of this. Dynamic disks and Storage Spaces. BitLocker — if the
volume was encrypted, the metadata in the damaged head is BitLocker's, and none
of the above applies.
