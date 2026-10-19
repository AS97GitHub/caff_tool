import json
import os
import re
import shutil
import struct
import sys

try:
    import tomllib
except ImportError:
    print("Error: Python 3.11 or higher is required.")
    sys.exit(1)


# --- terminal colors (GitHub Dark palette, 256-color ANSI) ---
_COLOR = sys.stdout.isatty()


def _c(text: str, code: str) -> str:
    """Wrap text in an ANSI SGR sequence; plain text when not a TTY."""
    return f"\x1b[{code}m{text}\x1b[0m" if _COLOR else text


def _enable_ansi() -> None:
    """Enable ANSI (VT) processing on the Windows console; no-op elsewhere."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)  # STD_OUTPUT_HANDLE
        mode = ctypes.c_ulong()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


TEAL = "38;5;115"    # #4ec9b0 - logo, frames
BLACK = "38;2;0;0;0"  # #000000 - logo background cells


def _paint_logo(line: str) -> str:
    """Paint logo cells: solid blocks teal, shade cells black (run-length ANSI)."""
    if not _COLOR:
        return line
    out, prev = [], None
    for ch in line:
        color = TEAL if ch == "\u2588" else BLACK
        if color != prev:
            out.append(f"\x1b[{color}m")
            prev = color
        out.append(ch)
    out.append("\x1b[0m")
    return "".join(out)
BLUE = "38;5;75"     # #79c0ff - headings, flags
ORANGE = "38;5;215"  # #ffa657 - executable name
PURPLE = "38;5;183"  # #d2a8ff - commands
GRAY = "38;5;245"    # #8b949e - comments
WHITE = "38;5;255"   # #e6edf3 - foreground
GREEN = "38;5;78"    # #3fb950 - [SUCCESS], [OK]
RED = "38;5;204"     # #f85149 - [ERROR]
YELLOW = "38;5;172"  # #d29922 - [WARNING]


class CaffTomlConverter:

    # ------------------------------------------------------------------
    # CAFF header checksum (field 0x14). Verified in-game (xemu).
    # Computed over the first 0x180 bytes with the checksum field zeroed.
    # ------------------------------------------------------------------
    @staticmethod
    def caff_checksum(data: bytes) -> int:
        buf = bytearray(data[:0x180])
        if len(buf) < 0x180:
            buf.extend(b"\x00" * (0x180 - len(buf)))
        struct.pack_into("<I", buf, 0x14, 0)

        def do_byte(initial: int, byte: int) -> int:
            signed = byte if byte < 128 else byte - 256
            shifted = (initial << 4) & 0xFFFFFFFF
            if shifted >= 0x80000000:
                shifted -= 0x100000000
            val = shifted + signed
            val_u = val & 0xFFFFFFFF
            mask = val_u & 0xF0000000
            if mask:
                mask |= mask >> 24
                val_u ^= mask
            return val_u & 0xFFFFFFFF

        chksm = 0
        for i in range(0x180):
            chksm = do_byte(chksm, buf[i])
        return chksm

    # ------------------------------------------------------------------
    # Structural parser: keys -> LSBL#1 offset table -> text blob -> tail
    # ------------------------------------------------------------------
    @staticmethod
    def _parse_caff(raw: bytes) -> dict:
        if raw[:4] != b"CAFF":
            raise ValueError("Invalid format: missing CAFF header.")

        count = struct.unpack_from("<I", raw, 0x1A8)[0]

        # Key names (UTF-16LE), addressed by the key table at 0x1AC
        keys = []
        key_x = []
        for i in range(count):
            koff, x = struct.unpack_from("<II", raw, 0x1AC + i * 8)
            key_x.append(x)
            kstart = 0x180 + koff
            # wchar-aligned scan: terminator is \x00\x00, but the char's own
            # low zero byte must not be mistaken for its start
            kend = kstart
            while kend + 1 < len(raw) and not (
                raw[kend] == 0 and raw[kend + 1] == 0
            ):
                kend += 2
            keys.append(raw[kstart:kend].decode("utf-16-le", errors="ignore"))
        kt_end = 0x1AC + count * 8

        lsbl = raw.find(b"LSBL", kt_end)
        if lsbl < 0:
            raise ValueError("Invalid file: missing LSBL block.")

        hdr = [struct.unpack_from("<I", raw, lsbl + o)[0] for o in (4, 8, 12, 16, 20, 24, 28)]

        # Offset table at LSBL+0x24: (u16 line, u32 wchar_offset)*, ends with (0xFFFF, end)
        tbl = []
        p = lsbl + 0x24
        while p + 6 <= len(raw):
            line, off = struct.unpack_from("<HI", raw, p)
            tbl.append((line, off))
            p += 6
            if line == 0xFFFF:
                break
        else:
            raise ValueError("LSBL offset table has no terminator.")

        starts = [(l, o) for l, o in tbl if l != 0xFFFF]
        txt_start = p
        end_wchar = tbl[-1][1]

        # Original strings as raw byte slices (including NUL terminator)
        strs = []
        for i, (line, off) in enumerate(starts):
            s = txt_start + off * 2
            e = txt_start + (starts[i + 1][1] if i + 1 < len(starts) else end_wchar) * 2
            strs.append(raw[s:e])

        return {
            "count": count,
            "keys": keys,
            "key_x": key_x,
            "lsbl": lsbl,
            "hdr": hdr,
            "tbl": tbl,
            "starts": starts,
            "txt_start": txt_start,
            "strs": strs,
            "tail": raw[txt_start + end_wchar * 2:],
            "pre": bytearray(raw[:lsbl]),          # header + key table + keys
            "old_blob_len": end_wchar * 2,
        }

    # ------------------------------------------------------------------
    # Rebuild: new_strs = list of bytes, each including trailing \x00\x00.
    # Recomputes LSBL#1 offsets, size fields, B/C and header checksum.
    # ------------------------------------------------------------------
    @staticmethod
    def _rebuild_caff(parsed: dict, new_strs: list) -> bytes:
        if len(new_strs) != len(parsed["starts"]):
            raise ValueError(
                f"String count mismatch: table has {len(parsed['starts'])}, got {len(new_strs)}"
            )

        blob = b"".join(new_strs)
        new_tbl = []
        off = 0
        for (line, _), s in zip(parsed["starts"], new_strs):
            if len(s) % 2:
                raise ValueError("UTF-16 string has odd byte length.")
            new_tbl.append((line, off))
            off += len(s) // 2
        new_tbl.append((0xFFFF, off))  # terminator = end of text block (wchar)

        delta = len(blob) - parsed["old_blob_len"]

        table_bytes = b"".join(struct.pack("<HI", l, o) for l, o in new_tbl)
        lsbl_prefix = 36 + 6 * len(new_tbl)          # magic + 7*u32 + count + table
        A = lsbl_prefix + len(blob) + 2              # = txt_end - lsbl + 2 (verified)
        D = A - 0x1C
        B = parsed["hdr"][4] + delta                 # fields past text shift by delta
        C = parsed["hdr"][5] + delta

        lsbl_blk = b"LSBL"
        lsbl_blk += b"".join(struct.pack("<I", v) for v in (parsed["hdr"][0], parsed["hdr"][1], parsed["hdr"][2], A, B, C, D))
        lsbl_blk += struct.pack("<I", parsed["count"])
        lsbl_blk += table_bytes

        pre = bytearray(parsed["pre"])
        size = len(pre) + len(lsbl_blk) + len(blob) + len(parsed["tail"])
        struct.pack_into("<I", pre, 0x34, struct.unpack_from("<I", pre, 0x34)[0] + delta)
        struct.pack_into("<I", pre, 0x50, size - 0x180)
        struct.pack_into("<I", pre, 0x64, size - 0x180)
        struct.pack_into("<I", pre, 0x194, struct.unpack_from("<I", pre, 0x194)[0] + delta)

        result = bytearray(bytes(pre) + lsbl_blk + blob + parsed["tail"])
        struct.pack_into("<I", result, 0x14, CaffTomlConverter.caff_checksum(result))
        return bytes(result)

    # ------------------------------------------------------------------
    # Apply TOML entries onto raw binary data.
    # Short/equal strings -> in-place patching (byte-exact).
    # If any string is longer than its buffer -> full CAFF rebuild
    # (LSBL#1 offset table + size fields + checksum are recalculated).
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # TOML string helper: JSON escapes are valid in TOML; text is written
    # as literal UTF-8 (readable Cyrillic etc.); U+007F escaped by hand
    # (TOML forbids it raw inside strings).
    # ------------------------------------------------------------------
    @staticmethod
    def _s(value: str) -> str:
        return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")

    @staticmethod
    def _sl(value: str) -> str:
        """TOML literal string (single quotes, no escapes) when safe."""
        if value and not re.search(r"[\x00-\x1f\x7f']", value):
            return "'" + value + "'"
        return CaffTomlConverter._s(value)

    # ------------------------------------------------------------------
    # Expand TOML entries into flat patch items:
    #   (offset, max_bytes, text, label)
    # Main text comes from the entry; position_x/position_y/scale fields
    # are resolved to their own strings via key-name lookup in the binary.
    # ------------------------------------------------------------------
    @staticmethod
    def _collect_items(raw_bytes: bytes, entries: list) -> list:
        parsed = CaffTomlConverter._parse_caff(raw_bytes)
        line2idx = {L: j for j, (L, _o) in enumerate(parsed["starts"])}
        key2off = {}
        for kname, x in zip(parsed["keys"], parsed["key_x"]):
            j = line2idx.get(x >> 16)
            if j is not None and kname not in key2off:
                key2off[kname] = parsed["txt_start"] + parsed["starts"][j][1] * 2
        off2len = {
            parsed["txt_start"] + off * 2: len(parsed["strs"][j]) - 2
            for j, (_L, off) in enumerate(parsed["starts"])
        }

        matches = CaffTomlConverter._get_string_matches(bytearray(raw_bytes), entries)

        items = []
        for i, entry in enumerate(entries):
            label = entry.get("key", f"Entry #{i+1}")
            # compound "A|B|C" resolves via base "A"; but a real key that
            # literally contains '|' always wins
            if label in key2off or "|" not in label:
                base_label = label
            else:
                base_label = label.split("|")[0]
            if "offset" in entry and "max_bytes" in entry:
                # explicit metadata (old TOMLs) wins
                off, mb = entry["offset"], entry["max_bytes"]
            else:
                # resolve by key name via the key-table/line binding
                off = key2off.get(base_label)
                if off is None:
                    raise ValueError(
                        f"Key '{label}' not found in binary; cannot map entry."
                    )
                mb = off2len[off]
            items.append((off, mb, entry.get("text", ""), label))
            for field, suffix in (
                ("position_x", "_POSITION_X"),
                ("position_y", "_POSITION_Y"),
                ("scale", "_SCALE"),
            ):
                if field in entry:
                    poff = key2off.get(base_label + suffix)
                    if poff is None:
                        raise ValueError(
                            f"Key '{base_label + suffix}' not found in binary; "
                            f"cannot map '{field}'."
                        )
                    items.append((poff, off2len[poff], entry[field], label + suffix))
        return items

    # ------------------------------------------------------------------
    # Apply flat patch items. Short/equal -> in-place (byte-exact);
    # longer -> full CAFF rebuild (LSBL#1 offsets, sizes, checksum).
    # ------------------------------------------------------------------
    @staticmethod
    def _apply_items(raw_data: bytearray, items: list) -> bytes:
        encoded = [text.encode("utf-16le") for (_o, _m, text, _l) in items]
        # In-place patching is safe ONLY for exact-length replacements:
        # shorter strings padded with NULs would leave embedded zero-wchars
        # inside the string (the engine reads by LSBL length), breaking it.
        oversized = [
            i for i, (_o, mb, _t, _l) in enumerate(items)
            if len(encoded[i]) != mb
        ]

        if not oversized:
            for (off, mb, _t, _l), enc in zip(items, encoded):
                raw_data[off:off + mb] = enc + b"\x00" * (mb - len(enc))
            return bytes(raw_data)

        # ---- rebuild path -------------------------------------------------
        parsed = CaffTomlConverter._parse_caff(bytes(raw_data))
        offset_to_idx = {
            parsed["txt_start"] + off * 2: i for i, (_L, off) in enumerate(parsed["starts"])
        }
        offset2label = {off: label for (off, _m, _t, label) in items}

        new_strs = list(parsed["strs"])  # unchanged strings keep original bytes
        rebuilt = set()
        for (off, _mb, text, _l), enc in zip(items, encoded):
            idx = offset_to_idx.get(off)
            if idx is None:
                continue
            old_text = parsed["strs"][idx][:-2].decode("utf-16-le", errors="ignore")
            if text == old_text:
                continue
            new_strs[idx] = enc + b"\x00\x00"
            rebuilt.add(idx)

        print(_c("\n[INFO]", BLUE) + " String(s) changed length -> full CAFF rebuild:")
        for idx in sorted(rebuilt):
            abs_off = parsed["txt_start"] + parsed["starts"][idx][1] * 2
            print(f"  + '{offset2label.get(abs_off, '#' + str(idx))}' "
                  f"({len(parsed['strs'][idx]) - 2} -> {len(new_strs[idx]) - 2} bytes)")
        print(f"  Text block delta: "
              f"{sum(len(s) for s in new_strs) - parsed['old_blob_len']:+d} bytes")

        return CaffTomlConverter._rebuild_caff(parsed, new_strs)

    @staticmethod
    def _build_records(parsed: dict) -> list:
        """Key-bound string records (positions merged into base entries)."""
        # Keys are bound to strings via the source-script line number:
        # key_table.X high 16 bits == LSBL#1 entry line (u16) of the string.
        line2key = {}
        for kname, x in zip(parsed["keys"], parsed["key_x"]):
            line2key.setdefault(x >> 16, kname)

        # Strings located via the LSBL#1 offset table (wchar offsets)
        records = []
        for i, raw_s in enumerate(parsed["strs"]):
            records.append({
                "key": line2key.get(parsed["starts"][i][0], f"string_key_{i}"),
                "offset": parsed["txt_start"] + parsed["starts"][i][1] * 2,
                "max_bytes": len(raw_s) - 2,
                "text": raw_s[:-2].decode("utf-16-le", errors="ignore"),
            })

        # Merge layout records: KEY_POSITION_X / KEY_POSITION_Y / KEY_SCALE
        # become fields of the base KEY entry.
        by_key = {r["key"]: r for r in records}
        pos_suffixes = (
            ("_POSITION_X", "position_x"),
            ("_POSITION_Y", "position_y"),
            ("_SCALE", "scale"),
        )
        consumed = set()
        for r in records:
            for suffix, field in pos_suffixes:
                if r["key"].endswith(suffix):
                    base = by_key.get(r["key"][: -len(suffix)])
                    if base is not None and field not in base:
                        base[field] = r["text"]
                        base.setdefault("_members", [base["key"]]).append(r["key"])
                        consumed.add(id(r))
                    break
        return [r for r in records if id(r) not in consumed]

    @staticmethod
    def _entries_lines(merged: list, bank: str = None) -> list:
        """[[entries]] TOML lines for merged records (optionally tagged with bank)."""
        toml_lines = []
        for r in merged:
            toml_lines.append("[[entries]]")
            if "_members" in r:
                rank = {"_POSITION_X": 0, "_POSITION_Y": 1, "_SCALE": 2}
                rest = sorted(
                    r["_members"][1:],
                    key=lambda k: next(
                        (v for s, v in rank.items() if k.endswith(s)), 9
                    ),
                )
                display_key = "|".join([r["_members"][0]] + rest)
            else:
                display_key = r["key"]
            toml_lines.append(f"key = {CaffTomlConverter._s(display_key)}")
            if bank is not None:
                toml_lines.append(f"bank = {CaffTomlConverter._s(bank)}")
            for field in ("position_x", "position_y", "scale"):
                if field in r:
                    toml_lines.append(f"{field} = {CaffTomlConverter._s(r[field])}")
            toml_lines.append(f"text = {CaffTomlConverter._s(r['text'])}")
            toml_lines.append("")
        return toml_lines

    @staticmethod
    def bin_to_toml(bin_bytes: bytes) -> str:
        if not bin_bytes.startswith(b"CAFF"):
            raise ValueError("Invalid format: missing CAFF header.")

        # Structural parse: key table -> keys -> LSBL#1 -> exact string boundaries
        parsed = CaffTomlConverter._parse_caff(bin_bytes)
        merged = CaffTomlConverter._build_records(parsed)

        # Build Path & Script metadata (informational, lives in the tail)
        remaining = parsed["tail"]
        audio_meta = ""
        audio_start = remaining.find(b";")
        if audio_start != -1:
            audio_end = remaining.find(b"\x00", audio_start)
            if audio_end != -1:
                audio_meta = remaining[audio_start:audio_end].decode(
                    "ascii", errors="ignore"
                )

        build_path = ""
        path_start = remaining.find(b":\\")
        if path_start != -1:
            path_start_offset = path_start - 1
            path_end = remaining.find(b"\x00", path_start_offset)
            if path_end != -1:
                build_path = remaining[path_start_offset:path_end].decode(
                    "ascii", errors="ignore"
                )

        # Construct TOML
        toml_lines = [
            "[meta]",
            f"build_path = {CaffTomlConverter._sl(build_path)}",
            "",
            "[script]",
            f"audio_meta = {CaffTomlConverter._s(audio_meta)}",
            "",
        ]

        toml_lines.extend(CaffTomlConverter._entries_lines(merged))

        toml_lines.append("[binary]")
        toml_lines.append(f'template_hex = "{bin_bytes.hex()}"\n')

        return "\n".join(toml_lines)

    @staticmethod
    def _get_string_matches(raw_data: bytearray, entries: list) -> list:
        """Utility to retrieve (offset, max_bytes) tuples from TOML metadata or via structural fallback."""
        matches = []
        has_metadata = all("offset" in e and "max_bytes" in e for e in entries)

        if has_metadata and len(entries) > 0:
            for entry in entries:
                matches.append((entry["offset"], entry["max_bytes"]))
        else:
            parsed = CaffTomlConverter._parse_caff(bytes(raw_data))
            for i, raw_s in enumerate(parsed["strs"][: len(entries)]):
                off_abs = parsed["txt_start"] + parsed["starts"][i][1] * 2
                matches.append((off_abs, len(raw_s) - 2))

        return matches

    @staticmethod
    def toml_to_bin(toml_str: str) -> bytes:
        parsed = tomllib.loads(toml_str)
        template_hex = parsed.get("binary", {}).get("template_hex", "")

        if not template_hex:
            raise ValueError("TOML file missing [binary] template_hex field.")

        raw_data = bytearray(bytes.fromhex(template_hex))
        entries = parsed.get("entries", [])
        items = CaffTomlConverter._collect_items(bytes(raw_data), entries)
        return CaffTomlConverter._apply_items(raw_data, items)

    @staticmethod
    def pack_safe(toml_path: str, target_bin_path: str = None) -> bytes:
        """Safe mode string patching directly into binary data."""
        with open(toml_path, "r", encoding="utf-8") as f:
            parsed = tomllib.loads(f.read())

        # Determine binary source: target file on disk or embedded template_hex fallback
        raw_data = None
        if target_bin_path and os.path.exists(target_bin_path):
            with open(target_bin_path, "rb") as f:
                raw_data = bytearray(f.read())
        else:
            template_hex = parsed.get("binary", {}).get("template_hex", "")
            if template_hex:
                raw_data = bytearray(bytes.fromhex(template_hex))
            else:
                target_desc = (
                    f"'{target_bin_path}'" if target_bin_path else "target .bin"
                )
                print(
                    f"[WARNING] Could not find {target_desc} and TOML lacks template_hex field."
                )
                input(_c("Execution paused. Press Enter to exit...", WHITE))
                sys.exit(1)

        entries = parsed.get("entries", [])
        items = CaffTomlConverter._collect_items(bytes(raw_data), entries)

        # Check sizes up-front and inform about the rebuild path
        warnings = []
        for (off, mb, text, label) in items:
            new_len = len(text.encode("utf-16le"))
            if new_len != mb:
                warnings.append((label, new_len, mb))

        if warnings:
            print(f"\n{_c('[INFO]', BLUE)} {len(warnings)} string(s) changed length.")
            print(       "       They will be inserted via full CAFF rebuild (offsets/checksum auto-fixed).")
            for label, new_len, old_len in warnings:
                print(f"       ~ '{label}': {old_len} -> {new_len} bytes")

        return CaffTomlConverter._apply_items(raw_data, items)

    @staticmethod
    def unpack_all(lang_dir: str, out_toml: str) -> None:
        """All banks of a language folder into a single combined TOML."""
        lang_dir = lang_dir.rstrip("/\\")
        banks = sorted(
            d for d in os.listdir(lang_dir)
            if os.path.isfile(os.path.join(lang_dir, d, "default.bin"))
        )
        if not banks:
            raise ValueError(f"No default.bin found in subfolders of '{lang_dir}'")

        toml_lines = [
            "[meta]",
            f"language_dir = {CaffTomlConverter._s(lang_dir)}",
            f"bank_count = {len(banks)}",
            "",
        ]
        total_entries = 0
        for bank in banks:
            with open(os.path.join(lang_dir, bank, "default.bin"), "rb") as f:
                data = f.read()
            parsed = CaffTomlConverter._parse_caff(data)
            merged = CaffTomlConverter._build_records(parsed)
            total_entries += len(merged)
            toml_lines.extend(CaffTomlConverter._entries_lines(merged, bank=bank))
        with open(out_toml, "w", encoding="utf-8") as f:
            f.write("\n".join(toml_lines))
        print(f"{_c('[SUCCESS]', GREEN)} {len(banks)} banks, {total_entries} entries -> {out_toml}")

    @staticmethod
    def pack_all(toml_path: str, lang_dir: str, out_dir: str = None) -> None:
        """Distribute a combined TOML back into the banks of a language folder.

        If out_dir is given, patched banks are written there (folder structure
        preserved, untouched banks copied as-is) instead of overwriting lang_dir.
        """
        with open(toml_path, "r", encoding="utf-8") as f:
            combined = tomllib.loads(f.read())
        entries = combined.get("entries", [])
        lang_dir = lang_dir.rstrip("/\\")
        out_dir = out_dir.rstrip("/\\") if out_dir else None
        mirror = bool(out_dir) and os.path.abspath(out_dir) != os.path.abspath(lang_dir)
        if mirror:
            os.makedirs(out_dir, exist_ok=True)

        by_bank = {}
        for e in entries:
            bank = e.get("bank")
            if not bank:
                raise ValueError(
                    f"Entry '{e.get('key')}' has no 'bank' field - "
                    f"this is not a combined TOML (use 'pack' instead)"
                )
            by_bank.setdefault(bank, []).append(e)

        if mirror:
            # copy banks that have no entries in the TOML, so out_dir is complete
            for bank in sorted(os.listdir(lang_dir)):
                src = os.path.join(lang_dir, bank, "default.bin")
                if os.path.isfile(src) and bank not in by_bank:
                    dst = os.path.join(out_dir, bank)
                    os.makedirs(dst, exist_ok=True)
                    shutil.copyfile(src, os.path.join(dst, "default.bin"))
                    print(f"{_c('[OK]', GREEN)} {bank}: copied unchanged")

        n_banks = 0
        for bank, bank_entries in sorted(by_bank.items()):
            bin_path = os.path.join(lang_dir, bank, "default.bin")
            if not os.path.exists(bin_path):
                print(f"{_c('[WARNING]', YELLOW)} {bin_path} not found, bank '{bank}' skipped")
                continue
            with open(bin_path, "rb") as f:
                raw = bytearray(f.read())
            items = CaffTomlConverter._collect_items(bytes(raw), bank_entries)
            changed = sum(
                1 for (_o, mb, t, _l) in items
                if len(t.encode("utf-16le")) != mb
            )
            out = CaffTomlConverter._apply_items(raw, items)
            out_path = bin_path
            if mirror:
                out_dir_bank = os.path.join(out_dir, bank)
                os.makedirs(out_dir_bank, exist_ok=True)
                out_path = os.path.join(out_dir_bank, "default.bin")
            with open(out_path, "wb") as f:
                f.write(out)
            n_banks += 1
            tag = "rebuild" if changed else "in-place"
            print(f"{_c('[OK]', GREEN)} {bank}: {len(bank_entries)} entries ({tag})")
        dest = out_dir if mirror else lang_dir
        print(f"{_c('[SUCCESS]', GREEN)} {n_banks} banks written to '{dest}'")

    @staticmethod
    def verify_lossless(
        original_bytes: bytes, toml_str: str
    ) -> tuple[bool, str]:
        """In-memory roundtrip validation test."""
        repacked_bytes = CaffTomlConverter.toml_to_bin(toml_str)

        if original_bytes == repacked_bytes:
            return True, "100% byte-for-byte match"

        min_len = min(len(original_bytes), len(repacked_bytes))
        diff_offset = next(
            (
                i
                for i in range(min_len)
                if original_bytes[i] != repacked_bytes[i]
            ),
            min_len,
        )

        return (
            False,
            f"Mismatch detected at offset 0x{diff_offset:X} (Original: {len(original_bytes)} bytes, Repacked: {len(repacked_bytes)} bytes)",
        )


# --- CLI ---
def exit_with_pause():
    """Exit program with pause to keep window open."""
    try:
        input(_c("\nPress Enter to exit...", WHITE))
    except EOFError:
        pass  # non-interactive run (pipe/redirect) -> exit without pause
    sys.exit()

def print_usage():
    """Print usage information and examples for the CLI (GitHub Dark style)."""
    executable_name = os.path.basename(sys.argv[0])
    logo = [
        "░██████░░██████░░██████░░██████░░░░██████░░██████░░██████░░██░░░░░",
        "░██░░░░░░██░░██░░██░░░░░░██░░░░░░░░░░██░░░░██░░██░░██░░██░░██░░░░░",
        "░██░░░░░░██████░░█████░░░█████░░░░░░░██░░░░██░░██░░██░░██░░██░░░░░",
        "░██░░░░░░██░░██░░██░░░░░░██░░░░░░░░░░██░░░░██░░██░░██░░██░░██░░░░░",
        "░██████░░██░░██░░██░░░░░░██░░░░░░░░░░██░░░░██████░░██████░░██████░",
    ]
    h = _c("Usage:", BLUE + ";1")
    m = _c("Modes:", BLUE + ";1")
    n = _c("Notes:", BLUE + ";1")
    e = _c(executable_name, ORANGE)

    def row(cmd, desc, comment=""):
        c = _c(cmd, PURPLE) if cmd else ""
        line = "  " + c + " " * max(1, 21 - len(cmd)) + desc
        if comment:
            line += " " + _c(comment, GRAY)
        print(line)

    print()
    for line in logo:
        print(_paint_logo(line))
    print()
    print(h)
    print(f"  {e} {_c('unpack', PURPLE)} input.bin [output.toml]")
    print(f"  {e} {_c('pack', PURPLE)} input.toml [output.bin]")
    print(f"  {e} {_c('pack', PURPLE)} input.toml [target.bin] [output.bin] {_c('--safe', BLUE)}")
    print(f"  {e} {_c('unpack-all', PURPLE)} <lang_dir> <output.toml>")
    print(f"  {e} {_c('pack-all', PURPLE)} <combined.toml> <lang_dir> [out_dir]")
    print()
    print(m)
    row("unpack", ".bin -> .toml", "(with lossless round-trip verification)")
    row("pack", ".toml -> .bin", "(from embedded template_hex)")
    row("pack --safe", "patch a live .bin", "(overwrites target, or writes [output.bin])")
    row("unpack-all", "all banks of a language folder -> one combined TOML")
    row("pack-all", "combined TOML -> banks patched in the language folder")
    row("", "(with [out_dir]: written there, untouched banks copied)")
    print()
    print(n)
    print("  - Only equal-length strings are patched in place " + _c("(byte-exact)", GRAY) + ".")
    print("  - Any length change triggers a full CAFF rebuild: LSBL offset table,")
    print("    size fields and header checksum are recalculated automatically.")
    print()
    exit_with_pause()

if __name__ == "__main__":
    _enable_ansi()
    is_safe = "--safe" in sys.argv
    cli_args = [arg for arg in sys.argv[1:] if arg != "--safe"]

    if len(cli_args) < 2:
        print_usage()

    mode = cli_args[0].lower()

    if not os.path.exists(cli_args[1]):
        print(f"{_c('[ERROR]', RED)} File not found: {cli_args[1]}")
        sys.exit(1)

    if mode == "unpack-all":
        if len(cli_args) < 3:
            print(_c('[ERROR]', RED) + " unpack-all requires: <lang_dir> <output.toml>")
            sys.exit(1)
        out_toml = cli_args[2]
        if not os.path.splitext(out_toml)[1]:
            out_toml += ".toml"
            print(f"{_c('[INFO]', BLUE)} no extension given, writing to '{out_toml}'")
        CaffTomlConverter.unpack_all(cli_args[1], out_toml)

    elif mode == "pack-all":
        if len(cli_args) < 3:
            print(_c('[ERROR]', RED) + " pack-all requires: <combined.toml> <lang_dir> [out_dir]")
            sys.exit(1)
        CaffTomlConverter.pack_all(cli_args[1], cli_args[2], cli_args[3] if len(cli_args) > 3 else None)

    elif mode == "unpack":
        in_bin_path = cli_args[1]
        out_toml_path = (
            cli_args[2]
            if len(cli_args) > 2
            else os.path.splitext(in_bin_path)[0] + ".toml"
        )
        if not os.path.splitext(out_toml_path)[1]:
            out_toml_path += ".toml"
            print(f"{_c('[INFO]', BLUE)} no extension given, writing to '{out_toml_path}'")

        with open(in_bin_path, "rb") as f:
            original_data = f.read()

        generated_toml = CaffTomlConverter.bin_to_toml(original_data)

        is_lossless, status_msg = CaffTomlConverter.verify_lossless(
            original_data, generated_toml
        )
        if not is_lossless:
            print(f"{_c('[ERROR]', RED)} Lossless verification failed for '{in_bin_path}'!")
            print(f"        Details: {status_msg}")
            sys.exit(1)

        with open(out_toml_path, "w", encoding="utf-8") as f:
            f.write(generated_toml)

        print(
            f"[SUCCESS] Verified ({status_msg}) & Exported {in_bin_path} -> {out_toml_path}"
        )

    elif mode == "pack":
        in_toml_path = cli_args[1]

        if is_safe:
            default_bin = os.path.splitext(in_toml_path)[0] + ".bin"
            target_bin = cli_args[2] if len(cli_args) > 2 else default_bin
            out_bin_path = cli_args[3] if len(cli_args) > 3 else target_bin

            modded_bin = CaffTomlConverter.pack_safe(in_toml_path, target_bin)

            with open(out_bin_path, "wb") as f:
                f.write(modded_bin)

            print(
                f"[SUCCESS] (SAFE) Overwrote strings in standalone mode -> Saved to {out_bin_path}"
            )
        else:
            out_bin_path = (
                cli_args[2]
                if len(cli_args) > 2
                else os.path.splitext(in_toml_path)[0] + ".bin"
            )

            with open(in_toml_path, "r", encoding="utf-8") as f:
                toml_content = f.read()

            modded_bin = CaffTomlConverter.toml_to_bin(toml_content)

            with open(out_bin_path, "wb") as f:
                f.write(modded_bin)

            print(f"{_c('[SUCCESS]', GREEN)} Repacked {in_toml_path} -> {out_bin_path}")
