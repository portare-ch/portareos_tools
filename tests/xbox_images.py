"""Synthetic Xbox disc images for the nova-push and portare-chd tests."""

import os
import struct

XBOX_MAGIC = b"MICROSOFT*XBOX*MEDIA"
XBOX_FILLER = b"unused sector filler " * 98  # 2058 bytes, sliced into sectors
XBOX_FILES = {
    "default.xbe": b"XBEH" + bytes(range(256)) * 9,
    "Zeta.bin": os.urandom(2048),  # exactly one sector
    "_hidden.dat": b"",  # upper-cased, "_" sorts after letters
    "alpha.txt": b"alpha",
    "Beta.TXT": b"beta" * 1000,
    "media/Intro.xmv": os.urandom(5000),
    "media/sounds/hit.wav": os.urandom(300),
    "media/sounds/empty": None,  # an empty directory
    # Enough entries for a table longer than a sector.
    **{
        f"levels/level{number:02d}_with_a_long_name.map": bytes([number]) * number
        for number in range(60)
    },
}


def xdvdfs_data(files):
    """An XDVDFS game partition holding files ({path: bytes}, None for an empty
    directory), laid out unlike an XISO: filler sectors between the files, each
    table after its files, unbalanced trees, names in mixed case."""
    tree = {}
    for path, data in files.items():
        *folders, name = path.split("/")
        node = tree
        for folder in folders:
            node = node.setdefault(folder, {})
        node[name] = {} if data is None else data
    image = bytearray(XBOX_FILLER[:2048] * 48)

    def store(data):
        image.extend(XBOX_FILLER[len(image) % 7 : 2048 + len(image) % 7])
        sector = len(image) // 2048
        image.extend(data + b"\xff" * (-len(data) % 2048))
        return sector

    def directory(node, depth):
        entries = []
        for name in sorted(node, key=str.upper, reverse=depth % 2 == 1):
            value = node[name]
            if isinstance(value, dict):
                entries.append((name.encode(), *directory(value, depth + 1), 0x10))
            else:
                entries.append((name.encode(), store(value), len(value), 0x20))
        # A right chain of ascending names, or a left chain of descending ones.
        table, offsets = bytearray(), []
        for name, *_ in entries:
            length = (14 + len(name) + 3) & ~3
            if len(table) // 2048 != (len(table) + length - 1) // 2048:
                table.extend(b"\xff" * (-len(table) % 2048))
            offsets.append(len(table))
            table.extend(b"\xff" * length)
        for number, (name, sector, size, attributes) in enumerate(entries):
            child = offsets[number + 1] // 4 if number + 1 < len(entries) else 0
            left, right = (child, 0) if depth % 2 else (0, child)
            entry = struct.pack(
                "<HHIIBB", left, right, sector, size, attributes, len(name)
            )
            start = offsets[number]
            table[start : start + 14 + len(name)] = entry + name
        table.extend(b"\xff" * (-len(table) % 2048 or (0 if table else 2048)))
        return store(bytes(table)), len(table)

    root, size = directory(tree, 0)
    descriptor = bytearray(2048)
    descriptor[:20] = descriptor[0x7EC:0x800] = XBOX_MAGIC
    struct.pack_into("<II8s", descriptor, 20, root, size, b"filetime")
    image[32 * 2048 : 33 * 2048] = descriptor
    return bytes(image + XBOX_FILLER[:2048] * 4)


def write_xiso(path, offset=0, files=None):
    """Sparse Xbox image: an XDVDFS partition at offset (a full dump unless 0)."""
    with open(path, "wb") as image:
        if offset:
            image.write(b"video partition " * 128)
        image.seek(offset)
        image.write(xdvdfs_data(XBOX_FILES if files is None else files))
    return path


def read_xdvdfs(data, test):
    """The files of an XISO (XDVDFS at offset 0), as xdvdfs_data takes them,
    checking that each table is a sorted binary tree of aligned entries that
    never cross a sector."""
    descriptor = data[32 * 2048 : 33 * 2048]
    test.assertEqual(descriptor[:20], XBOX_MAGIC)
    test.assertEqual(descriptor[0x7EC:], XBOX_MAGIC)
    files = {}

    def walk(sector, size, prefix):
        test.assertEqual(size % 2048, 0)
        table = data[sector * 2048 : sector * 2048 + size]
        if table[:2] == b"\xff\xff":
            files[prefix.rstrip("/")] = None
            return

        def visit(offset):
            left, right, start, length, attributes, name_length = struct.unpack_from(
                "<HHIIBB", table, offset
            )
            test.assertEqual(offset % 4, 0)
            test.assertEqual(offset // 2048, (offset + 13 + name_length) // 2048)
            name = table[offset + 14 : offset + 14 + name_length].decode()
            if attributes & 0x10:
                walk(start, length, f"{prefix}{name}/")
            else:
                files[prefix + name] = data[start * 2048 : start * 2048 + length]
            return (visit(left * 4) if left else []) + [name] + (
                visit(right * 4) if right else []
            )

        names = visit(0)
        test.assertEqual(names, sorted(names, key=str.upper))

    walk(*struct.unpack_from("<II", descriptor, 20), "")
    return files
