# Indicators

From four hosts, August 2026. Take the behavioural section more seriously than
the hashes: this is somebody's private build of leaked source, so the next one
compiles to a different hash and does the same things.

That is not a hypothetical. Two builds have now been seen from this actor, and
the difference between them changes how much of your data is damaged.

## Hashes

```
SHA-256  517689b6c377e7622842686dadf8637b4d0690a548bc7afa89d121b0e1027164  encryptor build A (ELF, 70720 bytes)
SHA-256  84f0ce0c05d301e25d72a75fa282cfa2848d68921bdac613df06e19cdc182ffa  encryptor build B (ELF, 70720 bytes)
SHA-256  22dd384d6f1a21bb0b2ea7d6570774e895e4ca68d2f68a1551fc07c0ec340aca  run.sh (2610 bytes) - IDENTICAL for both
```

The orchestration script is byte-for-byte the same across both builds. The
encryptor is not, and the same size means nothing — the two differ in the one
constant that matters:

| Build | Damage per file | Ransom note, qTox IDs, onion, master key |
|---|---|---|
| A | `0x20000000` — 512 MiB | identical |
| B | `0x20800000` — **520 MiB** | identical |

Same actor, same infrastructure, same script, 8 MiB more destruction. **Measure
the boundary per incident** with `tools/measure-boundary.py` rather than
assuming either value. Assuming 512 MiB against build B reports 8 MiB of real
damage as intact plaintext, which is the direction that loses data.

None of these hashes appears in any public index at time of writing, and neither
does the master public key below. That rules out the named Babuk descendants
whose private keys have been recovered, and rules in somebody compiling the 2021
source leak themselves.

## On the host

```
/var/run/backup                 the encryptor, dropped mode 777
/var/run/run.sh                 the orchestration script
/tmp/script_output_1.txt        encryptor stdout, first pass
/tmp/script_output_2.txt        encryptor stdout, second pass
/tmp/.m.out                     output from the VM kill loop
```

`/tmp` on ESXi is a RAM disk. Those three files disappear on reboot, so copy them
somewhere real before you restart anything.

```
.babyk                          appended to every file it touches
How To Restore Your Files.txt   ransom note, dropped in every VM folder
```

## The ransom note

```
!!LOCK!!!!LOCK!!!!LOCK!!!!LOCK!!!!LOCK!!!!LOCK!!

Download qtox:https://qtox.github.io/
Our qTox ID: CF8AE1C0BEE91BB0F245E88C095889C276783CC901979D2609F8F1150E04D834CFCA49CB6F9C
Our qTox ID: 88D8A7CC5BF22B8749479326E327478C208FA6195EA7B9C1FF9BD3CC12550D7E98B44DA2A631

Download tor:https://www.torproject.org/download/
Our website:http://ds7mfxoc5dk676koh26nrxgi6i4jmvdxkee7imm7naxnanvibn75eyad.onion

your decrypt ID: <derived from the victim's own IP address>
```

The decrypt ID is built from the victim's address, so redact yours before you
share a note with anyone.

## Master public key

```
e690bf1107b80a690823c682ee0a3254d4edc02feaeb85fd30fe082497d51659
```

Curve25519, compiled into the binary. Of everything on this page, this is the
one to file with law enforcement. If this actor is ever arrested and their keys
seized, victims get matched by exactly this value, and only the reported ones
get matched at all.

## What the encryptor prints

Useful for confirming what happened, and for recognising a re-run. This is the
output from a pass over files that had already been encrypted:

```
Statistic:
------------------
Doesn't encrypted files: 0
Encrypted files: 0
Skipped files: 916
Whole files count: 916
Crypted: 0
------------------
```

Zero encrypted is not a failure. It skipped everything because the files already
carried the `.babyk` extension. Anything you restore afterwards will not be
skipped.

## How they got in

Initial access in these incidents is attributed to **CVE-2026-59309**, a
vulnerability in VMware ESXi. That attribution comes from the affected
organisation and its incident response, not from anything recoverable on the
hosts themselves — so treat it as the reported vector and check your own
evidence rather than assuming it applies to you.

What the hosts *do* independently show is an environment that would fall to any
remote ESXi vulnerability:

```
VMware ESXi 7.0.3 build-19482537        7.0 Update 3c, March 2022
```

Both hosts ran that build, roughly three and a half years without a patch at the
time of the attack. Both had the CIM services enabled (`CIMHttpServer`,
`CIMHttpsServer`, `CIMSLP`), which is the historical entry point for this whole
family of ESXi lockers and is off by default on current builds. And as noted
below, `execInstalledOnly` does not exist on this build at all, so the platform's
own guard against running an unsigned binary was never available.

Two host-level observations worth repeating because they generalise:

- **The encryptor re-ran days after the initial attack.** On both hosts,
  `/tmp/script_output_*.txt` carried timestamps from the morning responders were
  already working the incident. It reported zero files encrypted — because they
  all already carried the extension — but persistence was live the whole time.
  Check the mtimes on those files, not just their presence.
- **Account inventory differed between hosts.** One had only the stock
  `root`/`dcui`/`vpxuser`; the other had three additional accounts, one with full
  Admin. Neither state proves anything on its own. Compare hosts against each
  other and against what the owner expects, and treat an unexplained admin
  account as unexplained until somebody names it.

Patch level, SLP/CIM exposure and management-network reachability are the three
things worth fixing before anything is restored, whatever the entry vector turns
out to have been.

## Behaviour worth alerting on

- **`execInstalledOnly` set to 0.** ESXi's guard against unsigned binaries. The
  malware cannot run until this is off, and nothing in normal administration
  turns it off. Cheapest high-signal rule available:
  `esxcli system settings advanced set -o /User/execInstalledOnly -i 0`

  **On older builds the setting does not exist at all**, and then this indicator
  never fires. On ESXi 7.0.3 build-19482537 the attacker's own captured output
  reads:

  ```
  Unable to find option execInstalledOnly
   [NoMatchError]
  ```

  Their attempt to disable it failed because there was nothing to disable. Do
  not read a missing alert as a clean host — and note the corollary for
  responders, which cost time in one of these incidents: running
  `esxcli system settings kernel set -s execInstalledOnly -v TRUE` as a hardening
  step on such a build **silently does nothing**. Check
  `esxcli system settings kernel list -o execInstalledOnly` afterwards and
  confirm `Configured` actually changed.
- **`esxcli vm process kill -t force` in a loop**, over every world ID, skipping
  anything matching `vcls`, `vcenter` or `vcsa`.
- **`esxcli software vib remove -n vmware-fdm`**, uninstalling the HA agent.
- **`/etc/vmware/welcome` rewritten.** The console banner becomes the ransom
  note.
- **A burst of renames under `/vmfs/volumes/`**, every file growing by exactly
  32 bytes.
- **`auth.log` and `shell.log` empty** on a host with real uptime. Emptiness is
  the finding. Remote syslog makes it impossible to produce.

## Fingerprinting the family

- Files grow by exactly 32 bytes per pass. VMFS flat files are 512-byte aligned,
  so `size % 512` gives you the pass count directly: 0 never encrypted, 32 once,
  64 twice.
- Damage stops at exactly one offset and stops dead — `0x20000000` for build A,
  `0x20800000` for build B. Whichever it is, it is the same on every file on the
  host, to the sector. Entropy or zero-density sampling shows uniform random
  below the line and normal data above it. Measure it; do not assume it.
- Small files are wholly encrypted, large files only at the head.
- `.vmx` files are not targeted. VM definitions survive, so you can re-register
  guests with `vim-cmd solo/registervm` once the disks are back.

## Timeline shape

Relative to the drop, from file mtimes on the host:

| Offset | Event |
|---|---|
| T+0 | `run.sh` and the encryptor written to `/var/run/` |
| T+5m | encryption passes; flat VMDKs rewritten and renamed |
| T+40m | kill loops still cycling, HA agent removed |

The whole destructive phase took about forty minutes for roughly 30 VMs across
two datastores. There is no slow exfiltration window to catch here; if you are
reading logs after the fact, you are looking at something that was over quickly.
