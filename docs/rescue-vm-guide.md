# Rescue VM Guide — ESXi Web UI, click by click

Goal: boot a rescue Linux VM on the ESXi host, attach a damaged disk safely,
and confirm what is recoverable.

---

## 0. Generate the `-recovered.vmdk` descriptors first

You cannot attach a `.babyk` file to a VM directly — ESXi needs a *descriptor*,
and the original one was itself encrypted. The descriptor is a ~360-byte text
file, so regenerating it is trivial.

Do this once per host, before you build any rescue VM. Pick whichever of the
three methods below fits how you are working.

### Why it works

A VMDK descriptor declares its extent length in **sectors**. Declaring
`original_size / 512` does two jobs at once:

- it hides the 32/64 appended key bytes, and
- it restores 512-byte alignment, which those appended bytes broke and which
  otherwise confuses every recovery tool.

ESXi then treats the `.babyk` file as an ordinary flat extent. Nothing is
renamed, copied, or modified.

### Method A — shell script (nothing to upload)

`tools/make-descriptors.sh` is pure POSIX shell. Paste it into an SSH session or
copy it over, then:

```sh
sh make-descriptors.sh                                # dry run, all datastores
sh make-descriptors.sh --write                        # create them
sh make-descriptors.sh --write /vmfs/volumes/<uuid>   # limit to one volume
```

It auto-discovers every mounted datastore, skips files that already have a
descriptor, and never overwrites anything.

### Method B — Python tool

```sh
scp tools/make_descriptors.py root@HOST:/tmp/ir_mkdesc.py
python3 /tmp/ir_mkdesc.py /vmfs/volumes/<uuid> --write
```

Verified to produce byte-identical output to Method A.

### Method C — by hand, one disk

Four numbers, then a text file.

```sh
ls -l VM-flat.vmdk.babyk
# -rw------- 1 root root 107374182464 ... VM-flat.vmdk.babyk
```

1. **Size on disk** = `107374182464`
2. **`size % 512`** = `64` → two encryption passes → subtract **64** bytes
   (`0` → subtract 0, `32` → subtract 32, `64` → subtract 64)
3. **Original size** = `107374182464 - 64` = `107374182400`
4. **Sectors** = `107374182400 / 512` = **`209715200`**
5. **Cylinders** = `209715200 / 16065` = **`13054`**  (16065 = 255 heads × 63 sectors)

Create `VM-recovered.vmdk` in the same folder:

```
# Disk DescriptorFile
version=1
encoding="UTF-8"
CID=fffffffe
parentCID=ffffffff
isNativeSnapshot="no"
createType="vmfs"

# Extent description
RW 209715200 VMFS "VM-flat.vmdk.babyk"

# The Disk Data Base
#DDB
ddb.adapterType = "lsilogic"
ddb.geometry.cylinders = "13054"
ddb.geometry.heads = "255"
ddb.geometry.sectors = "63"
ddb.virtualHWVersion = "21"
```

Only two values change per disk: the **sector count** on the `RW` line and the
**extent filename** in quotes. Cylinders is cosmetic — ESXi recomputes geometry
and will not reject a rounded value.

> Write it with `vi` on the host, or upload it through the datastore browser.
> If you edit it on Windows, save it with **LF** line endings, not CRLF.

### Sanity check

```sh
vmkfstools -e VM-recovered.vmdk    # "Disk chain is consistent" = good
```

---

## 1. Which ISO

**Use SystemRescue** — <https://www.system-rescue.org/Download/>
(~900 MB, file is `systemrescue-<version>-amd64.iso`)

It is the right choice because it ships everything needed **offline**:
`gdisk`/`sgdisk`, `lvm2`, `e2fsprogs`, `testdisk`, `photorec`, `ddrescue`,
`ntfs-3g`, `rsync`, `openssh`. It boots straight to a root shell.

| ISO | Verdict |
|---|---|
| **SystemRescue** | **Best.** All tools built in, no network needed, boots to root shell. |
| Ubuntu 24.04 Desktop Live | Workable, but you must `apt install gdisk lvm2` which needs internet. |
| Ubuntu Server Live | Avoid — the installer environment fights you. |
| GParted Live / Clonezilla | Too minimal; no LVM tooling worth using. |

> Your guests are Ubuntu with **LVM root volumes**. Without `lvm2` the root
> partition will look like unformatted space and you will wrongly conclude the
> data is gone. This is the single most common false negative in this kind of
> recovery.

---

## 2. Upload the ISO

ESXi web UI → **Storage** → **Datastores** → click `datastore1` →
**Datastore browser**

1. **Create directory** → name it `ISO`
2. Select the `ISO` folder → **Upload** → choose the SystemRescue ISO
3. Wait for it to finish (don't close the browser tab)

---

## 3. Create the rescue VM

**Virtual Machines** → **Create / Register VM**

| Wizard step | Setting |
|---|---|
| 1. Creation type | **Create a new virtual machine** |
| 2. Name and OS | Name `RESCUE` · Guest OS family **Linux** · Version **Other 6.x or later Linux (64-bit)** |
| 3. Storage | `datastore1` (it has the free space) |
| 4. Customize settings | see below |

On the **Customize settings** page:

- **CPU** → `2`
- **Memory** → `4096 MB`
- **Hard disk 1** → click the **X** to remove it. The rescue OS runs entirely
  from RAM; it needs no disk of its own.
- **CD/DVD Drive 1** → change dropdown to **Datastore ISO file** → browse to
  `datastore1/ISO/systemrescue-*.iso` → **tick "Connect at power on"**
- **Network Adapter 1** → **untick "Connect at power on"**, or move it to an
  isolated port group.

> Leave the rescue VM off the network. The host was compromised; there is no
> reason for this VM to reach anything, and you will be mounting filesystems
> from a machine that was owned.

Click **Next** → **Finish**.

---

## 4. Attach the damaged disk — the safety-critical step

Select `RESCUE` → **Actions** → **Edit settings** → **Add hard disk** →
**Existing hard disk**

1. Browse to the VM's folder on its datastore.
2. Select **`<vm>-recovered.vmdk`** — the descriptor you generated in step 0.

   > **Do not** pick `<vm>.vmdk.babyk` — that is the encrypted *original*
   > descriptor and it will not work. It is a small text file, so it was
   > entirely inside the damage.

3. Click the **▸ arrow** next to the newly added "Hard disk 2" to expand it.
4. Set **Disk Mode** → **Independent – non-persistent**

   This is the important one. In non-persistent mode every write the guest makes
   goes into a redo log that is **thrown away at power off**. The original flat
   file physically cannot be modified. It makes everything below safe to repeat,
   experiment with, and get wrong.

5. **Save**

Repeat for additional disks if a VM had more than one.

---

## 5. Boot it

Select `RESCUE` → **Power on** → **Console** → **Open browser console**

SystemRescue boots to a root prompt in about 30 seconds. Press Enter at the boot
menu to take the default. No login is required.

---

## 6. Recover — the actual commands

### 6.1 Find the disk

```sh
lsblk
```

The rescue ISO runs from RAM, so your attached disk is normally `/dev/sda`.
Confirm by size — it should match the original VM's disk.

### 6.2 Rebuild the partition table from the intact backup GPT

The primary GPT at the front of the disk was destroyed. The backup at the end
of the disk survived. `gdisk` rebuilds one from the other:

```sh
gdisk /dev/sda
```

At the prompts type, one per line:

```
r        <- recovery & transformation menu
b        <- rebuild main GPT header from backup
w        <- write table to disk
Y        <- confirm
```

Then:

```sh
partprobe /dev/sda
lsblk
```

You should now see `sda1` (EFI), `sda2` (`/boot`), `sda3` (LVM).

### 6.3 Activate LVM

```sh
vgscan --mknodes
vgchange -ay
lvs
```

`lvs` should list something like `ubuntu-lv` in volume group `ubuntu-vg`.

### 6.4 Mount read-only and look

```sh
mkdir -p /mnt/root
mount -o ro /dev/mapper/ubuntu--vg-ubuntu--lv /mnt/root
ls -la /mnt/root
du -sh /mnt/root/* 2>/dev/null
```

**If you see `etc  home  root  usr  var` — your data is recovered.** Everything
under `/mnt/root` is readable: databases, application data, configs, home
directories.

Check the things you actually care about:

```sh
ls -la /mnt/root/var/lib/mysql/     # databases
ls -la /mnt/root/home/
ls -la /mnt/root/opt/
ls -la /mnt/root/var/www/
```

---

## 7. Getting the data off

Create a destination disk on the rescue VM (you have ~31 TB free on
`datastore1`), rather than pushing data over a network you shouldn't trust.

**Edit settings** → **Add hard disk** → **New standard hard disk** → size it
larger than the data → set it to **Thin provisioned** → Save.
It appears as a new blank device (check `lsblk`, likely `/dev/sdb`).

In the console:

```sh
mkfs.ext4 /dev/sdb
mkdir -p /mnt/out
mount /dev/sdb /mnt/out
rsync -aAXH --info=progress2 /mnt/root/ /mnt/out/
sync
```

Power off the rescue VM, detach that disk, and attach it to a freshly built
clean VM. This is the recommended end state — you get the data without carrying
a potentially backdoored OS image forward.

---

## 8. Alternative: make the original VM boot again

Only the EFI partition was destroyed, so the original OS is repairable. But this
**writes to the disk**, so it will do nothing while the disk is in
non-persistent mode. Only do this on a copy, or after deliberately switching the
disk to *Independent – persistent*.

```sh
mkfs.vfat -F32 /dev/sda1

umount /mnt/root
mount /dev/mapper/ubuntu--vg-ubuntu--lv /mnt/root
mount /dev/sda2 /mnt/root/boot
mkdir -p /mnt/root/boot/efi
mount /dev/sda1 /mnt/root/boot/efi

for d in dev proc sys run; do mount --bind /$d /mnt/root/$d; done
chroot /mnt/root /bin/bash

  grub-install --target=x86_64-efi --efi-directory=/boot/efi --bootloader-id=ubuntu
  update-grub
  update-initramfs -u
  blkid /dev/sda1          # copy the new UUID
  nano /etc/fstab          # replace the old /boot/efi UUID with the new one
  exit

for d in run sys proc dev; do umount /mnt/root/$d; done
umount /mnt/root/boot/efi /mnt/root/boot /mnt/root
```

To make a writable copy first, from the **ESXi SSH shell** (not the rescue VM):

```sh
DS=/vmfs/volumes/<datastore-uuid>
mkdir -p "/vmfs/volumes/<target-datastore>/RECOVERY"
vmkfstools -i "$DS/<vm>/<vm>-recovered.vmdk" \
           "/vmfs/volumes/<target-datastore>/RECOVERY/<vm>.vmdk" -d thin
```

Thin-provisioned, so the copy only consumes the blocks that are actually
allocated.

---

## 9. When it doesn't go to plan

| Symptom | Fix |
|---|---|
| `gdisk` finds no backup GPT | The disk is MBR-partitioned — common on appliance VMs. Run `testdisk /dev/sda` → Analyse → Deeper Search. |
| `lvs` shows nothing | `vgscan -v`; if metadata is damaged, `vgcfgrestore -f /mnt/root/etc/lvm/archive/<vg>_00001.vg <vgname>` |
| `mount` says bad superblock | `e2fsck -b 32768 -B 4096 /dev/sdaN` (ext4 backup superblock) |
| Partition mounts but is empty | You mounted the wrong LV. Check `lvs` output again. |
| Data looks like random bytes *past* 512 MiB | That guest had its own full-disk encryption. You need that passphrase; the ransomware is not the problem here. |
| Windows/NTFS guest | Don't use Linux tooling. The `$MFT` normally survives — attach the disk to a Windows box and run **R-Studio**, **DMDE**, or **UFS Explorer**. |

---

## 10. Quick "is anything recoverable" test

The 5-minute version, before committing to a full recovery:

```sh
lsblk                                       # find the disk
gdisk /dev/sda                              # r, b, w, Y
partprobe /dev/sda
vgchange -ay
mount -o ro /dev/mapper/ubuntu--vg-ubuntu--lv /mnt
ls /mnt
```

Files listed = recoverable. That is the whole test.
