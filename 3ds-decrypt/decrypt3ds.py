#!/usr/bin/env python3
"""
decrypt3ds - batch-decrypt Nintendo 3DS .3ds/.cci and .cia files for emulators (Citra/Azahar/Lime3DS).

Works like "Batch CIA 3DS Decryptor", but in Python and cross-platform:
  * .3ds / .cci          -> <name>-decrypted.3ds / .cci
  * .cia (game or demo)  -> <name>-decrypted.cci      (use --keep-cia to get a decrypted .cia instead)
  * .cia (update / DLC)  -> <name>-decrypted.cia      (updates and DLC must stay CIA to be installed)

No keys are included. You need a dump of the ARM9 bootROM (boot9.bin) from your own console,
made with GodMode9. Titles that use seed crypto also need seeddb.bin (or the seed from your console).

Requires:  pip install pyctr
"""

import argparse
import hashlib
import os
import struct
import sys
from pathlib import Path

try:
    from pyctr.crypto import CryptoEngine
    from pyctr.crypto.engine import BootromNotFoundError, CorruptBootromError
    from pyctr.crypto.seeddb import load_seeddb, MissingSeedError
    from pyctr.type.cci import CCIReader
    from pyctr.type.cia import CIAReader, CIASection
    from pyctr.type.ncch import NCCHReader, NCCHSection, NCCHSeedError
except ImportError:
    sys.exit('pyctr is missing. Install it with:  pip install pyctr')

MEDIA_UNIT = 0x200
CHUNK = 4 * 1024 * 1024
CCI_FIRST_PARTITION = 0x4000
SCRIPT_DIR = Path(__file__).resolve().parent

# title ID high word -> kind of title
TITLE_KINDS = {
    0x00040000: 'game',
    0x00040002: 'demo',
    0x0004000E: 'update',
    0x0004008C: 'dlc',
    0x00040001: 'dlp-child',
    0x00048004: 'dsiware',
}

# TMD signature type -> (signature size, padding size)
TMD_SIG_SIZES = {
    0x00010003: (0x200, 0x3C),
    0x00010004: (0x100, 0x3C),
    0x00010005: (0x3C, 0x40),
}


class SkipFile(Exception):
    """Raised when a file should be skipped with a message, rather than treated as an error."""


# ---------------------------------------------------------------- helpers

def title_kind(title_id: str) -> str:
    return TITLE_KINDS.get(int(title_id, 16) >> 32, 'other')


def copy_stream(src, dst, size: int, progress=None, hasher=None):
    """Copy `size` bytes from src to dst in chunks, optionally hashing and reporting progress."""
    remaining = size
    while remaining:
        data = src.read(min(CHUNK, remaining))
        if not data:
            raise IOError(f'unexpected end of data ({remaining:#x} bytes missing)')
        dst.write(data)
        if hasher:
            hasher.update(data)
        remaining -= len(data)
        if progress:
            progress(len(data))


def pad_to(dst, size: int, fill: bytes = b'\0', hasher=None):
    """Write `fill` bytes until the current position of dst is `size`."""
    missing = size - dst.tell()
    if missing > 0:
        padding = fill * missing
        dst.write(padding)
        if hasher:
            hasher.update(padding)


class Progress:
    def __init__(self, label: str, total: int):
        self.label = label
        self.total = max(total, 1)
        self.done = 0
        self.shown = -1

    def __call__(self, n: int):
        self.done += n
        pct = self.done * 100 // self.total
        if pct != self.shown:
            self.shown = pct
            print(f'\r  {self.label}: {pct:3d}%', end='', flush=True)

    def finish(self):
        print(f'\r  {self.label}: done ({self.done / 1048576:.1f} MiB)')


def open_ncch(fp) -> NCCHReader:
    """
    Open an NCCH for full decryption. Loads the ExeFS (needed to know which parts use which key) but not the RomFS,
    which is only re-encrypted data to us and does not need to be parsed.
    """
    ncch = NCCHReader(fp, load_sections=False)
    real_flags = ncch.flags
    ncch.flags = real_flags._replace(no_romfs=True)
    try:
        ncch.load_sections()
    finally:
        ncch.flags = real_flags
    return ncch


def write_decrypted_ncch(ncch: NCCHReader, dst, progress=None, hasher=None):
    """Write the whole NCCH with every section decrypted and the header flags set to NoCrypto."""
    if ncch.flags.no_crypto:
        src = ncch.open_raw_section(NCCHSection.Raw)
    else:
        src = ncch.open_raw_section(NCCHSection.FullDecrypted)
    with src:
        copy_stream(src, dst, ncch.content_size, progress, hasher)


def is_encrypted(ncch: NCCHReader) -> bool:
    return not ncch.flags.no_crypto


# ---------------------------------------------------------------- .3ds / .cci

def decrypt_cci(src_path: Path, out_path: Path):
    with CCIReader(src_path, load_contents=False) as cci, open(src_path, 'rb') as raw:
        partitions = sorted(int(s) for s in cci.sections if s >= 0)
        if not partitions:
            raise SkipFile('no partitions found')

        ncchs = {p: open_ncch(cci.open_raw_section(p)) for p in partitions}
        if not any(is_encrypted(n) for n in ncchs.values()):
            raise SkipFile('already decrypted')

        app = ncchs.get(0)
        print(f'  CCI  title {cci.media_id}  {app.product_code if app else ""}  partitions {partitions}')

        total = sum(cci.sections[p].size for p in partitions)
        progress = Progress('decrypting', total)
        with open(out_path, 'wb') as out:
            # header, card info and everything else before the first partition is copied as-is
            first = cci.sections[partitions[0]].offset
            copy_stream(raw, out, first)

            for p in partitions:
                region = cci.sections[p]
                pad_to(out, region.offset, b'\xff')
                write_decrypted_ncch(ncchs[p], out, progress)
                pad_to(out, region.offset + region.size, b'\xff')
            progress.finish()

            # mark the partitions as unencrypted in the NCSD partition crypt type table
            out.seek(0x118)
            out.write(b'\0' * 8)


# ---------------------------------------------------------------- .cia

def build_ncsd_header(title_id: str, ncchs: dict, sizes: dict, exheader_hash: bytes) -> bytes:
    """Build a CCI (NCSD) header + card info for partitions laid out from 0x4000. sizes are in bytes."""
    header = bytearray(CCI_FIRST_PARTITION)
    header[0:0x100] = b'\xff' * 0x100                       # signature (not valid, emulators don't check it)
    header[0x100:0x104] = b'NCSD'

    offset = CCI_FIRST_PARTITION
    table = bytearray(0x40)
    ids = bytearray(0x40)
    for p in sorted(ncchs):
        size = sizes[p]
        struct.pack_into('<II', table, p * 8, offset // MEDIA_UNIT, size // MEDIA_UNIT)
        struct.pack_into('<Q', ids, p * 8, int(ncchs[p].partition_id, 16))
        offset += size
    total = offset

    struct.pack_into('<I', header, 0x104, total // MEDIA_UNIT)
    struct.pack_into('<Q', header, 0x108, int(title_id, 16))
    # 0x110 partition FS types and 0x118 crypt types stay zero
    header[0x120:0x160] = table
    header[0x160:0x180] = exheader_hash
    # partition flags: [4] platform = CTR, [5] media type = card1, [6] media unit = 0x200 << 0
    header[0x188 + 4] = 1
    header[0x188 + 5] = 1
    header[0x190:0x1D0] = ids

    # card info header
    struct.pack_into('<I', header, 0x200, 0xFFFFFFFF)       # writable address (none for card1)
    struct.pack_into('<I', header, 0x300, total)            # filled size
    return bytes(header), total


def decrypt_cia_to_cci(cia: CIAReader, out_path: Path):
    title_id = cia.tmd.title_id
    # CCI partitions 0-7 map to CIA content indexes 0-7 (0 = game, 1 = manual, 2 = download play child)
    ncchs = {r.cindex: open_ncch(cia.open_raw_section(r.cindex)) for r in cia.content_info if r.cindex < 8}
    if 0 not in ncchs:
        raise SkipFile('no main application content (index 0); use --keep-cia')

    sizes = {p: n.content_size for p, n in ncchs.items()}
    app = ncchs[0]
    with app.open_raw_section(NCCHSection.FullDecrypted if is_encrypted(app) else NCCHSection.Raw) as f:
        f.seek(0x160)
        exheader_hash = f.read(0x20)
    header, total = build_ncsd_header(title_id, ncchs, sizes, exheader_hash)

    print(f'  CIA -> CCI  title {title_id}  {app.product_code}  contents {sorted(ncchs)}')
    progress = Progress('decrypting', total - CCI_FIRST_PARTITION)
    with open(out_path, 'wb') as out:
        out.write(header)
        for p in sorted(ncchs):
            write_decrypted_ncch(ncchs[p], out, progress)
        progress.finish()


def decrypt_cia_to_cia(cia: CIAReader, src_path: Path, out_path: Path):
    """
    Write a CIA with every content decrypted (title key layer and NCCH layer), content "encrypted" flags cleared and
    the content hashes in the TMD updated. The signatures are left as they were (emulators don't check them).
    """
    title_id = cia.tmd.title_id
    print(f'  CIA  title {title_id}  ({title_kind(title_id)})  contents {[r.cindex for r in cia.content_info]}')

    tmd_region = cia.sections[CIASection.TitleMetadata]
    content_start = min(cia.sections[r.cindex].offset for r in cia.content_info) if cia.content_info else None

    with open(src_path, 'rb') as raw, open(out_path, 'wb') as out:
        # archive header, certificate chain, ticket and TMD are copied as-is; the TMD is patched afterwards
        copy_stream(raw, out, content_start)
        raw.seek(tmd_region.offset)
        tmd = bytearray(raw.read(tmd_region.size))

        total = sum(r.size for r in cia.content_info)
        progress = Progress('decrypting', total)
        new_hashes = {}
        for record in cia.content_info:
            region = cia.sections[record.cindex]
            hasher = hashlib.sha256()
            out.seek(region.offset)
            ncch = open_ncch(cia.open_raw_section(record.cindex))
            write_decrypted_ncch(ncch, out, progress, hasher)
            pad_to(out, region.offset + record.size, hasher=hasher)
            new_hashes[record.cindex] = hasher.digest()
        progress.finish()

        # copy the rest (padding + meta section)
        end = content_start + total
        raw.seek(end)
        out.seek(end)
        copy_stream(raw, out, os.path.getsize(src_path) - end)

        # patch the TMD: content type flags and hashes, then the info record hashes
        sig_type = struct.unpack_from('>I', tmd, 0)[0]
        sig_size, sig_pad = TMD_SIG_SIZES[sig_type]
        hdr = 4 + sig_size + sig_pad
        content_count = struct.unpack_from('>H', tmd, hdr + 0x9E)[0]
        info_start = hdr + 0xC4
        chunk_start = info_start + 0x900
        for i in range(content_count):
            rec = chunk_start + i * 0x30
            cindex, ctype = struct.unpack_from('>HH', tmd, rec + 4)
            if cindex in new_hashes:
                struct.pack_into('>H', tmd, rec + 6, ctype & ~1)
                tmd[rec + 0x10:rec + 0x30] = new_hashes[cindex]
        for i in range(64):
            info = info_start + i * 0x24
            index_offset, count = struct.unpack_from('>HH', tmd, info)
            if count == 0:
                continue
            chunks = tmd[chunk_start + index_offset * 0x30:chunk_start + (index_offset + count) * 0x30]
            tmd[info + 4:info + 0x24] = hashlib.sha256(chunks).digest()
        tmd[hdr + 0xA4:hdr + 0xC4] = hashlib.sha256(tmd[info_start:chunk_start]).digest()
        out.seek(tmd_region.offset)
        out.write(tmd)


# ---------------------------------------------------------------- driver

def output_path(src: Path, out_dir: Path, ext: str) -> Path:
    return out_dir / f'{src.stem}-decrypted{ext}'


def process(src: Path, out_dir: Path, keep_cia: bool, overwrite: bool) -> str:
    ext = src.suffix.lower()
    if src.stem.endswith('-decrypted'):
        raise SkipFile('looks like an output of this tool')

    if ext in ('.3ds', '.cci'):
        out = output_path(src, out_dir, ext)
        job = lambda: decrypt_cci(src, out)
    elif ext == '.cia':
        def job():
            with CIAReader(src, load_contents=False) as cia:
                if to_cci:
                    decrypt_cia_to_cci(cia, out)
                else:
                    decrypt_cia_to_cia(cia, src, out)

        with CIAReader(src, load_contents=False) as cia:
            kind = title_kind(cia.tmd.title_id)
        if kind == 'dsiware':
            raise SkipFile('DSiWare (Nintendo DS) title, not supported')
        to_cci = kind in ('game', 'demo') and not keep_cia
        out = output_path(src, out_dir, '.cci' if to_cci else '.cia')
    else:
        raise SkipFile('not a .3ds/.cci/.cia file')

    if out.exists() and not overwrite:
        raise SkipFile(f'{out.name} already exists (use --overwrite)')
    try:
        job()
    except BaseException:
        if out.exists():
            out.unlink()  # never leave a half-written file behind
        raise
    return out.name


def find_inputs(paths):
    for p in paths:
        p = Path(p)
        if p.is_dir():
            yield from sorted(f for f in p.iterdir() if f.suffix.lower() in ('.3ds', '.cci', '.cia') and f.is_file())
        else:
            yield p


def find_file(explicit, name):
    """Return the given path, or `name` next to this script or in the current directory."""
    if explicit:
        return Path(explicit)
    for d in (SCRIPT_DIR, Path.cwd()):
        if (d / name).is_file():
            return d / name
    return None  # pyctr then looks in its default places (~/.3ds, ~/3ds, %APPDATA%\3ds, ...)


def main():
    ap = argparse.ArgumentParser(description='Decrypt 3DS .3ds/.cci/.cia files for emulators.')
    ap.add_argument('inputs', nargs='*', default=['.'], help='files or folders (default: current folder)')
    ap.add_argument('-o', '--out', help='output folder (default: next to each input)')
    ap.add_argument('--boot9', help='path to boot9.bin dumped from your console')
    ap.add_argument('--seeddb', help='path to seeddb.bin (needed for seed-encrypted titles)')
    ap.add_argument('--keep-cia', action='store_true', help='output decrypted .cia for games instead of .cci')
    ap.add_argument('--overwrite', action='store_true', help='replace existing output files')
    args = ap.parse_args()

    boot9 = find_file(args.boot9, 'boot9.bin')
    try:
        CryptoEngine(boot9=str(boot9) if boot9 else None)  # loads the keys once for every reader
    except (BootromNotFoundError, CorruptBootromError, FileNotFoundError) as e:
        sys.exit(f'Could not load boot9.bin: {e}\n'
                 'Dump it from your own 3DS with GodMode9 and put it next to this script or pass --boot9.')

    seeddb = find_file(args.seeddb, 'seeddb.bin')
    if seeddb:
        load_seeddb(str(seeddb))

    ok = skipped = failed = 0
    for src in find_inputs(args.inputs):
        out_dir = Path(args.out) if args.out else src.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        print(f'{src.name}')
        try:
            name = process(src, out_dir, args.keep_cia, args.overwrite)
            print(f'  -> {name}')
            ok += 1
        except SkipFile as e:
            print(f'  skipped: {e}')
            skipped += 1
        except (MissingSeedError, NCCHSeedError) as e:
            print(f'  FAILED: this title needs a seed ({e}). Provide seeddb.bin with --seeddb.')
            failed += 1
        except KeyboardInterrupt:
            print('\ninterrupted')
            sys.exit(130)
        except Exception as e:
            print(f'  FAILED: {type(e).__name__}: {e}')
            failed += 1

    print(f'\n{ok} decrypted, {skipped} skipped, {failed} failed')
    sys.exit(1 if failed else 0)


if __name__ == '__main__':
    main()
