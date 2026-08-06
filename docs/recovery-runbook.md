# ESXi ransomware recovery — operational runbook

The procedure, in the order you actually do it. Read
[`analysis.md`](analysis.md) first for why any of this works.

Written for **Ubuntu and Debian** guests, which is where the guest-side detail
matters. The disk-level technique is OS-independent.


---

## 1. The one fact everything rests on

The encryptor destroys **only the first `0x20000000` bytes (512 MiB)** of each
file, then appends a 32-byte key. Everything past 512 MiB is untouched
plaintext. **There is no way to decrypt** — Curve25519 ECDH + `/dev/urandom`
per-file keys, no flaw, no key available. All recovery is about the surviving
99%.

**Pass count from file size** (VMFS flat files are always 512-aligned):

| `size % 512` | Meaning |
|---|---|
| `0` | **never encrypted** — renamed only, needs nothing but a descriptor |
| `32` | encrypted once |
| `64` | encrypted twice |

Some disks routinely come back `0` — the orchestration script runs the
encryptor several times, and a disk still locked by a running VM gets skipped
but renamed anyway. **Always check this first on a new host; it's free and it
sometimes ends the job.**

---

## 2. Outcome is decided by where the root filesystem starts

This single number predicts how hard each VM will be:

| Root FS starts | Result | Work required |
|---|---|---|
| **> 512 MiB** (LVM at 2–3 GiB) | filesystem **clean**, zero data loss | bootloader only, ~5 min |
| **< 512 MiB** (ext4 at 1–2 MiB) | root inode + first ~131k inodes destroyed | full e2fsck + reassembly, ~2 hrs |

Why: `flex_bg` packs the inode tables for 16 block groups at the start of the
filesystem. **Inode tables have no backup copies anywhere.** If the filesystem
starts inside the blast radius, its metadata is gone permanently — but the file
*data* past 512 MiB still survives.

Ubuntu-with-LVM installs almost always land in the easy category (root at
~3 GiB). Debian-with-plain-ext4 installs land in the hard one (root at 1 MiB).

---

## 3. Access

Pin these down before anything else; every later step assumes them:

| What | Notes |
|---|---|
| Compromised ESXi host | root over SSH. An older host may only offer RSA keys — add `-o PubkeyAcceptedKeyTypes=+ssh-rsa` |
| Rescue VMs (SystemRescue) | one per disk you want to work in parallel; root password is set at boot |
| Every other hypervisor in the fleet | enumerate them early — these lockers are typically run against every reachable host |

Generate a dedicated IR keypair for the engagement rather than reusing an admin
key, and revoke it when you are done. Treat the host's existing
`authorized_keys` as attacker-controlled until you have read it yourself.

Windows has no password-capable `ssh`. Use **Posh-SSH**:

```powershell
Import-Module Posh-SSH
$pw = ConvertTo-SecureString '<rescue-root-password>' -AsPlainText -Force
$cred = New-Object System.Management.Automation.PSCredential('root', $pw)
$s = New-SSHSession -ComputerName <ip> -Credential $cred -AcceptKey -Force -ConnectionTimeout 30
(Invoke-SSHCommand -SessionId $s.SessionId -Command 'uname -a' -TimeOut 60).Output
Remove-SSHSession -SessionId $s.SessionId | Out-Null
```

Rescue VMs regenerate SSH host keys on reboot → clear the cache or use `-Force`:
`Get-SSHTrustedHost | Where HostName -like "*<ip>*" | Remove-SSHTrustedHost`

**Getting scripts onto a rescue VM** when SCP is awkward — base64 inline is
reliable and needs nothing installed:

```powershell
$b64 = [Convert]::ToBase64String([Text.Encoding]::UTF8.GetBytes(($txt -replace "`r`n","`n")))
Invoke-SSHCommand -SessionId $s.SessionId -Command "echo '$b64' | base64 -d > /tmp/x.sh && bash -n /tmp/x.sh"
```

---

## 4. Workflow for each remaining VM

### 4.1 On the ESXi host — triage (read-only)

```sh
# real volume paths - find(1) does NOT follow the /vmfs/volumes symlinks
V=/vmfs/volumes/5ea1d0c0-11111111-2222-333333333333
D=/vmfs/volumes/5ea1d0c0-44444444-5555-666666666666

python3 /tmp/ir_fleetscan.py "$V" "$D" --csv /tmp/fleet.csv   # per-partition map
sh /tmp/mkdesc.sh --write                                     # descriptors
```

`make-descriptors.sh` (pure shell, nothing to upload) creates
`<name>-recovered.vmdk` next to each flat file. It declares the extent as
`original_size / 512` sectors, which hides the appended key bytes **and**
restores 512-byte alignment. Non-destructive — creates new files only.

Verify with `vmkfstools -e <name>-recovered.vmdk` → "Disk chain is consistent."

### 4.2 Build a rescue VM

SystemRescue ISO (<https://www.system-rescue.org/Download/>) — **not** Ubuntu
live, which lacks `gdisk`/`lvm2` offline. Without `lvm2` an LVM root looks like
unformatted space and you will wrongly conclude the data is gone.

VM: 2 vCPU, 4 GB RAM, **no disk of its own**, ISO connected at power on,
network adapter disconnected. Then **Add hard disk → Existing hard disk →
`<name>-recovered.vmdk`**, and set **Disk Mode**:

- **Independent – non-persistent** while exploring — all writes discarded at
  power off, so nothing you do can hurt the data.
- **Dependent** when committing the repair.

> Non-persistent does **not** make writes fail. They succeed and are silently
> thrown away later. A write probe therefore proves nothing about persistence —
> confirm the setting in the UI, or power-cycle and check `lsblk`.

### 4.3 In the rescue VM — identify

```sh
lsblk -o NAME,SIZE,TYPE,FSTYPE
dd if=/dev/sda bs=512 skip=1 count=1 | head -c 8      # primary GPT (expect garbage)
SZ=$(blockdev --getsz /dev/sda)
dd if=/dev/sda bs=512 skip=$((SZ-1)) count=1 | head -c 8   # backup GPT (expect "EFI PART")
sgdisk -p /dev/sda                                     # reports Backup header: OK?
```

Then map the damage — 64 KiB probes, zero-density tells you everything:

```sh
python3 - <<'EOF'
f=open('/dev/sda','rb')
for gb in [0,0.25,0.5,0.6,1,2,3,3.3,4,8,16,32,64]:
    f.seek(int(gb*(1<<30))); b=f.read(65536)
    if not b: break
    p=100.0*b.count(0)/len(b)
    print("%7.2f GiB zero=%6.2f%% %s"%(gb,p,"EMPTY" if p>99 else ("cipher" if p<1.5 else "DATA")))
EOF
```

`zero≈0.4%` = uniform random = ciphertext. Expect it to stop at 0.5 GiB.
High-entropy readings *past* 0.5 GiB are usually compressed initrds, not damage.

### 4.4 Restore the partition table

```sh
printf '1\nr\nb\nw\nY\n' | gdisk /dev/sda
partprobe /dev/sda
```

The leading `1` answers gdisk's *"Found invalid MBR and corrupt GPT — 1 use
current GPT / 2 create blank"* prompt. **Never answer 2**, and never use `z`.
Without that leading `1`, gdisk eats your `r` and dumps you in the menu.

**If the disk was expanded in VMware**, the backup GPT is at the *original* end,
not the device end, and gdisk finds nothing — so an expanded disk is easy to
mistake for an MBR disk with nothing to recover from. Detect it: LVM `PSize` and
the partition offsets will be smaller than the device.

`tools/find-backup-gpt.sh` locates the original end for you. It reads the last
ext4 filesystem's own geometry, scans just past it for `EFI PART`, and reports
the size the GPT believes the disk to be:

```sh
sh tools/find-backup-gpt.sh /vmfs/volumes/<uuid>/<vm>/<vm>-flat.vmdk.babyk
```

On one 800 GiB disk that returned exactly 500 GiB, with a self-consistent header
at LBA 1048575999. Then cap a loop device at that size so the backup lands at
its end:

```sh
losetup --sizelimit <original_bytes> -f --show /dev/sda
printf '1\nr\nb\nw\nY\n' | gdisk /dev/loopN
losetup -d /dev/loopN
partprobe /dev/sda
sgdisk -e /dev/sda        # move the secondary GPT to the real device end
```

The `sgdisk -e` afterwards is what makes the table self-consistent again on the
enlarged device, and it also releases the extra space for use. `recover-easy-
path.sh` detects and handles this case without being told.

If there is no backup GPT at all (MBR disk): `testdisk /dev/sda` → Intel →
Analyse → **Deeper Search** (Quick Search finds nothing because the primary
superblock is inside the damage). Pressing `P` failing to list files is
**expected** — write the table anyway, the geometry is what matters.

### 4.5 Mount and classify

```sh
vgscan --mknodes && vgchange -ay && lvs
mount -t ext4 -o ro,noload /dev/ubuntu-vg/ubuntu-lv /mnt/root   # noload = skip dirty journal
ls /mnt/root && cat /mnt/root/etc/os-release
```

- Mounts and shows `etc home usr var` → **easy path**, go to §5.
- `Structure needs cleaning` → error flag set, needs a writing `e2fsck`, §6.
- `bad magic` → wrong superblock; find the right one, §7.

---

## 5. Easy path — root FS clean, bootloader only

Only the boot partition was damaged. Switch disk to **Dependent**, then:

**EFI system** (has an EFI System Partition; fstab has a `/boot/efi` line):

```sh
mkfs.vfat -F32 -i <ESPVOLID> /dev/sda1        # e.g. -i 7B046308 reproduces UUID 7B04-6308
mount /dev/<rootlv> /mnt/sys
mount /dev/sda2 /mnt/sys/boot
mkdir -p /mnt/sys/boot/efi && mount /dev/sda1 /mnt/sys/boot/efi
for d in dev dev/pts proc sys run; do mount --bind /$d /mnt/sys/$d; done
P='export PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin'
chroot /mnt/sys /bin/bash -c "$P; grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=ubuntu --recheck"
chroot /mnt/sys /bin/bash -c "$P; grub-install --target=x86_64-efi --efi-directory=/boot/efi --removable --recheck"
chroot /mnt/sys /bin/bash -c "$P; update-grub && update-initramfs -u"
```

**BIOS system** (BIOS boot partition, no `/boot/efi` in fstab):

```sh
chroot /mnt/sys /bin/bash -c "$P; grub-install --target=i386-pc --recheck /dev/sda"
chroot /mnt/sys /bin/bash -c "$P; update-grub"
```

**If `/boot` itself was destroyed** (starts < 512 MiB), recreate it and
re-download the kernel — `/boot` holds no user data:

```sh
mkfs.ext4 -F -U <original-boot-uuid> /dev/sda2
# ... mount, bind, then give the chroot DNS:
unlink /mnt/sys/etc/resolv.conf          # it's a symlink into /run
printf 'nameserver 1.1.1.1\n' > /mnt/sys/etc/resolv.conf
chroot /mnt/sys /bin/bash -c "$P; apt-get update -qq && apt-get install --reinstall -y linux-image-<ver>"
chroot /mnt/sys /bin/bash -c "$P; update-initramfs -c -k all"
# then grub-install as above, and restore the resolv.conf symlink
```

Get `<ver>` from `ls /mnt/sys/usr/lib/modules/` — module trees live on the root
FS and survive.

**Always reuse the original UUIDs** (`mkfs.ext4 -U`, `mkfs.vfat -i`). fstab then
needs no edits at all, which removes the most common "boots to `(initramfs)`"
failure.

---

## 6. Hard path — root FS metadata destroyed

Root inode and early inode tables are gone. Recover to a **fresh disk**; do not
try to fix in place.

1. Protect the source with a device-mapper snapshot so repair writes never touch
   it. The swap partition is a free COW store (its contents are worthless):

   ```sh
   swapoff /dev/sda2
   SZ=$(blockdev --getsz /dev/sda1)
   dmsetup create cow --table "0 $SZ snapshot /dev/sda1 /dev/sda2 P 8"
   ```

2. `e2fsck -fy -b <backup-sb> -B 4096 /dev/mapper/cow` — it must write, so it
   cannot run with `-n`. Expect it to clear the root inode and rebuild it.
3. Mount and identify the orphaned trees in `lost+found` by their contents:
   `etc` has `passwd`/`fstab`, `var` has `lib log spool`, `usr` has
   `bin sbin lib share`, `boot` has `vmlinuz*`.
4. Hot-add a fresh disk, partition it identically, `mkfs` with the **original**
   UUIDs, then `rsync -aHAX --numeric-ids` each tree to its real name.
5. Recreate what was trivially lost: `bin`/`sbin`/`lib`/`lib64` symlinks into
   `usr` (usrmerge), and empty `proc sys run mnt opt srv dev tmp` (`tmp` = 1777).
6. Install GRUB. If the target's `/usr/lib/grub` is missing, install from the
   **rescue** system instead: `grub-install --target=i386-pc --boot-directory=/mnt/out/boot /dev/sdX`.
7. Missing packaged files are all restorable — the dpkg DB survives:
   `dpkg --verify | awk '{print $NF}' | xargs -r dpkg -S | cut -d: -f1 | sort -u`
   then `apt-get install --reinstall <list>`.

Worked example: `tools/rebuild-bootable.sh`.

### When the volume is too big to copy

The instruction above — recover onto a fresh disk, never fix in place — assumes
you have somewhere to put it. On a data volume of a few terabytes you often do
not, and buying that space mid-incident is not always possible.

The snapshot still solves it, because you can **commit** one. Give it a COW
store on a scratch disk rather than on swap, verify the repair through the
snapshot, and only then merge it down:

```sh
# scratch disk attached as /dev/sdd, formatted and mounted at /mnt/work
truncate -s 400G /mnt/work/cow.img
COW=$(losetup -f --show /mnt/work/cow.img)
SZ=$(blockdev --getsz /dev/sdc)
dmsetup create snap --table "0 $SZ snapshot /dev/sdc $COW P 8"

e2fsck -fy -b 163840 -B 4096 /dev/mapper/snap
tune2fs -O ^orphan_file /dev/mapper/snap && e2fsck -fy /dev/mapper/snap
mount -o ro /dev/mapper/snap /mnt/check      # look before you commit
```

If it is right, replay the COW into the original:

```sh
umount /mnt/check
dmsetup remove snap
dmsetup create m-snap --table "0 $SZ snapshot-merge /dev/sdc $COW P 8"
# poll: merge is done when the allocated count falls to the metadata baseline
dmsetup status m-snap
dmsetup remove m-snap
```

An 8 TiB volume merged in under a minute, because only the COW moves — a few
gigabytes of metadata, not the data. Check `dmsetup status` before starting:
a snapshot whose COW filled up is invalid, and merging an invalid snapshot is
not something you want to discover afterwards.

The payoff is that `e2fsck` on an irreplaceable volume stops being a one-way
door. You can run it, look at the result, and still walk away.

---

## 7. Finding the right ext4 backup superblock

The first backups are often *inside* the damage. For a 4 K-block filesystem
starting at the partition start:

| Backup | Offset into FS | Survives 512 MiB damage? |
|---|---|---|
| block 32768 | 128 MiB | no |
| block 98304 | 384 MiB | no |
| **block 163840** | **640 MiB** | **yes** — usually the first usable |
| block 229376 | 896 MiB | yes |

`mount` wants the block number in 1 KiB units: `sb = block * 4096 / 1024`, so
block 163840 → `sb=655360`.

Don't guess — scan. `tools/find_fs.py` locates every ext4 superblock and
back-calculates the true filesystem start from each one's own block-group
number, which also detects an LVM offset (`fsstart=1048576` = LVM, not ext4 at
the partition start).

---

## 8. Gotchas that cost real time

- **`e2fsck -n` returns non-zero even when the superblock is fine.** Never test
  its exit code — read its output.
- **`chroot` + SystemRescue:** Arch's `PATH` omits `/usr/sbin`, so `grub-install`
  and `update-grub` fail with "No such file or directory" even though they exist.
  Always `export PATH=...:/usr/sbin:...` inside the chroot.
- **`grub-install` in a chroot warns "EFI variables cannot be set".** Expected —
  no NVRAM entry gets written. That is exactly why the second `--removable` run
  matters; it writes `\EFI\BOOT\BOOTX64.EFI`, which firmware boots regardless.
- **`mount -o ro` still tries to replay a dirty journal.** Use `ro,noload`.
- **`mount` guesses the wrong FS type** when the superblock is destroyed (you'll
  see bizarre errors like `squashfs: Unknown parameter 'noload'`). Pass `-t ext4`.
- **A `for f in $(find ...)` loop word-splits** on VM folders with spaces
  (`192.0.2.90-prod app 04`). Use `while IFS= read -r`.
- **`losetup --sizelimit`** is the clean way to hide the appended key bytes when
  working on a raw `.babyk` without a descriptor.
- **A repaired ext4 volume that still will not mount.** `e2fsck` finishes, the
  filesystem reports clean, and `mount` fails with
  `ext4_init_orphan_info: orphan file block N: bad magic`. `e2fsck` rebuilt
  ext4's optional `orphan_file` using block bitmaps that are garbage in the
  destroyed groups, so every block it allocated was out of range — the log is
  full of `Illegal block number: 3614656343` on a 900 GiB volume. The feature is
  a performance optimisation, not your data:

  ```sh
  tune2fs -O ^orphan_file /dev/sdXN
  e2fsck -fy /dev/sdXN
  mount -o ro /dev/sdXN /mnt
  ```

  Worth knowing because at that point the volume looks unrecoverable and it is
  completely intact. It hit both data volumes of one guest.
- **Ubuntu puts the LVM PV on a partition typed `8300` "Linux filesystem", not
  `8e00` "Linux LVM".** Selecting the root device by GPT type code therefore
  picks the raw PV, and the mount fails with
  `unknown filesystem type 'LVM2_member'`. Detect LVM at runtime with
  `pvs <partition>` instead of trusting the type.
- **Never stack a loop device over a partition the kernel can already see.** LVM
  then finds the same PV twice and refuses:
  `Cannot activate LVs in VG <vg> while PVs appear on duplicate devices`. The LV
  node is never created and the mount fails with `Can't lookup blockdev`. Use
  the partition directly when the GPT is readable; the loop is only for when the
  partition table is destroyed.
- **`umount /mnt/sys` is not enough after a chroot.** The bind-mounted `/sys`
  holds a nested `efivarfs`, which keeps the mountpoint busy and silently blocks
  `vgchange -an` afterwards. Use `umount -R`.
- **Free-block counts read from a backup superblock are stale.** Two volumes
  looked ~95% empty from their backups and actually held 577 GiB and 6.1 TiB.
  Only trust the figures `e2fsck` prints after a full check — the difference
  decides whether the data fits on the disk you were about to copy it to.
- **A guest with damaged data volumes will not finish booting.** Non-root
  filesystems in `fstab` with an fsck pass of 1 or 2 are required mounts;
  systemd waits for them, `fsck` fails against a destroyed superblock, and the
  guest drops to an emergency shell. Add `nofail` and set the pass to `0` so it
  boots, then repair and mount the volumes by hand on the running system:

  ```
  /dev/sdb1  /data  ext4  defaults,nofail,x-systemd.device-timeout=10  0 0
  ```

- **Docker bind mounts resolve at container start.** If containers came up
  before you mounted a recovered volume, they are bound to whatever empty
  directory existed underneath and will keep serving nothing until restarted.
  `docker restart $(docker ps -q)` is enough; the containers do not need
  recreating.

---

## 9. Per-VM settings you must get right at boot

Wrong firmware = "no boot device" on a perfectly repaired disk. **It differs per
VM** — check whether fstab has a `/boot/efi` line:

| Has `/boot/efi` in fstab | Firmware |
|---|---|
| yes (EFI System Partition present) | **EFI** |
| no (BIOS boot partition present) | **BIOS** |

**Netplan / NetworkManager MAC pinning** — if `match: macaddress:` is present,
set the new VM's MAC to match or delete the `match:` block, otherwise the VM
boots with no network.

### Confirming it actually worked

Recovery fails quietly more often than it fails loudly. Two real cases: a
partition table that `sgdisk` refused to write, so the volume never mounted and
the empty directories the container runtime had created underneath looked
exactly like recovered data; and volumes that mounted correctly while the
application kept serving nothing, because its bind mounts had resolved before
the mount existed. Both looked like success.

Check the thing itself, not the absence of an error message.

**The write reached the disk, not a redo log.** In non-persistent mode writes
succeed and are discarded, so this is the only way to tell:

```sh
ls -la /vmfs/volumes/<ds>/<vm>/<vm>-flat.vmdk.babyk   # mtime must have moved
ls /vmfs/volumes/<ds>/<vm>/ | grep -i redo             # must be empty
```

**The partition table is real.**

```sh
sgdisk -v /dev/sdX                       # "No problems found"
dd if=/dev/sdX bs=512 skip=1 count=1 | head -c 8   # "EFI PART"
```

**The filesystem is mounted, and it is the right one.** A mountpoint that exists
is not a mountpoint that is mounted:

```sh
findmnt /data                            # nothing = it is NOT mounted
df -h /data                              # usage should match what e2fsck reported
```

If `df` shows a few kilobytes where you expect hundreds of gigabytes, you are
looking at a directory on the root filesystem, not at the recovered volume.

**The guest came up.** `vim-cmd vmsvc/get.guest <vmid>` should report
`toolsStatus = "toolsOk"` and the address you expect. A guest with no
`open-vm-tools` will never report either, so fall back to reaching it over the
network rather than assuming failure.

**The workload can see the data.** This is the one people skip. Ask the
application, not the filesystem:

```sh
docker inspect $(docker ps -q) | grep -oE '"Source": "/[^"]*"' | sort -u
docker exec <container> ls <mount-point-inside-container>
```

Bind mounts resolve at container start. If the containers were running before
you mounted the volume, restart them — `docker restart $(docker ps -q)` — and
check again.

---

## 10. Sorting the fleet before you start

Of the 34 damaged disks on this host, 5 needed nothing at all and 16 were the
five-minute case. Sorting first means you get most of the estate back before you
touch anything hard.

| Category | How you spot it | Work | Count here |
|---|---|---|---|
| Never encrypted | `size % 512 == 0` | descriptor only | 5 |
| LVM root past the damage | mapdisk shows p3 INTACT with an LVM2 PV label | bootloader, ~5 min | 16 |
| `/boot` damaged, root intact | p2 head damaged, p3 intact | recreate `/boot`, re-download the kernel | 1 |
| Root filesystem inside the damage | plain ext4 starting at 1 MiB | `e2fsck` + reassembly, ~2 hrs | 1 |
| NTFS | GPT type "Basic data" | Windows tooling, `$MFT` normally intact | 2 |
| MBR, no backup GPT | `gdisk` finds no backup header | `testdisk` deeper search | 4 |
| Guest-level encryption | high entropy *past* 512 MiB | you need that passphrase; ransomware is not the problem | 1 |

The last row is worth checking for early. One guest read as random bytes well
past the damage line, which had nothing to do with the ransomware: it was
LUKS-style full-disk encryption inside the guest, and no amount of recovery work
substitutes for the passphrase.

### Working the list

Keep one table for the whole engagement: disk, `size % 512`, category from
above, what has been done. A recovery of any size spans days and people, and the
expensive mistake is re-deriving the damage model for a disk somebody already
mapped.

A few cases worth flagging when you meet them:

- **An unrecognised partition type with a damaged head** is not necessarily
  lost — run `find_fs.py` before concluding anything. It scans for ext4/XFS/
  btrfs/LUKS/LVM signatures and back-calculates the true filesystem start.
- **Appliance VMs** (routers, firewalls) are frequently MBR with no backup GPT,
  and they usually have a **tiny system disk** — often well under 512 MiB, which
  puts the whole thing inside the damage. One router's 60 MiB system disk was
  encrypted end to end and is unrecoverable at any price, while its 10 GiB
  second disk turned out to be a whole-disk ext4 filesystem with everything
  intact past the damage line.

  So answer the two questions separately. *The appliance* is usually gone, and
  rebuilding it from a config export takes minutes against hours of carving.
  *The data on its second disk* may be perfectly recoverable, and it is worth
  checking before anyone writes the VM off wholesale. Look for filesystem
  signatures past 512 MiB with `find_fs.py` rather than assuming a proprietary
  appliance means a proprietary filesystem — the OS disk and the data disk are
  often not the same kind of thing at all.
- **A disk that was expanded in VMware** has its backup GPT at the *original*
  end, not the device end — see §4.4 for the loop-device trick.
- **Template VMs** are worth recovering early: they are small, and a working
  template speeds up every clean rebuild that follows.
- **A guest that was suspended** when the encryptor ran has a `.vmss` pointing
  at a `.vmem` that is now encrypted, so resume can never succeed and ESXi asks
  whether to preserve or discard the state. Discard: the memory image is
  ciphertext, and preserving it only leaves the VM unbootable.
  `tools/fix-suspended-vms.sh` finds them and cold-boots them from disk.
- **A VM with disks on several datastores** has one folder *per datastore, all
  with the same name*. Any tool that resolves a guest by folder name will pick
  whichever datastore it walked first, which is not necessarily the one holding
  the system disk. Address those by full path.
- **A folder with no `.vmx`** can still have its disk repaired, but there is
  nothing to boot until you build a VM shell around it. Usually templates and
  scratch VMs; decide early whether they are worth the effort.

Once the fleet is sorted, [`batch-recovery.md`](batch-recovery.md) covers doing
the class-B guests together rather than one at a time.

---

## 11. The host is not clean yet

Before restoring anything onto it:

- [ ] Preserve the encryptor and its orchestration script for law enforcement, then remove them. They are left behind executable and world-writable.
- [ ] `esxcli system settings advanced set -o /User/execInstalledOnly -i 1`
- [ ] Rotate root password; audit `/etc/ssh/keys-root/authorized_keys`
- [ ] **Patch ESXi.** An unpatched hypervisor is the usual precondition — see `analysis.md` §6 for current fixed builds
- [ ] Get SSH/443 off the public internet; enable lockdown mode
- [ ] Remote syslog (`auth.log` and `shell.log` were wiped locally)

A restored VM powered on while that encryptor is present and reachable can be
re-encrypted — and this time the new writes land in the first 512 MiB, where
nothing survives.

**Credentials to treat as compromised:** every `/etc/shadow`, `/root/.ssh`,
`/home/*/.ssh`, `/root/.git-credentials`, `/root/.env`, MongoDB and Redis
config. They survived the ransomware, which means the attacker could read them
during hours of root access.

---

## 12. Files

```
docs/
  analysis.md                   damage model, crypto assessment, hardening
  recovery-runbook.md           this file — the operational procedure
  batch-recovery.md             doing a whole fleet instead of one guest
  environment-gotchas.md        ESXi shell limits, SSH, workstation traps
  rescue-vm-guide.md            ISO choice, click-by-click UI, console commands
  iocs.md                       indicators on their own, for a detection stack
  case-media-server.md          a hard-path recovery, start to finish
tools/
  esxi-recon.sh                 host triage: IOCs, datastores, pass counts
  make-descriptors.sh           descriptor generator, pure shell (nothing to upload)
  make_descriptors.py           same, Python
  babuk_triage.py               classify files, measure surviving plaintext
  babuk_mapdisk.py              map one disk via backup GPT / backup VBR
  babuk_fleetscan.py            fleet-wide survivability scan
  find_fs.py                    locate ext4/XFS/btrfs/LUKS/LVM when the head is gone
  find-backup-gpt.sh            find the backup GPT on a disk expanded in VMware
  remaining-report.sh           what is left, power-state aware
  recover-easy-path.sh          the easy path, automated end to end
  repair-ubuntu-efi.sh          the same by hand, as a worked example
  rebuild-bootable.sh           hard path: reassemble lost+found onto a new disk
  make-rescue-vm.sh             provision one rescue VM around one disk
  make-batch-rescue-vm.sh       provision one rescue VM around many disks
  batch-repair.sh               drive the repair across every attached disk
  bringup-recovered-vm.sh       repoint the vmx, register, boot one guest
  bringup-sequential.sh         the same for a list, one at a time
  fix-suspended-vms.sh          guests suspended with an encrypted .vmem
  final-status.sh               per-VM power/tools/IP table
  esxi_run.py                   run a command or script on a host (stdlib only)
  batch_driver.py               push manifest + scripts, run the batch, poll it
  windows/                      PowerShell helpers for password-only access
examples/
  fleet-inventory.sample.csv    the shape of a fleetscan CSV
  vmlist.sample.txt             VM list for the batch tools
  manifest.sample.txt           SCSI-unit-to-guest map, and why it exists
```

---

## 13. Handing over

When you hand this to a colleague — or to an AI assistant in a fresh session —
the briefing that actually works is short:

> Continuing a Babuk-family ESXi ransomware recovery. Read
> `docs/recovery-runbook.md` first: damage model, per-VM procedures, gotchas.
> Files cannot be decrypted; recovery works because only the first 512 MiB of
> each disk was destroyed. Rescue VMs are SystemRescue with damaged disks
> attached **Independent — non-persistent**. Progress so far: `<table>`.
> Next: `<disk>`.
