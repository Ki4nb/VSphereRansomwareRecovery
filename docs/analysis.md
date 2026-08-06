# What the malware does, and why the data survives

Analysis of a Babuk/Babyk-derived ESXi encryptor, taken from a host it ran on in
August 2026. Hashes and contacts are in [iocs.md](iocs.md).

## The shape of the attack

Two files land in `/var/run/`: an ELF binary for ESXi's VMkernel, and a shell
script that drives it. The script does the hypervisor work, the binary does the
encryption. Both were still sitting on the host, mode 777, when the analysis
started.

The script, in order:

1. Sleeps 300 seconds, then turns off `execInstalledOnly`. That is the ESXi
   setting that refuses to execute unsigned binaries, and the malware cannot run
   while it is on.
2. `chmod 777` on the encryptor.
3. Force-kills every running VM. `esxcli vm process kill -t force` over every
   world ID, then `-t hard`, in a loop. It skips anything whose name contains
   `vcls`, `vcenter` or `vcsa`, so vCenter itself keeps running.
4. Runs the encryptor against `/vmfs/volumes/`. Three times, with more kill
   loops and a `sleep 1800` in between.
5. Overwrites `/etc/vmware/welcome`, which is the banner on the DCUI console, so
   anyone who walks up to the physical machine sees the ransom note.
6. `esxcli software vib remove -n vmware-fdm`, removing the vSphere HA agent.

Killing the VMs first is a technical requirement rather than spite. A running VM
holds its flat VMDK open and ESXi will not let the file be rewritten underneath
it. Running the encryptor three times is a crude way of catching disks that were
still locked on the previous pass.

That retry loop is also the first piece of good news. The rename to `.babyk`
happens whether or not the encryption succeeds, so disks that stayed locked
through all three passes end up renamed and completely intact. On this host that
was five disks, about 325 GiB, which needed nothing but a new descriptor file.

The triple pass has a second consequence. Some files were caught twice, which
means two layers of encryption on the same 512 MiB and two appended keys.

## The 512 MiB limit

The encryptor processes `0x20000000` bytes, appends a 32-byte ephemeral public
key, and moves on. Files smaller than that are entirely encrypted. On this host
128 files were hit: 94 of them were under 512 MiB and are unrecoverable, and all
94 were VMDK descriptors, logs and `.nvram` files, every one of which can be
regenerated. The 34 files over 512 MiB were the actual virtual disks, and they
lost 512 MiB each out of 6,911.95 GiB total.

That works out to 17.02 GiB destroyed and 6,894.93 GiB intact. 99.754%.

You can see the boundary directly. Sample zero-byte density every 64 KiB;
ciphertext is uniform random and sits around 250 zeros per 64 KiB, real data is
far higher, and sparse regions collapse to near nothing under run-length folding:

```
offset      0 MiB : 252     ciphertext
offset    128 MiB : 252     ciphertext
offset    256 MiB : 276     ciphertext
offset    511 MiB : 261     ciphertext
offset    512 MiB : 246     boundary
offset    600 MiB : 16      sparse, untouched
offset   1024 MiB : 16      sparse, untouched
offset  16384 MiB : 27702   plaintext
offset  98304 MiB : 23895   plaintext
```

`tools/babuk_triage.py` does this across a whole datastore and writes a CSV.

### Counting the passes from the file size

VMFS flat files are always 512-byte aligned, so the appended keys are visible in
the size:

| `size % 512` | Meaning |
|---|---|
| `0` | never encrypted, only renamed |
| `32` | encrypted once |
| `64` | encrypted twice |

On this host: 26 files encrypted once, 4 encrypted twice, 5 renamed but never
touched.

A double-encrypted file over 512 MiB looks like this:

```
[ encrypted twice: first 512 MiB ][ untouched plaintext ][ K1 ][ K2 ]
```

`K2` is the outer layer and would have to come off first. `K1` sits past the
damage line, stored in the clear. If a working decryptor ever appears it has to
run twice on these files, and most decryptors assume a single pass. They will
report success and hand you garbage.

## Why decryption is off the table

| Piece | Implementation | Any way in? |
|---|---|---|
| Key agreement | Curve25519 ECDH, attacker's public key compiled into the binary | No |
| Per-file ephemeral private key | read from `/dev/urandom`, then clamped | No |
| Key derivation | SHA-256 over the 32-byte shared secret | No |
| Bulk cipher | Sosemanuk | No |

For each file the encryptor generates a fresh Curve25519 keypair, multiplies its
ephemeral private key by the attacker's embedded public key to get a shared
secret, hashes that with SHA-256 to key Sosemanuk, encrypts, and appends the
ephemeral *public* key to the file. Then it throws the ephemeral private key
away. Deriving the same shared secret again requires the attacker's master
private key.

Kudelski Security's writeup of the leaked Babuk source quotes the random
generator directly, and there is nothing wrong with it:

```c
void csprng(uint8_t* buffer, int count) {
    if (FILE *fp = fopen("/dev/urandom", "r")) { fread(buffer, 1, count, fp); fclose(fp); }
}
```

Getting the private key from the public key means solving the discrete logarithm
problem on Curve25519, roughly 2^126 operations. That is not an expensive
computation, it is an impossible one.

Four avenues were checked and closed:

- **Weak randomness.** None. `/dev/urandom` is a proper CSPRNG and the output is
  clamped correctly.
- **Implementation flaw.** Nothing published against Babuk's ECDH and Sosemanuk
  chain, and the binary matches the leaked source closely.
- **Known private keys.** Fifteen exist publicly: fourteen from the September
  2021 source leak, plus the Tortilla key that Cisco Talos recovered after an
  arrest and gave to Avast in January 2024. They all ship in one free tool.
- **Public threat intel.** Neither the binary's SHA-256 nor the embedded master
  public key turns up anywhere. This is somebody's own build from the leaked
  source rather than one of the named descendants (Tortilla, Rook, Night Sky,
  Pandora, Cheerscrypt, RA Group, ESXiArgs, Rorschach, RTM Locker, PrideLocker),
  whose keys are the ones that have been recovered.

Run the [No More Ransom](https://www.nomoreransom.org/) Babuk decryptor against a
copy of one file anyway. It tests all fifteen keys in about ten minutes, and the
cost of being wrong about this is enormous compared to the cost of checking.

Then report it, with the master public key included. Every key in that decryptor
exists because someone reported the incident that produced it.

## Why the damage is survivable

Filesystems keep spare copies of exactly the structures that live in the first
512 MiB, and they put the spares at the end of the volume. This encryptor only
ever touches the front.

| Structure | Primary, destroyed | Spare, survived |
|---|---|---|
| GPT partition table | LBA 1 | last sector of the disk |
| NTFS boot sector | first sector of the partition | last sector of the partition |
| NTFS `$MFT` | usually around 3 GiB in | past the damage line already |
| ext2/3/4 superblock | offset 1024 | block groups 1, 3, 5, 7, 9, 25… |
| LVM2 PV label | PV start + 512 B | no spare, but PVs normally start at 2–3 GiB |

### The exception that decides everything

ext4 inode tables have no backup copies. `flex_bg` packs the tables for 16 block
groups together at the start of the filesystem, and if that start is inside the
first 512 MiB, the metadata is gone permanently. File contents are still on the
disk, and `e2fsck` can rebuild a tree out of them, but the names and the
directory structure come back as numbered directories in `lost+found`.

Which is why one number predicts the entire difficulty of a guest:

| Root filesystem starts | What you are dealing with | Time |
|---|---|---|
| past 512 MiB (LVM root at 2–3 GiB) | filesystem completely clean, only the bootloader is gone | minutes |
| at 1 MiB (plain ext4) | root inode and ~131k inodes destroyed | hours |

Ubuntu's guided install with LVM lands in the first row. A plain-ext4 Debian
install lands in the second. The malware does not distinguish between them; the
installer's default partition layout decides your afternoon.

## Anti-forensics

`/var/log/auth.log` and `/var/log/shell.log` were both empty on a host with 405
days of uptime. On a machine that has been running that long, an empty auth log
is not an absence of evidence, it is the evidence.

Also modified inside the incident window: `/etc/passwd`, `/etc/shadow`,
`/etc/group`, `/etc/security/access.conf`, `/etc/security/opasswd`. The DCUI
banner was replaced. The HA agent was uninstalled.

What was *not* found matters too. `/var/spool/cron/crontabs/root` and
`/var/run/inetd.conf` held nothing but stock ESXi entries, so there was no cron
or inetd persistence. The access route was SSH and the cleanup was log wiping.
That shapes the response: rotate credentials and patch, rather than hunt for a
rootkit.

## How it got in

The host was running ESXi 8.0.2 build 22380479. That build dates from September
2023, so the machine had gone roughly two years without patching, and it had 405
days of uptime when this happened.

That is the whole story, most likely. There is no evidence of an exploit chain,
no dropper, no persistence mechanism, nothing more sophisticated than being on
the network as root. ESXi hosts drift into this state easily: rebooting one means
scheduling downtime for every VM it carries, so the patch gets deferred, and then
deferred again.

A host that far behind is exposed to everything published since. The current
advisory as of writing is
[VMSA-2026-0006.1](https://support.broadcom.com/web/ecx/support-content-notification/-/external/content/SecurityAdvisories/0/38017),
whose worst entries are CVE-2026-59309, an authentication bypass in the VMware
Directory Service at CVSS 9.8, and CVE-2026-47876, an out-of-bounds write in
VMXNET3 at 9.3. Fixed builds:

| Branch | Fixed build |
|---|---|
| ESXi 9.1.x | `ESXi-9.1.0.0200-25557999` |
| ESXi 9.0.x | `ESXi-9.0.2.0100-25595025` |
| ESXi 8.0 | `ESXi80U3k-25595708` or `ESXi80U2f-25626445` |

To be precise about what is being claimed: no specific CVE was tied to this
intrusion. The finding is the boring one. An internet-reachable hypervisor that
has not been patched in two years is the precondition, and closing that is what
removes the risk.

## What would actually have helped

Roughly in order of how much difference each one makes.

**Real backups, offsite, immutable or air-gapped.** Nothing else on this list
comes close. Snapshots on the same host are not backups; the encryptor walks
`/vmfs/volumes/` and reaches them too. This incident had none, which is why it
turned into a forensics exercise instead of a restore.

**Patch the hypervisor**, on a cadence that survives the inconvenience of
rebooting it.

**Keep SSH and the management interface off the public internet.** Management
VLAN or VPN only, and turn on lockdown mode.

**Ship logs off the host.** Local logs get wiped. Anything already sent to a
syslog server does not.

**Leave `execInstalledOnly` on**, and alert when it changes. This malware had to
disable it before it could run at all, and nothing in normal operation touches
that setting.

**Alert on mass `esxcli vm process kill` and on `vmware-fdm` removal.** Both are
loud and both are close to zero in day-to-day use.

**Rebuild guests from clean images** where you can, rather than carrying an OS
forward that had an attacker sitting on it for hours.

## Sources

- [Kudelski Security — Dissecting and Detecting Babuk Ransomware Cryptography](https://kudelskisecurity.com/research/dissecting-and-detecting-babuk-ransomware-cryptography/)
- [Cisco Talos — New decryptor for Babuk Tortilla ransomware variant released](https://blog.talosintelligence.com/decryptor-babuk-tortilla/)
- [BleepingComputer — Babuk ransomware's full source code leaked](https://www.bleepingcomputer.com/news/security/babuk-ransomwares-full-source-code-leaked-on-hacker-forum/)
- [SentinelOne — Threat actor groups hop on leaked Babuk code to build ESXi lockers](https://www.sentinelone.com/labs/hypervisor-ransomware-multiple-threat-actor-groups-hop-on-leaked-babuk-code-to-build-esxi-lockers/)
- [Synacktiv — PrideLocker, a fork of the Babuk ESX encryptor](https://www.synacktiv.com/en/publications/pridelocker-a-new-fork-of-babuk-esx-encryptor.html)
- [Sophos — Extracting data from encrypted virtual disks: seven methods](https://www.sophos.com/en-us/blog/extract-data-from-encrypted-vms/)
- [DCSO CyTec — Unransomware: From Zero to Full Recovery in a Blink](https://medium.com/@DCSO_CyTec/unransomware-from-zero-to-full-recovery-in-a-blink-8a47dd031df3)
- [No More Ransom](https://www.nomoreransom.org/)
