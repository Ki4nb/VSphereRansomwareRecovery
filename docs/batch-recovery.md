# Recovering a fleet, not a VM

[`recovery-runbook.md`](recovery-runbook.md) is the procedure for one guest.
This is what changes when there are thirty of them, which is the normal case:
these lockers are run against every reachable host, so you are rarely dealing
with a single machine.

The technique does not change. The economics do.

---

## 1. What actually costs time

Not the repair. A clean easy-path guest takes a couple of minutes, most of it
`update-initramfs` regenerating an initrd inside a chroot.

The cost is the **rescue VM**. SystemRescue runs entirely from RAM, so every
boot loses the root password, the firewall state and the IP address, and every
boot therefore needs somebody at the ESXi console typing:

```sh
systemctl disable --now iptables && systemctl start sshd
passwd
ip -br link
ip addr add <free-ip>/<prefix> dev <iface> && ip route add default via <gw>
```

One rescue VM per damaged disk means one console trip per damaged disk. Thirty
disks, thirty trips, and the recovery is now measured in days of somebody's
attention rather than hours of machine time.

So: **one rescue VM, many disks.** ESXi allows about 60 per VM (four pvscsi
controllers, fifteen usable units each — unit 7 is reserved for the HBA). One
console trip, then everything else is scripted.

> `systemctl disable --now iptables`, not `stop`. A plain `stop` does not stick,
> and the symptom is that the rescue VM answers ping while every SSH connection
> times out.

---

## 2. The order

```sh
# 1. Host triage. IOCs, datastores, pass counts, existing descriptors.
sh tools/esxi-recon.sh

# 2. Descriptors for every damaged flat file. Creates files, never modifies.
sh tools/make-descriptors.sh --write

# 3. Classify. Reads power state before touching a disk.
sh tools/remaining-report.sh
```

`remaining-report.sh` sorts the fleet into the four categories that matter:

| Class | Test | Work |
|---|---|---|
| **A** never encrypted | `size % 512 == 0` | descriptor only, no rescue VM at all |
| **B** easy path | backup GPT readable, root FS past 512 MiB | `recover-easy-path.sh`, minutes |
| **C** MBR, no backup GPT | nothing at the device end | `testdisk`, or rebuild from config |
| **skip** | `vswp`, `vmem`, vCLS agents | scratch files, not data |

Do the whole of A first. It needs no rescue VM, no repair, and no risk — the
disks were renamed and never written to, so their partition tables are intact.
On one host that was 33 guests. Getting most of the estate back before you touch
anything hard also buys goodwill you will want later.

Then batch the Bs.

```sh
# one rescue VM around every class-B disk
printf 'ubuntu-01\nubuntu-02\napp-server-01\n' > /tmp/vmlist.txt
sh tools/make-batch-rescue-vm.sh "VM Network" --list /tmp/vmlist.txt --commit

# ... one console trip ...

# dry run everything, then commit
python3 tools/batch_driver.py --esxi <host> --rescue <ip> --key ~/.ssh/ir_key
python3 tools/batch_driver.py --esxi <host> --rescue <ip> --key ~/.ssh/ir_key --commit
```

Then bring them up, one at a time:

```sh
sh tools/bringup-sequential.sh "VM Network" --list /tmp/vmlist.txt --commit
```

---

## 3. Telling thirty identical disks apart

This is the part that is not obvious.

These fleets are almost always clones of one template. That means the guests
share filesystem UUIDs, LVM volume group names, LVM volume group **UUIDs**, and
frequently the same hostname. Most of them are also the same size — of one
36-disk batch, twenty-five were 40 GiB. So none of the usual identifiers work:
not the UUID, not the label, not the size.

What does work is **where the disk is plugged in**. `make-batch-rescue-vm.sh`
writes a manifest next to the rescue VM's vmx:

```
<controller> <unit> <original_bytes> <passes> <vm name>
0 0 107374182400 2 ubuntu-01
0 1 42949672960 2 ubuntu-02
```

Linux exposes the same coordinates as the `H:C:T:L` in `lsblk -S`. The catch is
that the Linux host number is **not** the vmx controller number — the AHCI
controller driving the CD-ROM claims one too, and on a real run the three pvscsi
controllers came up as hosts 32, 33 and 34. So `batch-repair.sh` matches each
Linux host to a manifest controller by comparing the **complete set of
unit:size pairs**, then re-verifies each disk's exact byte size immediately
before touching it.

It is fail-closed. If no host matches a controller signature it aborts rather
than guessing, because a wrong map means repairing the wrong disk.

One consequence worth knowing: **never remove a line from the manifest.** To
skip a guest, mark it. Deleting a row changes that controller's signature and
unmatches the entire controller — which silently skipped six disks the first
time, and the failure looked like the disks were faulty rather than the map.

---

## 4. Cloned guests break LVM

Thirty clones attached at once present thirty volume groups with the same name
*and the same UUID*. A plain `vgchange -ay` refuses:

```
Cannot activate LVs in VG ubuntu-vg while PVs appear on duplicate devices
```

and then the logical volume node is never created, so the mount fails with the
much less helpful `Can't lookup blockdev`.

The fix is to scope every LVM call to the one disk being worked on:

```sh
vgchange -ay --devices /dev/sdb3
lvs        --devices /dev/sdb3 --noheadings -o lv_path
```

`recover-easy-path.sh` does this automatically when the LVM in the rescue
environment supports `--devices` (LVM 2.03.11+, which SystemRescue has). Without
it, a batch run fails on every disk after the first and it looks like the
procedure is broken rather than the namespace being ambiguous.

---

## 5. Sequencing, and the mistake that wastes an afternoon

**Power off the rescue VM before you bring anything up.**

A powered-on VM holds a VMFS lock on every flat file attached to it. While that
lock is held, `dd` against the file on the host reads *nothing* — not an error,
just no data. Every safety check that inspects the disk therefore comes back
empty and refuses to proceed, and the message you get is about the disk being
unreadable rather than about the lock.

The same effect quietly corrupts your inventory: `babuk_fleetscan.py` cannot
open a locked file, so it omits that disk from the CSV entirely. As you recover
more VMs and power them on, the fleet scan sees fewer and fewer of them. Use
`remaining-report.sh`, which checks power state first and reports a running VM
as done rather than probing a file it cannot read.

---

## 6. Long jobs need to be detached

A batch of thirty disks runs for the better part of an hour. If that job is tied
to an SSH exec channel and the channel drops, the remote process gets SIGHUP and
dies — with no error at either end. It happened at disk 20 of 32, and the only
symptom was that the log stopped growing.

Run anything long under `setsid nohup` and poll it:

```sh
setsid nohup bash /tmp/batch-repair.sh /tmp/manifest.txt --commit \
    > /tmp/batch.out 2>&1 < /dev/null &
```

`batch_driver.py` does this, and `batch-repair.sh` skips disks whose log already
contains `=== DONE ===`, so a relaunch resumes rather than redoing an hour of
work.

While diagnosing this, do not test with `pgrep -f batch-repair.sh` over SSH: the
pattern matches the shell running your own check, so it reports the job alive
long after it died.

---

## 7. Bringing them up

One at a time. Thirty VMs starting together on a host that has just been through
an incident will contend for the same datastore, and if one is going to fail its
fsck or hang on a missing mount you want to see it before the next twenty-nine
do the same thing.

`bringup-sequential.sh` waits for each guest to report VMware Tools before
starting the next, falling back to power state after a timeout so a guest
without Tools installed does not stall the queue.

For each VM it also has to fix the vmx, because the original definitions do not
work as-is:

- **The disk pointer** still names the encrypted descriptor. Repoint it at the
  `-recovered.vmdk`. Pick the disk node by `deviceType`, never by position: a
  CD-ROM also has a `.fileName`, and taking the first match writes the disk
  descriptor onto the ISO drive, leaving the real disk pointing at a file that
  no longer exists.
- **The network.** If vCenter was itself a casualty, every guest's
  `ethernet0.dvs.*` binding refers to a distributed portgroup that no longer
  resolves, and the NIC silently fails to attach. Strip those lines and set a
  standard `networkName`.
- **The MAC.** Guests pin their interface by MAC in netplan. Set
  `ethernet0.addressType = "static"` and `ethernet0.address` to the original.
  ESXi accepts addresses outside its nominal `00:50:56:00-3F` static range and
  reports them as `addressType = "manual"`; it works.
- **Firmware.** EFI and BIOS guests sit side by side in the same fleet. The
  original vmx already has it right — preserve it rather than assuming.

---

## 8. Guests that were suspended

A guest suspended when the encryptor ran has a `.vmss` pointing at a `.vmem`
that is now encrypted. Resume fails, and ESXi puts up a question asking whether
to preserve or discard the suspended state.

Discard. The memory image is ciphertext and cannot be restored, so preserving it
just leaves the VM unbootable. Discarding gives a clean cold boot from the
repaired disk.

```sh
sh tools/fix-suspended-vms.sh            # find them
sh tools/fix-suspended-vms.sh --commit   # power off, stash the .vmss, cold boot
```

Watch for these in the bring-up summary: a suspended guest shows as powered on
in some views and never reports Tools, so it reads as a boot failure when it is
actually a VM waiting for an answer to a dialog.

---

## 9. Keep a table

One table for the whole engagement: disk, `size % 512`, class, what has been
done. A recovery of any size spans days and people, and the expensive mistake is
re-deriving the damage model for a disk somebody already mapped.

`remaining-report.sh` regenerates that table from the host itself, so it is
correct after an interruption rather than as correct as somebody's notes.
