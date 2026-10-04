# decrypt3ds

Batch decryptor for Nintendo 3DS `.3ds` / `.cci` / `.cia` files, for emulators (Azahar, Citra, Lime3DS).
It does the same job as "Batch CIA 3DS Decryptor", but in Python, so it runs on Windows, macOS and Linux.

| Input | Output |
|---|---|
| `.3ds` / `.cci` | `<name>-decrypted.3ds` / `.cci` |
| `.cia` game or demo | `<name>-decrypted.cci` (or `.cia` with `--keep-cia`) |
| `.cia` update or DLC | `<name>-decrypted.cia` (install it in the emulator) |

No keys are included. You need files dumped from **your own** 3DS.

## Setup (once)

1. Install Python 3.8 or newer. On Windows, tick "Add python.exe to PATH" in the installer.
2. Install the library:
   ```
   pip install pyctr
   ```
3. Dump `boot9.bin` from your 3DS with GodMode9:
   `[M:] MEMORY VIRTUAL` → `boot9.bin` → copy to `0:/gm9/out`, then take it from the SD card.
4. Put `boot9.bin` next to `decrypt3ds.py`, or in `%APPDATA%\3ds\` (Windows) / `~/.3ds/` (macOS, Linux).
5. Optional: for seed-encrypted eShop titles, also put `seeddb.bin` there
   (GodMode9: `HOME` → `More...` → `Build support files`, then take `seeddb.bin` from `0:/gm9/out`).

## Use

- **Windows, easiest:** drag game files (or a folder) onto `decrypt3ds.bat`.
- **Command line:**
  ```
  python decrypt3ds.py                        # every .3ds/.cci/.cia in the current folder
  python decrypt3ds.py "Game.cia" "Other.3ds" # specific files
  python decrypt3ds.py D:\roms -o D:\decrypted
  ```

Options:

| Option | Meaning |
|---|---|
| `-o DIR` | Write output to `DIR` instead of next to each file |
| `--boot9 FILE` | Path to `boot9.bin` |
| `--seeddb FILE` | Path to `seeddb.bin` |
| `--keep-cia` | Make a decrypted `.cia` for games too, instead of `.cci` |
| `--overwrite` | Replace existing output files |

Files that are already decrypted are skipped. If a file fails, nothing half-written is left behind.

## Test

`python test_decrypt3ds.py` checks the tool against synthetic encrypted files, using a random fake boot9.
It needs no real keys or games.
