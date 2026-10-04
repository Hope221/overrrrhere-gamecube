#!/usr/bin/env python3
"""
Self-test for decrypt3ds.py, no real keys or games needed.

A random fake boot9 is used (pyctr's boot9 hash check is patched for the test). Synthetic NCCH, CCI and CIA files
are encrypted here with plain AES-CTR / AES-CBC, then decrypted by decrypt3ds, and the result is compared with the
original plaintext.

Run:  python test_decrypt3ds.py
"""

import hashlib
import os
import struct
import sys
import tempfile
import unittest
from pathlib import Path

import pyctr.crypto.engine as engine_mod

FAKE_BOOT9 = os.urandom(0x8000)
engine_mod.BOOT9_PROT_HASH = hashlib.sha256(FAKE_BOOT9).hexdigest()

from pyctr.crypto import CryptoEngine                              # noqa: E402
from pyctr.crypto.seeddb import add_seed                           # noqa: E402
from pyctr.type.cia import CIAReader                               # noqa: E402
from pyctr.type.cci import CCIReader                               # noqa: E402
from pyctr.type.ncch import NCCHReader                             # noqa: E402

sys.path.insert(0, str(Path(__file__).resolve().parent))
import decrypt3ds                                                  # noqa: E402

TMP = Path(tempfile.mkdtemp(prefix='decrypt3ds-test-'))
(TMP / 'boot9.bin').write_bytes(FAKE_BOOT9)
CryptoEngine(boot9=str(TMP / 'boot9.bin'))

EXTRA_SLOTS = {0x00: 0x2C, 0x01: 0x25, 0x0A: 0x18, 0x0B: 0x1B}


def make_ncch(program_id: int, partition_id: int, crypto_method=0x01, seed=None, exefs=True, romfs_size=0x1400,
              encrypted=True):
    """Return (encrypted_ncch, plaintext_ncch_with_nocrypto_flags)."""
    key_y = os.urandom(16)
    exheader = os.urandom(0x800)

    # ExeFS: .code (extra key), banner (main key), logo (extra key)
    files = [('.code', os.urandom(0x300)), ('banner', os.urandom(0x200)), ('logo', os.urandom(0x100))]
    exefs_hdr = bytearray(0x200)
    exefs_body = bytearray()
    for i, (name, data) in enumerate(files):
        struct.pack_into('<8sII', exefs_hdr, i * 16, name.encode(), len(exefs_body), len(data))
        exefs_hdr[0x200 - (i + 1) * 0x20:0x200 - i * 0x20] = hashlib.sha256(data).digest()
        exefs_body += data
        exefs_body += b'\0' * (-len(exefs_body) % 0x200)
    exefs_plain = bytes(exefs_hdr + exefs_body) if exefs else b''
    romfs_plain = os.urandom(romfs_size)

    exefs_off = 5
    romfs_off = exefs_off + len(exefs_plain) // 0x200
    total_units = romfs_off + romfs_size // 0x200

    hdr = bytearray(0x200)
    hdr[0:0x10] = key_y
    hdr[0x100:0x104] = b'NCCH'
    struct.pack_into('<I', hdr, 0x104, total_units)
    struct.pack_into('<Q', hdr, 0x108, partition_id)
    struct.pack_into('<H', hdr, 0x112, 2)
    struct.pack_into('<Q', hdr, 0x118, program_id)
    hdr[0x150:0x160] = b'CTR-P-TEST'.ljust(16, b'\0')
    hdr[0x160:0x180] = hashlib.sha256(exheader[:0x400]).digest()
    struct.pack_into('<I', hdr, 0x180, 0x400)
    flags = bytearray(8)
    flags[3] = crypto_method
    flags[5] = 0x3
    flags[7] = 0x20 if seed else 0
    if not encrypted:
        flags[3], flags[7] = 0, 4
    hdr[0x188:0x190] = flags
    if exefs:
        struct.pack_into('<II', hdr, 0x1A0, exefs_off, len(exefs_plain) // 0x200)
    struct.pack_into('<II', hdr, 0x1B0, romfs_off, romfs_size // 0x200)

    seeded_y = key_y
    if seed:
        hdr[0x114:0x118] = hashlib.sha256(seed + program_id.to_bytes(8, 'little')).digest()[:4]
        seeded_y = hashlib.sha256(key_y + seed).digest()[:16]
        add_seed(program_id, seed)

    def plain(h):
        p = bytearray(h)
        p[0x18B], p[0x18F] = 0, 4
        return bytes(p) + exheader + exefs_plain + romfs_plain

    if not encrypted:
        return plain(hdr), plain(hdr)

    eng = CryptoEngine()
    eng.set_keyslot('y', 0x2C, key_y)
    extra = EXTRA_SLOTS[crypto_method]
    eng.set_keyslot('y', 0x41, 0)  # unused slot, just to keep the engine happy
    eng.set_keyslot('x', 0x50, eng.key_x[extra])
    eng.set_keyslot('y', 0x50, seeded_y)

    def ctr(slot, section, data, offset=0):
        iv = (partition_id << 64 | section << 56) + (offset >> 4)
        return eng.create_ctr_cipher(slot, iv).encrypt(data)

    exefs_enc = bytearray(ctr(0x2C, 2, exefs_plain))  # header, banner: main key
    if exefs:
        for i, (name, data) in enumerate(files):
            if name not in ('icon', 'banner'):
                off = 0x200 + struct.unpack_from('<I', exefs_hdr, i * 16 + 8)[0]
                exefs_enc[off:off + len(data)] = ctr(0x50, 2, exefs_plain[off:off + len(data)], off)
    enc = bytes(hdr) + ctr(0x2C, 1, exheader) + bytes(exefs_enc) + ctr(0x50, 3, romfs_plain)
    return enc, plain(hdr)


def make_cci(parts):
    """parts: {index: (enc, plain)} -> (cci_bytes, expected_partition_plaintexts)"""
    hdr = bytearray(0x4000)
    hdr[0x100:0x104] = b'NCSD'
    struct.pack_into('<Q', hdr, 0x108, 0x0004000000ABCD00)
    off = 0x4000
    body = bytearray()
    for i in sorted(parts):
        enc = parts[i][0]
        struct.pack_into('<II', hdr, 0x120 + i * 8, off // 0x200, len(enc) // 0x200)
        hdr[0x118 + i] = 3
        body += enc
        off += len(enc)
    struct.pack_into('<I', hdr, 0x104, off // 0x200)
    return bytes(hdr) + bytes(body)


def make_cia(title_id: int, contents):
    """contents: {cindex: (enc_ncch, plain)} -> cia bytes"""
    align = lambda b: b + b'\0' * (-len(b) % 0x40)
    tid = title_id.to_bytes(8, 'big')

    ticket = bytearray(0x350)
    ticket[0x1BF:0x1CF] = os.urandom(16)
    ticket[0x1DC:0x1E4] = tid
    eng = CryptoEngine()
    eng.load_from_ticket(bytes(ticket))

    content_data = b''
    chunks = b''
    for n, (cindex, (ncch, _)) in enumerate(sorted(contents.items())):
        chunks += struct.pack('>IHHQ32s', n, cindex, 1, len(ncch), hashlib.sha256(ncch).digest())
        iv = cindex.to_bytes(2, 'big') + b'\0' * 14
        content_data += eng.create_cbc_cipher(0x40, iv).encrypt(ncch)

    info = struct.pack('>HH32s', 0, len(contents), hashlib.sha256(chunks).digest()).ljust(0x900, b'\0')
    tmd_hdr = bytearray(0xC4)
    tmd_hdr[0:0x1A] = b'Root-CA00000003-CP0000000b'
    tmd_hdr[0x4C:0x54] = tid
    struct.pack_into('>H', tmd_hdr, 0x9E, len(contents))
    tmd_hdr[0xA4:0xC4] = hashlib.sha256(info).digest()
    tmd = struct.pack('>I', 0x00010004) + b'\xff' * 0x100 + b'\0' * 0x3C + bytes(tmd_hdr) + info + chunks

    cert = os.urandom(0xA00)
    meta = os.urandom(0x3AC0)
    index = bytearray(0x2000)
    for c in contents:
        index[c // 8] |= 0x80 >> (c % 8)
    arch = struct.pack('<IHHIIIIQ', 0x2020, 0, 0, len(cert), len(ticket), len(tmd), len(meta),
                       len(content_data)) + bytes(index)
    return align(arch) + align(cert) + align(bytes(ticket)) + align(tmd) + align(content_data) + meta


def ncch(container, index):
    return NCCHReader(container.open_raw_section(index), load_sections=False)


def run(path, keep_cia=False):
    return TMP / decrypt3ds.process(path, TMP, keep_cia, overwrite=True)


class Tests(unittest.TestCase):
    def test_cci(self):
        p0 = make_ncch(0x0004000000ABCD00, 0x0004000000ABCD00, crypto_method=0x01)
        p1 = make_ncch(0x0004000000ABCD00, 0x0005000000ABCD00, crypto_method=0x00, exefs=False)
        src = TMP / 'game.3ds'
        src.write_bytes(make_cci({0: p0, 1: p1}))
        out = run(src)
        self.assertEqual(out.name, 'game-decrypted.3ds')
        data = out.read_bytes()
        self.assertEqual(data[0x4000:0x4000 + len(p0[1])], p0[1])
        self.assertEqual(data[0x4000 + len(p0[1]):], p1[1])
        self.assertEqual(data[0x118:0x120], b'\0' * 8)
        with CCIReader(out, load_contents=False) as cci:
            self.assertTrue(all(ncch(cci, p).flags.no_crypto for p in (0, 1)))

    def test_cci_new3ds_keyslots(self):
        for method in (0x0A, 0x0B):
            p0 = make_ncch(0x0004000000BBBB00, 0x0004000000BBBB00, crypto_method=method)
            src = TMP / f'n3ds-{method}.3ds'
            src.write_bytes(make_cci({0: p0}))
            self.assertEqual(run(src).read_bytes()[0x4000:], p0[1])

    def test_seed(self):
        seed = os.urandom(16)
        p0 = make_ncch(0x0004000000C0FFEE, 0x0004000000C0FFEE, crypto_method=0x01, seed=seed)
        src = TMP / 'seeded.cci'
        src.write_bytes(make_cci({0: p0}))
        self.assertEqual(run(src).read_bytes()[0x4000:], p0[1])

    def test_already_decrypted(self):
        p0 = make_ncch(0x0004000000DDDD00, 0x0004000000DDDD00, encrypted=False)
        src = TMP / 'plain.3ds'
        src.write_bytes(make_cci({0: p0}))
        with self.assertRaises(decrypt3ds.SkipFile):
            run(src)

    def test_cia_game_to_cci(self):
        tid = 0x0004000000EEEE00
        c0 = make_ncch(tid, tid, crypto_method=0x01)
        c1 = make_ncch(tid, tid | 1 << 48, crypto_method=0x00, exefs=False)
        src = TMP / 'eshop.cia'
        src.write_bytes(make_cia(tid, {0: c0, 1: c1}))
        out = run(src)
        self.assertEqual(out.name, 'eshop-decrypted.cci')
        with CCIReader(out, load_contents=False) as cci:
            self.assertEqual(cci.media_id, f'{tid:016x}')
            self.assertEqual(sorted(s for s in cci.sections if s >= 0), [0, 1])
            self.assertTrue(all(ncch(cci, p).flags.no_crypto for p in (0, 1)))
        data = out.read_bytes()
        self.assertEqual(data[0x4000:0x4000 + len(c0[1])], c0[1])
        self.assertEqual(data[0x4000 + len(c0[1]):], c1[1])

    def _check_cia(self, out, tid, contents):
        with CIAReader(out, load_contents=False) as cia:  # also verifies the TMD info record hashes
            self.assertEqual(cia.tmd.title_id, f'{tid:016x}')
            for rec in cia.content_info:
                self.assertFalse(rec.type.encrypted)
                with cia.open_raw_section(rec.cindex) as f:
                    data = f.read()
                self.assertEqual(data, contents[rec.cindex][1])
                self.assertEqual(hashlib.sha256(data).digest(), rec.hash)
                self.assertTrue(ncch(cia, rec.cindex).flags.no_crypto)

    def test_cia_keep_cia(self):
        tid = 0x0004000000FFFF00
        contents = {0: make_ncch(tid, tid, crypto_method=0x01)}
        src = TMP / 'keep.cia'
        raw = make_cia(tid, contents)
        src.write_bytes(raw)
        out = run(src, keep_cia=True)
        self.assertEqual(out.name, 'keep-decrypted.cia')
        self.assertEqual(out.stat().st_size, len(raw))
        self.assertEqual(out.read_bytes()[-0x3AC0:], raw[-0x3AC0:])  # meta kept
        self._check_cia(out, tid, contents)

    def test_cia_update_and_dlc(self):
        for tid in (0x0004000E00AAAA00, 0x0004008C00AAAA00):
            contents = {0: make_ncch(tid, tid, crypto_method=0x00, exefs=False),
                        1: make_ncch(tid, tid, crypto_method=0x00, exefs=False)}
            src = TMP / f'{tid:016x}.cia'
            src.write_bytes(make_cia(tid, contents))
            out = run(src)
            self.assertEqual(out.suffix, '.cia')
            self._check_cia(out, tid, contents)

    def test_missing_seed_cleans_up(self):
        tid = 0x0004000000123400
        p0 = make_ncch(tid, tid, crypto_method=0x01, seed=os.urandom(16))
        import pyctr.crypto.seeddb as sdb
        sdb._seeds.pop(tid, None)
        src = TMP / 'noseed.3ds'
        src.write_bytes(make_cci({0: p0}))
        with self.assertRaises(Exception):
            run(src)
        self.assertFalse((TMP / 'noseed-decrypted.3ds').exists())


if __name__ == '__main__':
    unittest.main(verbosity=2)
