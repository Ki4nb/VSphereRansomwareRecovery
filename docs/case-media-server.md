# Case: a media server with 6.7 TB in lost+found

A worked hard-path recovery, in order, including the parts that went wrong.
Everything identifying has been changed; the technical shape is exact.

The guest: one Linux VM running a containerised application stack — a database,
an object-storage service, a transcoder, and a web front end — with **three
disks spread across three datastores**:

| Disk | Size | Role |
|---|---|---|
| system | 800 GiB | OS, LVM root |
| data | 900 GiB | object storage for the application |
| media | 8 TiB | video library, whole-disk ext4, no partition table |

All three were single-pass encrypted. Read
[`recovery-runbook.md`](recovery-runbook.md) first; this assumes it.

---

## 1. Three folders with the same name

The first thing that bites has nothing to do with ransomware. A VM whose disks
live on different datastores has one folder **per datastore, all named after the
VM**. Every tool that resolves a guest by folder name — including the ones in
this repo, before they were fixed — silently takes whichever datastore it walks
first.

Address them by full path, and check what you got:

```sh
find /vmfs/volumes -name '<vm>-flat.vmdk.babyk' | while read -r f; do
    echo "$(( $(ls -l "$f" | awk '{print $5}') / 1073741824 )) GiB  $f"
done
```

## 2. The system disk was not MBR, it was expanded

`babuk_fleetscan.py` reported no backup GPT, which normally means an MBR disk
and a `testdisk` job. It was wrong — or rather, it was right about the device
end and that was the wrong place to look.

The disk had been **grown in VMware from 500 GiB to 800 GiB**, so its secondary
GPT was still sitting at the old end, 300 GiB from the bottom of the device.

```
sh tools/find-backup-gpt.sh <flat file>

  filesystems found:
     start 0x43400000     size       2.00 GiB   <- /boot
     start 0xc3500000     size     496.95 GiB   <- LVM root
  scanning 0x7cffd00000 - 0x7d0fd00000 for 'EFI PART' ...
  FOUND backup GPT header at 0x7cfffffe00 (LBA 1048575999)
  ORIGINAL DISK SIZE = 536870912000 bytes (500.00 GiB)
```

Both filesystems start past the 512 MiB damage line, so the system disk was
never a hard-path problem at all. It was the easy path wearing a disguise: cap a
loop device at 500 GiB, rebuild the GPT from the backup, `sgdisk -e` afterwards
to move the secondary to the real end.

**The lesson:** "no backup GPT" means *no backup GPT at the device end*. Before
concluding MBR, check whether the partitions add up to the size of the disk.

## 3. It would not boot, and the disk was fine

The repair worked. The guest still would not come up, because its `fstab`
declared both data volumes as required mounts:

```
/dev/sdb1                            /data   ext4  defaults  0 2
UUID=<media-uuid>                    /media  ext4  defaults  0 2
/media/library    /data/library      none    bind  0 0
```

fsck pass `2` on a volume whose superblock is destroyed means systemd waits,
`fsck` fails, and the guest drops to an emergency shell — on a system disk that
is in perfect condition.

Patch it before first boot. `nofail` and pass `0`:

```
/dev/sdb1  /data  ext4  defaults,nofail,x-systemd.device-timeout=10  0 0
```

Keep a copy of the original. After that it booted clean, with zero failed units,
and could be worked on live — which is the whole point: get the guest up on its
system disk, then treat the data volumes as a separate problem.

## 4. The backup superblock lied about how much data there was

With the guest running, both data volumes were attached
`independent-persistent`, alongside a fresh 2 TB scratch disk.

Reading each volume's surviving backup superblock suggested they were nearly
empty:

| Volume | Total blocks | Free blocks | Implied usage |
|---|---|---|---|
| data | 235,929,088 | 231,944,003 | ~16 GB |
| media | 2,147,483,648 | 2,130,265,570 | ~70 GB |

That would have meant 86 GB of data and an easy copy onto the scratch disk. It
was wrong. After a full `e2fsck` the real figures were **577 GiB and 6.1 TiB**,
across 678,114 and 2,668,605 files.

**Free-block counts in a backup superblock are stale.** They are written at
`mkfs` time and not maintained. Believing them would have meant provisioning a
tenth of the storage actually needed and discovering it mid-copy.

## 5. Repairing without committing

6.1 TiB does not fit on a 2 TB scratch disk, so "recover onto a fresh disk"
was not available. Instead, each volume got a device-mapper snapshot with its
COW store on the scratch disk — `e2fsck` writes to the COW, the originals stay
byte-identical:

```sh
truncate -s 400G /mnt/work/cow.img
COW=$(losetup -f --show /mnt/work/cow.img)
SZ=$(blockdev --getsz /dev/sdc)
dmsetup create snap --table "0 $SZ snapshot /dev/sdc $COW P 8"
e2fsck -fy -b 163840 -B 4096 /dev/mapper/snap
```

`e2fsck` did what the model predicts when the root inode is inside the damage:

```
Inode 2 seems to contain garbage.  Clear? yes
Root inode is not a directory.  Clear? yes
```

Both COW stores stayed under 3 GB. Repairing an 8 TiB filesystem costs
gigabytes, not terabytes, because only metadata changes.

## 6. Repaired, clean, and still would not mount

```
EXT4-fs error (device dm-2): ext4_init_orphan_info:611: orphan file block 4: bad magic
EXT4-fs (dm-2): mount failed
```

The `e2fsck` log explained it:

```
Creating orphan file (512 blocks): Error writing block 4056799261
ext2fs_block_alloc_stats: Illegal block number: 3614656343
```

`e2fsck` had rebuilt ext4's optional **`orphan_file`** using the block bitmaps —
which are garbage in the destroyed groups — so every block it allocated was past
the end of the device. A 900 GiB volume has no block 3,614,656,343.

`orphan_file` is a performance feature. Drop it:

```sh
tune2fs -O ^orphan_file /dev/mapper/snap
e2fsck -fy /dev/mapper/snap
mount -o ro /dev/mapper/snap /mnt/check
```

Both volumes mounted immediately. **This is the single most valuable thing in
this document:** at the point where it says `bad magic` and refuses to mount,
the filesystem is fully intact and one `tune2fs` away from readable. It is very
easy to conclude the opposite.

## 7. Working out what the directories were called

Everything landed in `lost+found`, because the root directory's entries died
with the root inode. Names *inside* each recovered tree survive; only the
top-level names are gone.

```
data volume : 979 entries, one tree of 563 GB
media volume:  32 entries, one tree of 6.0 TB
```

Contents identify the big ones — the 6 TB tree held 2,323 UUID-named folders,
each containing `480p/seg_001.ts`-style HLS segments, so it was obviously the
media library. But "obviously the media library" is not a path, and the
application needs the exact one.

The running containers knew:

```sh
docker inspect $(docker ps -q) | grep -oE '"Source": "/data[^"]*"' | sort -u
  "Source": "/data/library"
  "Source": "/data/object-store"
```

That is the authoritative answer, straight from the workload rather than from
guesswork. Rename the trees to match, and the bind mount in `fstab` lines up
again.

## 8. Committing

With both volumes verified through their snapshots, the repairs were merged
down into the originals:

```sh
dmsetup remove snap
dmsetup create m-snap --table "0 $SZ snapshot-merge /dev/sdc $COW P 8"
dmsetup status m-snap        # poll until allocated falls to the metadata baseline
dmsetup remove m-snap
```

Both merges finished in about a minute and both volumes then reported
`Filesystem state: clean` on the real disks.

## 9. Two more traps on the way out

**`sgdisk` refused to write the data volume's partition table.** Its filesystem
starts 1 MiB in, so it needs a partition for `fstab` to find it at `/dev/sdb1` —
but `sgdisk` was still parsing the garbage MBR in the encrypted head:

```
Problem: MBR partitions 3 and 4 overlap!
Warning! An error was reported when writing the partition table!
```

`sgdisk --zap-all` first, then write the GPT. Verify the superblock immediately
before and after — via a loop device at the filesystem's offset — so you can
prove you did not cross it.

This one also failed *quietly enough to be misleading*: the partition was never
created, `/data` never mounted, and the directories visible at `/data` were
empty stubs the container runtime had created on the root filesystem. It looked
like a successful recovery until the mount was actually checked.

**The containers could not see any of it.** They had started before `/data` was
mounted, so their bind mounts resolved to those empty stubs. Docker resolves
bind mounts at container start, so `docker restart $(docker ps -q)` is enough —
no recreate needed — but until you do it the application serves nothing and the
recovery looks like it failed.

---

## What this case is worth remembering for

- "No backup GPT" can mean the disk was expanded, not that it is MBR.
- Get the guest booting on its system disk first, with damaged data volumes set
  `nofail`. Debugging a data volume on a running system beats debugging it in a
  rescue environment.
- Backup superblock free-block counts are stale by an order of magnitude.
- A dm snapshot is not only protection, it is a **stage**: repair, inspect,
  then merge. That works on volumes far too large to copy.
- `orphan_file` will make a perfectly recovered filesystem look destroyed.
- The workload itself is the best source for what the directories were called.
