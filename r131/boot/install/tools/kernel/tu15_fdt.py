# SPDX-License-Identifier: GPL-2.0-or-later
# Copyright (C) 2026 Olympian LED, Inc.
"""Read complete TU15 FDT properties for board-port validation (no device I/O)."""
import struct
from pathlib import Path


class Tree:
    def __init__(self, path):
        # A path, or the blob itself (tools/kernel/dt_gate.py reads the DTB out of a boot image).
        data = bytes(path) if isinstance(path, (bytes, bytearray, memoryview)) else Path(path).read_bytes()
        if len(data) < 40:
            raise ValueError("truncated FDT")
        magic, size, start, strings, _, version, _, _, ns, nt = struct.unpack_from(">10I", data)
        if magic != 0xd00dfeed or version < 17 or size > len(data):
            raise ValueError("invalid FDT header")
        if start + nt > size or strings + ns > size:
            raise ValueError("invalid FDT blocks")
        self.nodes = {}
        stack = []
        cursor, end = start, start + nt
        names = data[strings:strings + ns]
        while cursor + 4 <= end:
            token, = struct.unpack_from(">I", data, cursor)
            cursor += 4
            if token == 1:
                nul = data.find(b"\0", cursor, end)
                if nul < 0:
                    raise ValueError("unterminated node")
                stack.append(data[cursor:nul].decode("ascii"))
                node = "/" + "/".join(stack[1:])
                if node in self.nodes:
                    raise ValueError("duplicate node")
                self.nodes[node] = {}
                cursor = (nul + 4) & ~3
            elif token == 2:
                if not stack:
                    raise ValueError("unbalanced node")
                stack.pop()
            elif token == 3:
                if not stack or cursor + 8 > end:
                    raise ValueError("invalid property")
                length, name = struct.unpack_from(">II", data, cursor)
                cursor += 8
                nul = names.find(b"\0", name)
                if name >= len(names) or nul < 0 or cursor + length > end:
                    raise ValueError("invalid property bounds")
                key = names[name:nul].decode("ascii")
                props = self.nodes["/" + "/".join(stack[1:])]
                if key in props:
                    raise ValueError("duplicate property")
                props[key] = data[cursor:cursor + length]
                cursor = (cursor + length + 3) & ~3
            elif token == 4:
                continue
            elif token == 9:
                if stack:
                    raise ValueError("unclosed node")
                break
            else:
                raise ValueError("unknown FDT token")
        else:
            raise ValueError("missing FDT end")
        self.handles = {}
        for node, props in self.nodes.items():
            if "phandle" in props:
                handle = self.cells(node, "phandle")[0]
                if handle in self.handles:
                    raise ValueError("duplicate phandle")
                self.handles[handle] = node

    def cells(self, node, prop):
        value = self.nodes[node][prop]
        if len(value) % 4:
            raise ValueError("property is not cells")
        return list(struct.unpack(">" + "I" * (len(value) // 4), value))

    def strings(self, node, prop):
        return self.nodes[node][prop].rstrip(b"\0").decode("ascii").split("\0")

    def symbol(self, name):
        return self.strings("/__symbols__", name)[0]

    def reference(self, node, prop):
        return self.handles[self.cells(node, prop)[0]]

    def gpio(self, node, prop):
        handle, pin, flags = self.cells(node, prop)
        bank = next(i for i in range(5) if self.symbol("gpio" + str(i)) == self.handles[handle])
        return bank, pin, flags


class Writer:
    """A flattened device tree, written (version 17, an empty memory-reservation block): nested nodes and
    byte / string / u32 properties, which is all a FIT needs. The strings block shares suffixes as dtc's
    does; Tree above reads the result back. tools/boot/dho900_uboot_chain.py builds its FIT with it."""

    BEGIN_NODE, END_NODE, PROP, END = 1, 2, 3, 9

    def __init__(self):
        self._struct = bytearray()
        self._strings = bytearray(b"\0")
        self._depth = 0

    def _name(self, name):
        key = name.encode("ascii") + b"\0"
        at = bytes(self._strings).find(key)
        if at < 0:
            at = len(self._strings)
            self._strings += key
        return at

    def begin(self, name):
        raw = name.encode("ascii") + b"\0"
        self._struct += struct.pack(">I", self.BEGIN_NODE) + raw.ljust((len(raw) + 3) & ~3, b"\0")
        self._depth += 1
        return self

    def end(self):
        if self._depth <= 0:
            raise ValueError("FDT: end() without begin()")
        self._struct += struct.pack(">I", self.END_NODE)
        self._depth -= 1
        return self

    def prop(self, name, value):
        if self._depth <= 0:
            raise ValueError("FDT: a property outside every node")
        value = bytes(value)
        self._struct += struct.pack(">III", self.PROP, len(value), self._name(name))
        self._struct += value.ljust((len(value) + 3) & ~3, b"\0")
        return self

    def prop_str(self, name, value):
        return self.prop(name, value.encode("utf-8") + b"\0")

    def prop_u32(self, name, *values):
        return self.prop(name, struct.pack(">%dI" % len(values), *values))

    def to_bytes(self):
        if self._depth:
            raise ValueError("FDT: %d node(s) left open" % self._depth)
        body = bytes(self._struct) + struct.pack(">I", self.END)
        off_rsvmap = 40
        off_struct = off_rsvmap + 16
        off_strings = off_struct + len(body)
        total = off_strings + len(self._strings)
        header = struct.pack(">10I", 0xd00dfeed, total, off_struct, off_strings, off_rsvmap, 17, 16, 0,
                             len(self._strings), len(body))
        return header + bytes(16) + body + bytes(self._strings)
