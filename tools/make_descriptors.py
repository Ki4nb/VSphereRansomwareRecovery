#!/usr/bin/env python3
"""
Generate working VMDK descriptors for Babuk-damaged flat files.

Non-destructive: only ever CREATES new "<name>-recovered.vmdk" files. It never
renames, overwrites, or writes a single byte to the flat data.

The trick: a descriptor declares the extent length in sectors. By declaring
original_size/512 we expose the disk WITHOUT the 32/64 appended key bytes,
which simultaneously restores 512-byte alignment. ESXi then treats the .babyk
file as an ordinary flat extent.

Usage:  python3 make_descriptors.py <root> [...] [--write]
"""
import os
import sys

KEY_LEN = 32
TEMPLATE = """# Disk DescriptorFile
version=1
encoding="UTF-8"
CID=fffffffe
parentCID=ffffffff
isNativeSnapshot="no"
createType="vmfs"

# Extent description
RW {sectors} VMFS "{extent}"

# The Disk Data Base
#DDB
ddb.adapterType = "lsilogic"
ddb.geometry.cylinders = "{cyl}"
ddb.geometry.heads = "255"
ddb.geometry.sectors = "63"
ddb.virtualHWVersion = "21"
"""


def original_size(size):
    rem = size % 512
    passes = {0: 0, KEY_LEN: 1, 2 * KEY_LEN: 2}.get(rem)
    if passes is None:
        return None, None
    return size - passes * KEY_LEN, passes


def main():
    roots, write = [], False
    for a in sys.argv[1:]:
        if a == "--write":
            write = True
        else:
            roots.append(a)

    made = skipped = 0
    for root in roots:
        for dirpath, _, names in os.walk(root):
            for name in names:
                if not (name.endswith("-flat.vmdk.babyk") or name.endswith("_1-flat.vmdk.babyk")):
                    continue
                flat = os.path.join(dirpath, name)
                try:
                    size = os.path.getsize(flat)
                except OSError:
                    continue
                if size <= 1 << 20:
                    continue

                orig, passes = original_size(size)
                if orig is None:
                    print(f"  SKIP (odd padding {size % 512}): {name}")
                    skipped += 1
                    continue

                sectors = orig // 512
                cyl = max(1, sectors // (255 * 63))
                base = name[: -len("-flat.vmdk.babyk")]
                out = os.path.join(dirpath, base + "-recovered.vmdk")

                if os.path.exists(out):
                    print(f"  EXISTS, not touching: {os.path.basename(out)}")
                    skipped += 1
                    continue

                text = TEMPLATE.format(sectors=sectors, extent=name, cyl=cyl)
                print(f"  {'WRITE' if write else 'DRY  '} {base}: "
                      f"{sectors} sectors ({orig / (1 << 30):.2f} GiB), {passes} pass(es)")
                if write:
                    with open(out, "w") as fh:
                        fh.write(text)
                made += 1

    print(f"\n{'created' if write else 'would create'}: {made}   skipped: {skipped}")
    if not write:
        print("re-run with --write to create the descriptors")


if __name__ == "__main__":
    main()
