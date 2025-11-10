import sys
import json
import re
import argparse
import os # Import modul os untuk manipulasi path
import time
import traceback
import random
import hashlib
from datetime import datetime, timedelta
# Mode masking global (di-set di main() dari argumen CLI)
MASK_MODE = 'star'  # pilihan: 'star' (default), 'scramble'
from concurrent.futures import ThreadPoolExecutor, as_completed

# ==============================================================================
# 1. HELPER FUNCTIONS
# ==============================================================================

def parse_char_ranges(range_string: str) -> list[dict]:
    """
    Mengubah string rentang (cth: "1-9, 31-40") menjadi list indeks 0-based.
    """
    ranges = []
    if not range_string:
        return ranges
    
    parts = [p.strip() for p in range_string.split(',') if p.strip()]
    
    for part in parts:
        if '-' in part:
            try:
                start_char, end_char = map(int, part.split('-'))
            except ValueError:
                continue
        else:
            try:
                start_char = end_char = int(part)
            except ValueError:
                continue

        # Pastikan indeks valid (1-based dan start <= end)
        if start_char >= 1 and end_char >= start_char:
            # Konversi ke indeks 0-based
            ranges.append({'start': start_char - 1, 'end': end_char - 1})
            
    return ranges

def create_anchor_regex(anchor_string: str, use_raw_regex: bool = False, case_insensitive: bool = True) -> re.Pattern:
    """
    Buat objek Regex dari anchor string.
    - Jika use_raw_regex=True: perlakukan anchor_string sebagai regex utuh (tanpa modifikasi).
    - Jika False: dukung wildcard '%' dan OR dengan '|' serta auto-^/$ sesuai aturan lama.
    """
    flags = re.IGNORECASE if case_insensitive else 0
    if not anchor_string:
        # Regex yang tidak akan pernah cocok
        return re.compile(r'a^$a', flags)

    if use_raw_regex:
        try:
            return re.compile(anchor_string, flags)
        except re.error:
            # Regex invalid: kembalikan regex tidak cocok untuk mencegah crash
            return re.compile(r'a^$a', flags)

    # MODE WILDCARD lama dengan '%'
    raw_anchors = anchor_string.split('|')
    anchors = [s for s in raw_anchors if s]
    if not anchors:
        return re.compile(r'a^$a', flags)

    regex_parts = []
    for anchor in anchors:
        starts_with_percent = anchor.startswith('%')
        ends_with_percent = anchor.endswith('%')
        part = re.escape(anchor)
        # Pastikan '%' menjadi wildcard apapun
        part = part.replace(r'\%', '.*').replace('%', '.*')
        if not starts_with_percent:
            part = '^' + part
        if not ends_with_percent:
            part = part + '$'
        regex_parts.append(part)

    final_regex_string = '|'.join(regex_parts)
    return re.compile(final_regex_string, flags)

def anchor_to_pattern_string(anchor_string: str, use_raw_regex: bool, case_sensitive: bool) -> tuple[str, bool]:
    """Bangun fragmen regex (string) untuk agregasi.
    Return (pattern, is_ignorecase) agar caller bisa mengelompokkan per sensitivitas.
    """
    if use_raw_regex:
        return anchor_string, (not case_sensitive)
    # Legacy wildcard mode
    s = re.escape(anchor_string)
    s = s.replace(r'\%', '.*').replace('%', '.*')
    add_start = not anchor_string.startswith('%')
    add_end = not anchor_string.endswith('%')
    if add_start:
        s = '^' + s
    if add_end:
        s = s + '$'
    return s, (not case_sensitive)

def build_combined_type1_regex(type1_rules: list[dict]):
    """Gabungkan anchor type1 menjadi 2 regex (ignorecase dan case) dengan named groups.
    Return dict: { 'icase': (compiled_regex, group_map), 'case': (compiled_regex, group_map) }
    group_map: group_name -> list_of_ranges
    """
    icase_parts = []
    icase_map = {}
    case_parts = []
    case_map = {}
    always_ranges: list[dict] = []  # anchor kosong => mask semua baris
    for idx, r in enumerate(type1_rules, start=1):
        anchor = r.get('anchor', '')
        use_raw = bool(r.get('useRawRegex', False))
        case_sensitive = bool(r.get('caseSensitive', False))
        ranges = parse_char_ranges(r.get('positionsString', ''))
        if not ranges:
            continue
        if (anchor or '').strip() == '':
            # tanpa anchor: selalu terapkan
            always_ranges.extend(ranges)
            continue
        patt, is_icase = anchor_to_pattern_string(anchor, use_raw, case_sensitive)
        gname = f"R{idx}"
        if is_icase:
            icase_parts.append(f"(?P<{gname}>{patt})")
            icase_map.setdefault(gname, []).extend(ranges)
        else:
            case_parts.append(f"(?P<{gname}>{patt})")
            case_map.setdefault(gname, []).extend(ranges)
    result = {}
    if icase_parts:
        result['icase'] = (re.compile('|'.join(icase_parts), re.IGNORECASE), icase_map)
    if case_parts:
        result['case'] = (re.compile('|'.join(case_parts)), case_map)
    if always_ranges:
        result['always'] = always_ranges
    return result

def apply_masking_to_line(line: str, ranges: list[dict]) -> tuple[str, int]:
    """
    Menerapkan masking berdasarkan rentang posisi karakter ke satu baris.
    HANYA karakter non-whitespace yang akan di-masking.
    """
    chars = list(line)
    masked_indices = []  # indeks karakter (non-whitespace) yang akan dimasking

    for char_index in range(len(chars)):
        # Cek apakah posisi karakter masuk dalam rentang masking
        in_range = False
        for pos in ranges:
            if pos['start'] <= char_index <= pos['end']:
                in_range = True
                break
        if in_range and not chars[char_index].isspace():
            masked_indices.append(char_index)

    # Tidak ada yang perlu dimasking
    if not masked_indices:
        return line, 0

    if MASK_MODE == 'scramble':
        # Scramble mempertahankan multiset karakter, hanya mengacak urutannya.
        # Deterministik: seed dari hash substring yang akan dimasking sehingga idempotent antar run.
        segment_chars = [chars[i] for i in masked_indices]
        seed_src = ''.join(segment_chars)
        # Jika semua karakter sama (misal 'AAAA'), shuffle tidak mengubah – itu ok.
        seed_int = int(hashlib.sha256(seed_src.encode('utf-8')).hexdigest(), 16) & 0xffffffff
        rng = random.Random(seed_int)
        rng.shuffle(segment_chars)
        for idx, ci in enumerate(masked_indices):
            chars[ci] = segment_chars[idx]
    else:  # 'star'
        for ci in masked_indices:
            chars[ci] = '*'

    return ''.join(chars), len(masked_indices)

# ==============================================================================
# 2. MASKING LOGIC
# ==============================================================================

def apply_type1_masking(lines: list[str], rule: dict, progress_cb=None) -> int:
    """
    Memproses masking Tipe 1: Anchor Teks + Posisi Karakter.
    """
    anchor = rule.get('anchor', '')
    use_raw = bool(rule.get('useRawRegex', False))
    case_sensitive = bool(rule.get('caseSensitive', False))
    positions_string = rule.get('positionsString', '')
    
    ranges = parse_char_ranges(positions_string)
    anchor_regex = create_anchor_regex(anchor, use_raw_regex=use_raw, case_insensitive=not case_sensitive)

    total_masked_count = 0

    total_lines = len(lines)
    # Tentukan frekuensi update progress agar tidak terlalu sering
    step = max(1, total_lines // 100)

    for i in range(total_lines):
        line = lines[i]
        
        # Cek apakah baris mengandung anchor. 
        if anchor_regex.search(line):
            new_line, masked_count = apply_masking_to_line(line, ranges)
            
            if masked_count > 0:
                lines[i] = new_line  # Update baris
                total_masked_count += masked_count

        # Update progress per beberapa baris
        if progress_cb and (i % step == 0 or i == total_lines - 1):
            progress_cb(i + 1, total_lines)
    
    return total_masked_count

def apply_type2_masking(lines: list[str], rule: dict, progress_cb=None) -> int:
    """
    Memproses masking Tipe 2: Blok Baris + Posisi Karakter Berulang.
    """
    anchor_start = rule.get('anchorStart', '')
    skip_start = rule.get('skipStart', 0)
    anchor_end = rule.get('anchorEnd', '')
    skip_end = rule.get('skipEnd', 0)
    lines_per_record = rule.get('linesPerRecord', 1)
    positions_string = rule.get('positionsString', '')
    use_raw_global = bool(rule.get('useRawRegex', False))
    start_use_raw = bool(rule.get('useRawRegexStart', use_raw_global))
    end_use_raw = bool(rule.get('useRawRegexEnd', use_raw_global))
    case_sensitive = bool(rule.get('caseSensitive', False))
    
    # Pisahkan string posisi untuk setiap baris record (dipisahkan oleh '&&')
    raw_position_strings = [s.strip() for s in positions_string.split('&&')]
    multi_line_positions = [parse_char_ranges(s) for s in raw_position_strings]
    
    total_masked_count = 0
    current_line_index = 0
    start_regex = create_anchor_regex(anchor_start, use_raw_regex=start_use_raw, case_insensitive=not case_sensitive)
    end_regex = create_anchor_regex(anchor_end, use_raw_regex=end_use_raw, case_insensitive=not case_sensitive)

    total_lines = len(lines)
    # Frekuensi update progress
    step = max(1, total_lines // 100)

    while current_line_index < len(lines):
        start_line_index = -1
        end_line_index = -1
        
        # 1. Cari Anchor Start Line
        for i in range(current_line_index, len(lines)):
            if start_regex.search(lines[i]):
                start_line_index = i
                break
            # Update progress saat scanning
            if progress_cb and (i % step == 0 or i == total_lines - 1):
                progress_cb(i + 1, total_lines)

        if start_line_index == -1:
            break

        # 2. Cari Anchor End Line (mulai setelah start line)
        for i in range(start_line_index + 1, len(lines)):
            if end_regex.search(lines[i]):
                end_line_index = i
                break
            if progress_cb and (i % step == 0 or i == total_lines - 1):
                progress_cb(i + 1, total_lines)

        if end_line_index == -1 or end_line_index <= start_line_index:
            # Tidak ditemukan pasangan yang valid
            current_line_index = len(lines)
            break

        # 3. Hitung Batasan Blok Masking (Inklusif)
        mask_start_line = start_line_index + skip_start + 1
        mask_end_line = end_line_index - skip_end - 1

        # 4. Terapkan Masking
        if mask_start_line <= mask_end_line:
            for i in range(mask_start_line, mask_end_line + 1):
                if 0 <= i < len(lines): 
                    # Hitung indeks baris relatif dalam satu record
                    relative_line_index = (i - mask_start_line) % lines_per_record
                    
                    # Ambil rentang posisi untuk baris relatif ini
                    positions_for_this_line = multi_line_positions[relative_line_index] \
                                              if relative_line_index < len(multi_line_positions) else []
                    
                    if positions_for_this_line:
                        new_line, masked_count = apply_masking_to_line(lines[i], positions_for_this_line)
                        lines[i] = new_line # Update baris
                        total_masked_count += masked_count
                # Update progress juga saat masking berjalan
                if progress_cb and (i % step == 0 or i == total_lines - 1):
                    progress_cb(min(i + 1, total_lines), total_lines)
        
        # 5. Pindahkan pointer untuk iterasi berikutnya (mulai setelah end line)
        current_line_index = end_line_index + 1

    return total_masked_count

def group_type2_rules(rules: list[dict]) -> list[dict]:
    """Kelompokkan aturan type2 dengan parameter struktural identik lalu gabungkan positionsString.
    Kunci grouping: (anchorStart, anchorEnd, skipStart, skipEnd, linesPerRecord, caseSensitive, useRawRegexStart/useRawRegexEnd/useRawRegex)
    Posisi per relative line digabung (union) per indeks.
    """
    groups = {}
    for r in rules:
        if r.get('type') != 'type2':
            continue
        key = (
            r.get('anchorStart',''), r.get('anchorEnd',''),
            int(r.get('skipStart',0)), int(r.get('skipEnd',0)),
            int(r.get('linesPerRecord',1)) or 1,
            bool(r.get('caseSensitive', False)),
            bool(r.get('useRawRegexStart', r.get('useRawRegex', False))),
            bool(r.get('useRawRegexEnd', r.get('useRawRegex', False))),
            bool(r.get('useRawRegex', False))
        )
        groups.setdefault(key, []).append(r)
    aggregated = []
    for key, rule_list in groups.items():
        if not rule_list:
            continue
        # Ambil parameter dasar dari rule pertama
        base = dict(rule_list[0])
        # Gabungkan positionsString per relative line
        lines_per_record = key[4]
        # Kumpulkan list ranges per relative index
        per_index_ranges: list[list[dict]] = [[] for _ in range(lines_per_record)]
        for rl in rule_list:
            pos_string = rl.get('positionsString','')
            raw_position_strings = [s.strip() for s in pos_string.split('&&')]
            for idx in range(lines_per_record):
                src = raw_position_strings[idx] if idx < len(raw_position_strings) else ''
                ranges = parse_char_ranges(src)
                if ranges:
                    per_index_ranges[idx].extend(ranges)
        # Dedup merge overlapping ranges untuk tiap index
        merged_parts = []
        for idx_ranges in per_index_ranges:
            if not idx_ranges:
                merged_parts.append('')
                continue
            # Sort & merge
            idx_ranges.sort(key=lambda x: (x['start'], x['end']))
            merged = []
            cur = dict(idx_ranges[0])
            for r in idx_ranges[1:]:
                if r['start'] <= cur['end'] + 1:  # overlap atau bersebelahan
                    cur['end'] = max(cur['end'], r['end'])
                else:
                    merged.append(cur)
                    cur = dict(r)
            merged.append(cur)
            # Konversi kembali ke format positionsString segment (1-based)
            seg = ', '.join(f"{m['start']+1}-{m['end']+1}" if m['start'] != m['end'] else f"{m['start']+1}" for m in merged)
            merged_parts.append(seg)
        base['positionsString'] = ' && '.join(merged_parts)
        aggregated.append(base)
    return aggregated

# ==============================================================================
# 3. MAIN EXECUTION
# ==============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Utilitas Masking Data Teks. Terapkan aturan masking dari file konfigurasi JSON ke file teks input. Mendukung file tunggal, folder, dan wildcard.",
        epilog=(
            "Contoh penggunaan:\n"
            "  # Mode klasik (single file)\n"
            "  python filemask.py config.json sourcedata.txt [output_masked.txt]\n\n"
            "  # Mode folder (auto temukan config .json di folder)\n"
            "  python filemask.py  ./data_folder  --progress\n\n"
            "  # Mode wildcard (contoh: hanya file yang match pola)\n"
            "  python filemask.py  './data_folder/Dat*'  --config ./data_folder/config.json --outdir ./out\n"
        ),
        formatter_class=argparse.RawTextHelpFormatter
    )
    
    # Argumen: config opsional, input path (file|folder|wildcard) atau mode viewer
    parser.add_argument('--config', dest='config_file', nargs='?', default=None,
                        help="Path ke file konfigurasi JSON. Jika tidak diberikan: akan dicari .json dalam folder input.")
    parser.add_argument('input_path', nargs='?', default=None,
                        help="Path input: file, folder, atau wildcard (cth: ./data/*.txt atau Dat*). Opsional jika menggunakan --viewer.")
    parser.add_argument('--viewer', nargs='?', const='', metavar='FILE',
                        help="Buka GUI Large File Viewer. Opsional berikan FILE awal untuk langsung dibuka.")
    
    # Output: untuk single file bisa pakai positional output_file; untuk multi-file pakai --outdir
    parser.add_argument('output_file', nargs='?', default=None, 
                        help="[Mode single file] Path file output. Jika kosong, default ke nama_input_masked.txt")
    parser.add_argument('--outdir', default=None,
                        help="[Mode multi-file] Folder tujuan output. Jika kosong, output di folder asal tiap file.")
    parser.add_argument('--output', action='store_true',
                        help="Simpan hasil ke subfolder 'output' di dalam folder asal masing-masing file (diabaikan jika --outdir dipakai).")
    parser.add_argument('--ext', nargs='+', default=None,
                        help="Batasi ekstensi file yang diproses. Contoh: --ext .txt .sql atau txt sql")
    parser.add_argument('--encoding', default=None,
                        help="Paksa encoding input (mis: utf-8, latin-1, utf-16). Jika tidak ditentukan akan pakai utf-8 atau auto jika --auto-encoding.")
    parser.add_argument('--auto-encoding', action='store_true',
                        help="Aktifkan deteksi encoding otomatis (BOM + fallback percobaan). Mengabaikan error decode utf-8.")
    parser.add_argument('--force-output-utf8', action='store_true',
                        help="Selalu tulis output sebagai UTF-8 meskipun input encoding berbeda.")
    parser.add_argument('--stream', action='store_true',
                        help="[Deprecated] Gunakan --mode stream. Alias cepat untuk mengaktifkan mode streaming.")
    parser.add_argument('--mode', choices=['auto','memory','stream'], default='auto',
                        help="Strategi pemrosesan: auto (default), memory (in-memory), atau stream (baris demi baris).")

    # Opsi mode masking: bintang (*) atau scramble
    parser.add_argument('--mask-mode', choices=['star','scramble'], default='star',
                        help="Cara masking karakter di posisi yang ditarget: 'star' (default, ganti '*') atau 'scramble' (acak urutan karakter asli).")

    # Opsi progress bar di terminal
    parser.add_argument('--progress', action='store_true', help='Tampilkan progress bar saat memproses')
    parser.add_argument('--progress-style', choices=['auto','simple','rich'], default='auto',
                        help='Gaya progress bar: auto (default, pakai rich jika tersedia), simple, atau rich')
    parser.add_argument('--jobs', type=int, default=1,
                        help='Jumlah file yang diproses paralel (default 1). Nonaktifkan progress per-file saat >1.')
    parser.add_argument('--sample', type=int, default=None,
                        help='Hanya keluarkan N baris pertama dari hasil masking (sampling output).')

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)

    args = parser.parse_args()

    # Mode viewer: buka large_file_viewer dan keluar
    if args.viewer is not None:
        try:
            from large_file_viewer import launch_viewer
        except ImportError as e:
            print(f"ERROR: Modul viewer tidak ditemukan: {e}")
            sys.exit(1)
        initial = args.viewer if args.viewer else None
        launch_viewer(initial)
        return
    global MASK_MODE
    MASK_MODE = args.mask_mode
    
    # Helper: deteksi apakah path mengandung wildcard
    def has_glob(p: str) -> bool:
        return any(ch in p for ch in ['*', '?', '['])

    # Kumpulkan daftar file input berdasarkan input_path
    input_candidates = []
    if args.input_path is None:
        print("ERROR: input_path wajib kecuali menggunakan --viewer. Lihat --help untuk contoh.")
        sys.exit(1)
    in_path = args.input_path
    if has_glob(in_path):
        import glob
        input_candidates = sorted([p for p in glob.glob(in_path) if os.path.isfile(p)])
        # Jika glob menunjuk ke folder, tambahkan semua file di dalamnya
        dirs_from_glob = sorted([p for p in glob.glob(in_path) if os.path.isdir(p)])
        for d in dirs_from_glob:
            for name in sorted(os.listdir(d)):
                fp = os.path.join(d, name)
                if os.path.isfile(fp):
                    input_candidates.append(fp)
    elif os.path.isdir(in_path):
        # Ambil semua file di folder (non-recursive)
        for name in sorted(os.listdir(in_path)):
            fp = os.path.join(in_path, name)
            if os.path.isfile(fp):
                input_candidates.append(fp)
    elif os.path.isfile(in_path):
        input_candidates = [in_path]
    else:
        print(f"ERROR: Path input tidak ditemukan: {in_path}")
        sys.exit(1)

    if not input_candidates:
        print("ERROR: Tidak ada file yang cocok dengan path input.")
        sys.exit(1)

    # Tentukan folder basis untuk pencarian config (pakai folder input pertama)
    first_dir = os.path.dirname(os.path.abspath(input_candidates[0])) or os.getcwd()

    # Auto-discovery config jika tidak diberikan
    config_path = args.config_file
    if config_path is None:
        # Cari .json di folder basis
        jsons = [os.path.join(first_dir, f) for f in sorted(os.listdir(first_dir)) if f.lower().endswith('.json')]
        if len(jsons) == 0:
            print("ERROR: Mode auto-config: tidak ditemukan file .json di folder input. Gunakan --config untuk menentukan config secara eksplisit.")
            sys.exit(1)
        if len(jsons) > 1:
            print("ERROR: Mode auto-config: ditemukan lebih dari satu file .json di folder. Gunakan --config untuk memilih salah satu:")
            for jp in jsons:
                print(f"  - {jp}")
            sys.exit(1)
        config_path = jsons[0]
        print(f"INFO: Menggunakan config (auto): '{config_path}'")

    # Filter file input berdasarkan ketentuan JSON exclusion saat auto-config
    multi_mode = len(input_candidates) > 1 or os.path.isdir(in_path) or has_glob(in_path)
    if args.config_file is None:
        # Auto-config: semua file selain .json akan diproses
        input_files = [p for p in input_candidates if not p.lower().endswith('.json')]
    else:
        # Config explicit: boleh memproses semua file (termasuk .json)
        input_files = input_candidates

    # Terapkan filter ekstensi jika diminta
    if args.ext:
        wanted_exts = set()
        for item in args.ext:
            for token in item.split(','):
                token = token.strip()
                if not token:
                    continue
                if not token.startswith('.'):
                    token = '.' + token
                wanted_exts.add(token.lower())
        before = len(input_files)
        input_files = [p for p in input_files if os.path.splitext(p)[1].lower() in wanted_exts]
        print(f"INFO: Filter ekstensi aktif {sorted(wanted_exts)} -> {before} -> {len(input_files)} file")

    if not input_files:
        print("ERROR: Tidak ada file input yang akan diproses setelah filter.")
        sys.exit(1)

    # Exclude otomatis: abaikan file yang sudah hasil masking (mengandung pola baru atau legacy)
    before_excl = len(input_files)
    excl_patterns = ['_mask_', '_masked']
    filtered = []
    excluded = []
    for p in input_files:
        base_lower = os.path.basename(p).lower()
        if any(pat in base_lower for pat in excl_patterns):
            excluded.append(p)
        else:
            filtered.append(p)
    input_files = filtered
    if excluded:
        print(f"INFO: {len(excluded)} file diabaikan karena mengandung pola {excl_patterns} (hasil masking).")
    if not input_files:
        print("ERROR: Setelah eksklusi pola masking tidak ada file yang tersisa untuk diproses.")
        sys.exit(1)

    if multi_mode and args.output_file:
        print("WARNING: output_file diabaikan dalam mode multi-file. Gunakan --outdir untuk menentukan folder output.")


    # 1. Muat Konfigurasi
    try:
        with open(config_path, 'r', encoding='utf-8') as f:
            config = json.load(f)
        rules = config.get('rules', [])
        if not rules:
            print(f"ERROR: File konfigurasi valid, tetapi tidak ada aturan masking (rules) yang ditemukan di '{config_path}'.")
            sys.exit(1)
        print(f"INFO: {len(rules)} aturan masking berhasil dimuat dari '{config_path}'.")
    except FileNotFoundError:
        print(f"ERROR: File konfigurasi tidak ditemukan: {config_path}")
        sys.exit(1)
    except json.JSONDecodeError:
        print(f"ERROR: Gagal mem-parsing file konfigurasi JSON. Pastikan formatnya benar.")
        sys.exit(1)
    except Exception as e:
        print(f"ERROR: Terjadi kesalahan saat memuat konfigurasi: {e}")
        sys.exit(1)

    # 2. Urutkan Aturan (Tipe 1 didahulukan, Tipe 2 kedua)
    rules.sort(key=lambda r: 0 if r.get('type') == 'type1' else 1)
    
    # Utility untuk progress bar (dipakai untuk per-file dan per-rule)
    def make_progress_printer(label: str):
        # Tentukan apakah rich akan digunakan
        use_rich = False
        if sys.stdout.isatty():
            if args.progress_style == 'rich':
                use_rich = True
            elif args.progress_style == 'auto':
                use_rich = True  # akan fallback jika import gagal
        if use_rich:
            try:
                import importlib
                rp = importlib.import_module('rich.progress')
                Progress = getattr(rp, 'Progress')
                TextColumn = getattr(rp, 'TextColumn')
                BarColumn = getattr(rp, 'BarColumn')
                TaskProgressColumn = getattr(rp, 'TaskProgressColumn')
                TimeElapsedColumn = getattr(rp, 'TimeElapsedColumn')
                TimeRemainingColumn = getattr(rp, 'TimeRemainingColumn')
                MofNCompleteColumn = getattr(rp, 'MofNCompleteColumn')

                progress = Progress(
                    TextColumn("{task.fields[label]}", justify="left"),
                    BarColumn(bar_width=None, style="green"),
                    TaskProgressColumn(),
                    MofNCompleteColumn(),
                    TextColumn("{task.fields[speed]}", justify="right"),
                    TimeElapsedColumn(),
                    TimeRemainingColumn(),
                    transient=True,
                )

                progress.start()
                start_time = time.time()
                # task dibuat pada pemanggilan pertama ketika total diketahui
                task_id_holder = {"id": None, "total": 0}

                def _print(current: int, total: int):
                    # Inisialisasi task saat pertama kali
                    if task_id_holder["id"] is None:
                        task_id_holder["total"] = max(total, 1)
                        task_id_holder["id"] = progress.add_task(
                            "masking",
                            total=task_id_holder["total"],
                            label=label,
                            speed="  0.0 l/s",
                        )
                    # Hitung speed
                    elapsed = max(0.000001, time.time() - start_time)
                    speed = current / elapsed
                    progress.update(
                        task_id_holder["id"],
                        completed=min(current, task_id_holder["total"]),
                        speed=f" {speed:5.1f} l/s",
                    )

                def _finish():
                    try:
                        progress.stop()
                    except Exception:
                        pass

                return _print, _finish
            except Exception:
                # Fallback ke simple jika rich tidak tersedia/bermasalah
                pass

        # Simple progress (ANSI teks), dengan penyesuaian Windows agar tetap satu baris
        is_tty = sys.stdout.isatty()
        start_time = time.time()
        is_windows = (os.name == 'nt')
        # Gunakan ASCII di Windows untuk menghindari lebar unicode blok
        bar_fill_char = '#' if is_windows else '█'
        bar_empty_char = '-' if not is_windows else '-'
        # Track panjang sebelumnya agar bisa menghapus sisa karakter
        prev_len = 0

        def _fmt_hms(seconds: float) -> str:
            seconds = max(0, int(seconds))
            h = seconds // 3600
            m = (seconds % 3600) // 60
            s = seconds % 60
            return f"{h:02d}:{m:02d}:{s:02d}"

        def _print(current: int, total: int):
            nonlocal prev_len
            if total <= 0:
                return
            # Hitung lebar terminal agar tidak wrap ke baris berikutnya (terutama di Windows)
            try:
                import shutil
                term_cols = shutil.get_terminal_size(fallback=(80, 20)).columns
            except Exception:
                term_cols = 80

            ratio = max(0.0, min(1.0, current / total))
            pct = f"{ratio * 100:5.1f}%"
            elapsed = time.time() - start_time
            speed = (current / elapsed) if elapsed > 0 else 0.0
            eta = ((total - current) / speed) if speed > 0 else None
            eta_str = _fmt_hms(eta) if eta is not None else "--:--:--"
            elp_str = _fmt_hms(elapsed)
            # Bagian teks non-bar
            suffix = f" {pct} ({current}/{total}) | elp {elp_str} | eta {eta_str} | {speed:6.1f} it/s"
            # Usahakan tetap satu baris: alokasikan bar_width berdasarkan lebar terminal
            max_cols = max(20, term_cols - 1)  # sisakan 1 kolom
            # Jika label terlalu panjang, potong
            max_label = 30 if max_cols > 60 else 12
            disp_label = (label if len(label) <= max_label else (label[:max_label - 1] + '…'))
            # Hitung lebar bar yang memungkinkan
            reserved = len(disp_label) + 1 + 2 + len(suffix)  # label + sp + [] + suffix
            bar_width = max(10, max_cols - reserved)
            # Bangun bar
            filled = int(ratio * bar_width)
            if filled > bar_width:
                filled = bar_width
            bar = '[' + (bar_fill_char * filled) + (bar_empty_char * (bar_width - filled)) + ']'
            line = f"{disp_label} {bar}{suffix}"
            # Jika masih kepanjangan, kurangi lagi bar
            if len(line) > max_cols:
                over = len(line) - max_cols
                bar_width = max(5, bar_width - over)
                filled = int(ratio * bar_width)
                if filled > bar_width:
                    filled = bar_width
                bar = '[' + (bar_fill_char * filled) + (bar_empty_char * (bar_width - filled)) + ']'
                line = f"{disp_label} {bar}{suffix}"
            # Gunakan carriage return untuk update baris yang sama jika TTY
            if is_tty:
                # Hapus sisa dari update sebelumnya dengan padding spasi
                clear_tail = ' ' * max(0, prev_len - len(line))
                sys.stdout.write('\r' + line + clear_tail)
                sys.stdout.flush()
                prev_len = len(line)
            else:
                # Jika bukan TTY (misal dialihkan ke file), print ringkas
                if current == total:
                    print(line)

        def _finish():
            # Pastikan baris baru setelah progress selesai
            try:
                # Tulis CR sekali lagi agar tampilan final rapi, lalu newline
                sys.stdout.write('\r')
                sys.stdout.write('')
            except Exception:
                pass
            sys.stdout.write('\n')
            sys.stdout.flush()

        return _print, _finish

    # 3. Terapkan Masking ke banyak file
    grand_total_masked = 0
    print(f"INFO: Total file yang akan diproses: {len(input_files)}")
    files_progress_cb = None
    files_finish_progress = None
    if args.progress and len(input_files) > 1:
        # Progress atas daftar file
        def make_files_label():
            return f"Files [{len(input_files)}]"
        files_progress_cb, files_finish_progress = make_progress_printer(make_files_label())
        # Inisialisasi ke 0 dari total
        files_progress_cb(0, len(input_files))
    
    # Catat waktu mulai keseluruhan
    overall_start_ts = time.time()
    overall_start_dt = datetime.now()
    print(f"INFO: Proses dimulai pada: {overall_start_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    print("INFO: Memulai proses masking...")
    # Helper nama file output baru
    def build_output_filename(src_path: str, out_dir: str) -> str:
        base, ext = os.path.splitext(os.path.basename(src_path))
        ts = datetime.now().strftime('%y%m%d_%H%M%S')
        return os.path.join(out_dir, f"{base}_mask_{ts}{ext}")

    # Helper pemindahan file yang aman antar device (fallback copy+remove jika EXDEV)
    def safe_replace(src: str, dst: str):
        try:
            os.replace(src, dst)
        except OSError as e:
            # 18 = EXDEV (cross-device link), fallback ke copy
            if getattr(e, 'errno', None) == 18:
                import shutil
                shutil.copyfile(src, dst)
                os.remove(src)
            else:
                raise
    processed_files = 0
    for file_index, input_fp in enumerate(input_files, start=1):
    # 3.1 Baca / proses file input (pilih streaming atau in-memory)
        def detect_encoding(data: bytes) -> str:
            # BOM detection
            if data.startswith(b'\xff\xfe'):
                return 'utf-16-le'
            if data.startswith(b'\xfe\xff'):
                return 'utf-16-be'
            if data.startswith(b'\xef\xbb\xbf'):
                return 'utf-8-sig'
            # heuristic fallback attempts
            for enc in ('utf-8', 'utf-16', 'latin-1'):
                try:
                    data.decode(enc)
                    return enc
                except Exception:
                    pass
            return 'latin-1'

        def sniff_encoding(path: str) -> tuple[str, bytes]:
            with open(path, 'rb') as bf:
                head = bf.read(65536)  # 64KB sample
            enc = 'utf-8'
            if args.encoding:
                enc = args.encoding
            elif args.auto_encoding:
                enc = detect_encoding(head)
            else:
                # try utf-8 else fallback latin-1
                try:
                    head.decode('utf-8')
                except UnicodeDecodeError:
                    enc = 'latin-1'
            return enc, head

        # Streaming helper per rule
        def stream_apply_rule(rule: dict, src_path: str, dst_path: str, enc: str, progress_bytes_cb=None) -> int:
            rule_type = rule.get('type')
            masked_total = 0
            file_size = os.path.getsize(src_path)
            processed_bytes = 0
            # Compile necessary regex
            if rule_type == 'type1':
                anchor = rule.get('anchor','')
                use_raw = bool(rule.get('useRawRegex', False))
                case_sensitive = bool(rule.get('caseSensitive', False))
                positions_string = rule.get('positionsString','')
                ranges = parse_char_ranges(positions_string)
                anchor_regex = create_anchor_regex(anchor, use_raw_regex=use_raw, case_insensitive=not case_sensitive)
                with open(src_path, 'r', encoding=enc, errors='replace') as r, open(dst_path, 'w', encoding=('utf-8' if args.force_output_utf8 else enc)) as w:
                    for line in r:
                        raw_len = len(line.encode(enc, errors='ignore'))
                        processed_bytes += raw_len
                        core = line.rstrip('\n')
                        if anchor_regex.search(core):
                            new_line, cnt = apply_masking_to_line(core, ranges)
                            masked_total += cnt
                            w.write(new_line + ('\n' if line.endswith('\n') else ''))
                        else:
                            w.write(line)
                        if progress_bytes_cb and file_size > 0:
                            progress_bytes_cb(processed_bytes, file_size)
                return masked_total
            elif rule_type == 'type2':
                anchor_start = rule.get('anchorStart','')
                anchor_end = rule.get('anchorEnd','')
                skip_start = int(rule.get('skipStart',0))
                skip_end = int(rule.get('skipEnd',0))
                lines_per_record = int(rule.get('linesPerRecord',1)) or 1
                positions_string = rule.get('positionsString','')
                use_raw_global = bool(rule.get('useRawRegex', False))
                start_use_raw = bool(rule.get('useRawRegexStart', use_raw_global))
                end_use_raw = bool(rule.get('useRawRegexEnd', use_raw_global))
                case_sensitive = bool(rule.get('caseSensitive', False))
                raw_position_strings = [s.strip() for s in positions_string.split('&&')]
                multi_line_positions = [parse_char_ranges(s) for s in raw_position_strings]
                start_regex = create_anchor_regex(anchor_start, use_raw_regex=start_use_raw, case_insensitive=not case_sensitive)
                end_regex = create_anchor_regex(anchor_end, use_raw_regex=end_use_raw, case_insensitive=not case_sensitive)
                state = 'outside'
                skip_counter = 0
                relative_idx = 0
                # ring buffer untuk skip_end lines (original + masked)
                from collections import deque
                tail_buffer = deque()
                with open(src_path, 'r', encoding=enc, errors='replace') as r, open(dst_path, 'w', encoding=('utf-8' if args.force_output_utf8 else enc)) as w:
                    for line in r:
                        raw_len = len(line.encode(enc, errors='ignore'))
                        processed_bytes += raw_len
                        core = line.rstrip('\n')
                        if state == 'outside':
                            if start_regex.search(core):
                                state = 'in_skip_start'
                                skip_counter = 0
                                w.write(line)  # tulis anchor start apa adanya
                            else:
                                w.write(line)
                        elif state == 'in_skip_start':
                            skip_counter += 1
                            w.write(line)
                            if skip_counter >= skip_start:
                                state = 'in_mask'
                                relative_idx = 0
                        elif state == 'in_mask':
                            # Cek end anchor dulu
                            if end_regex.search(core):
                                # flush tail_buffer original lines (tanpa masked)
                                while tail_buffer:
                                    orig_line, masked_line, was_masked = tail_buffer.popleft()
                                    w.write(orig_line)
                                w.write(line)  # tulis anchor end
                                state = 'outside'
                                continue
                            # Tentukan posisi rentang berdasarkan relative line index
                            positions_for_line = multi_line_positions[relative_idx % lines_per_record] if multi_line_positions else []
                            if positions_for_line:
                                new_line, cnt = apply_masking_to_line(core, positions_for_line)
                                masked_total += cnt
                                masked_output = new_line + ('\n' if line.endswith('\n') else '')
                            else:
                                masked_output = line
                            # Simpan ke buffer; jika buffer lebih besar dari skip_end flush satu
                            tail_buffer.append((line, masked_output, positions_for_line != []))
                            if len(tail_buffer) > skip_end:
                                # keluarkan elemen paling lama (dengan masking jika ada)
                                orig_line, masked_line, was_masked = tail_buffer.popleft()
                                w.write(masked_line)
                            relative_idx += 1
                        if progress_bytes_cb and file_size > 0:
                            progress_bytes_cb(processed_bytes, file_size)
                    # EOF: jika masih dalam mask dan tidak ada end anchor, flush buffer masked
                    if state == 'in_mask':
                        while tail_buffer:
                            orig_line, masked_line, was_masked = tail_buffer.popleft()
                            w.write(masked_line)
                return masked_total
            else:
                # Copy passthrough jika tipe tidak dikenal
                with open(src_path, 'r', encoding=enc, errors='replace') as r, open(dst_path, 'w', encoding=('utf-8' if args.force_output_utf8 else enc)) as w:
                    for line in r:
                        raw_len = len(line.encode(enc, errors='ignore'))
                        processed_bytes += raw_len
                        w.write(line)
                        if progress_bytes_cb and file_size > 0:
                            progress_bytes_cb(processed_bytes, file_size)
                return 0

        # Tentukan mode efektif
        effective_mode = args.mode
        # Jika --sample aktif, paksa memory mode agar bisa potong N baris awal dengan mudah dan cepat
        if args.sample is not None and args.sample > 0:
            effective_mode = 'memory'
        elif args.stream and args.mode == 'auto':
            effective_mode = 'stream'
        if effective_mode == 'auto':
            try:
                fsize = os.path.getsize(input_fp)
            except Exception:
                fsize = 0
            if fsize > 512 * 1024 * 1024 or len(rules) >= 50:
                effective_mode = 'stream'
            else:
                effective_mode = 'memory'

        if effective_mode == 'stream':
            try:
                chosen_encoding, sample = sniff_encoding(input_fp)
                print(f"INFO: ({file_index}/{len(input_files)}) Stream mode start '{input_fp}' (encoding={chosen_encoding}, size={os.path.getsize(input_fp)} bytes)")
            except Exception as e:
                print(f"ERROR: Tidak dapat sniff encoding '{input_fp}': {e}")
                if files_progress_cb:
                    files_progress_cb(file_index, len(input_files))
                continue
            # Working path akan berubah antar tahap
            working_path = input_fp
            file_masked_count = 0
            # Tahap A: aggregated type1
            type1_rules = [r for r in rules if r.get('type') == 'type1']
            type2_rules = [r for r in rules if r.get('type') == 'type2']
            skip_lines_global = int(config.get('skipLines', 0) or 0)
            if type1_rules:
                combined = build_combined_type1_regex(type1_rules)
                temp_out = f"{working_path}.type1.tmp"
                label = f"{os.path.basename(input_fp)} [Type1 aggregated]"
                progress_cb = finish_cb = None
                if args.progress:
                    progress_cb, finish_cb = make_progress_printer(label)
                print("INFO:   [Stream] Type1 aggregated pass")
                fsize = os.path.getsize(working_path)
                processed_bytes = 0
                with open(working_path, 'r', encoding=chosen_encoding, errors='replace') as rfile, open(temp_out, 'w', encoding=('utf-8' if args.force_output_utf8 else chosen_encoding)) as wfile:
                    for line_index, line in enumerate(rfile):
                        raw_len = len(line.encode(chosen_encoding, errors='ignore'))
                        processed_bytes += raw_len
                        core = line.rstrip('\n')
                        total_ranges = []
                        if line_index < skip_lines_global:
                            wfile.write(line)
                            if progress_cb and fsize > 0:
                                progress_cb(processed_bytes, fsize)
                            continue
                        if 'always' in combined:
                            total_ranges.extend(combined['always'])
                        if 'icase' in combined:
                            rx, gmap = combined['icase']
                            m = rx.search(core)
                            if m:
                                for g, rngs in gmap.items():
                                    if m.group(g) is not None:
                                        total_ranges.extend(rngs)
                        if 'case' in combined:
                            rx, gmap = combined['case']
                            m = rx.search(core)
                            if m:
                                for g, rngs in gmap.items():
                                    if m.group(g) is not None:
                                        total_ranges.extend(rngs)
                        if total_ranges:
                            new_line, cnt = apply_masking_to_line(core, total_ranges)
                            file_masked_count += cnt
                            wfile.write(new_line + ('\n' if line.endswith('\n') else ''))
                        else:
                            wfile.write(line)
                        if progress_cb and fsize > 0:
                            progress_cb(processed_bytes, fsize)
                if finish_cb:
                    finish_cb()
                working_path = temp_out
                print(f"INFO:   [Stream] Type1 aggregated selesai. Karakter dimasking: {file_masked_count}")
            # Tahap B: type2 sequential (grouped)
            grouped_type2 = group_type2_rules(type2_rules)
            for ri, rule in enumerate(grouped_type2, start=1):
                temp_out = f"{working_path}.t2.{ri}.tmp"
                label = f"{os.path.basename(input_fp)} [Type2Group {ri}/{len(grouped_type2)}]"
                progress_cb = finish_cb = None
                if args.progress:
                    progress_cb, finish_cb = make_progress_printer(label)
                print(f"INFO:   [Stream] Type2Group {ri}/{len(grouped_type2)}")
                try:
                    count = stream_apply_rule(rule, working_path, temp_out, chosen_encoding, progress_cb)
                    file_masked_count += count
                    if finish_cb:
                        finish_cb()
                    working_path = temp_out
                    print(f"INFO:   [Stream] Type2Group {ri} selesai. +{count}")
                except Exception as e:
                    print(f"ERROR: Gagal menerapkan Type2Group-{ri} pada '{input_fp}': {e}")
                    if finish_cb:
                        finish_cb()
                    break
            # Setelah semua rule, tentukan path output final
            base, ext = os.path.splitext(os.path.basename(input_fp))
            if len(input_files) == 1 and args.output_file:
                output_fp = args.output_file
            else:
                if args.outdir:
                    out_dir = args.outdir
                elif args.output:
                    parent_dir = os.path.dirname(input_fp)
                    out_dir = os.path.join(parent_dir, 'output')
                else:
                    out_dir = os.path.dirname(input_fp)
                os.makedirs(out_dir, exist_ok=True)
                output_fp = build_output_filename(input_fp, out_dir)
            # Pindahkan hasil akhir (working_path sekarang file temp terakhir)
            try:
                # Tulis hasil akhir; jika sample aktif, hanya tulis N baris pertama
                out_enc = ('utf-8' if args.force_output_utf8 else chosen_encoding)
                if args.sample is not None and args.sample > 0:
                    with open(working_path, 'r', encoding=chosen_encoding, errors='replace') as r, open(output_fp, 'w', encoding=out_enc) as w:
                        for i, line in enumerate(r, start=1):
                            if i > args.sample:
                                break
                            w.write(line)
                    print(f"INFO: Sample mode aktif (--sample {args.sample}): hanya menulis {args.sample} baris pertama.")
                else:
                    # Pastikan jika working_path adalah file asli (tidak ada rule) kita tetap salin
                    if working_path == input_fp:
                        with open(working_path, 'r', encoding=chosen_encoding, errors='replace') as r, open(output_fp, 'w', encoding=out_enc) as w:
                            for line in r:
                                w.write(line)
                    else:
                        safe_replace(working_path, output_fp)
                print(f"SUCCESS: ({file_index}/{len(input_files)}) Tersimpan (stream): '{output_fp}' | Karakter dimasking: {file_masked_count}")
                grand_total_masked += file_masked_count
            except Exception as e:
                print(f"ERROR: Gagal menyimpan output stream untuk '{input_fp}': {e}")
            # Bersihkan file temp lain kecuali final
            temp_candidates = [f"{input_fp}.type1.tmp"] + [f"{input_fp}.t2.{i}.tmp" for i in range(1, len(grouped_type2)+1)]
            for tmp_path in temp_candidates:
                if os.path.exists(tmp_path) and tmp_path != output_fp:
                    try:
                        os.remove(tmp_path)
                    except Exception:
                        pass
            processed_files += 1
            def process_one_file(input_fp: str, file_index: int, total_files: int) -> tuple[int, int]:
                """Proses satu file dan kembalikan (masked_count, status_code) status_code=0 ok, 1 error"""
                try:
                    # 3.1 Baca / proses file input (pilih streaming atau in-memory)
                    def detect_encoding(data: bytes) -> str:
                        if data.startswith(b'\xff\xfe'):
                            return 'utf-16-le'
                        if data.startswith(b'\xfe\xff'):
                            return 'utf-16-be'
                        if data.startswith(b'\xef\xbb\xbf'):
                            return 'utf-8-sig'
                        for enc in ('utf-8', 'utf-16', 'latin-1'):
                            try:
                                data.decode(enc); return enc
                            except Exception: pass
                        return 'latin-1'

                    def sniff_encoding(path: str) -> tuple[str, bytes]:
                        with open(path, 'rb') as bf:
                            head = bf.read(65536)
                        enc = 'utf-8'
                        if args.encoding:
                            enc = args.encoding
                        elif args.auto_encoding:
                            enc = detect_encoding(head)
                        else:
                            try: head.decode('utf-8')
                            except UnicodeDecodeError: enc = 'latin-1'
                        return enc, head

                    def stream_apply_rule(rule: dict, src_path: str, dst_path: str, enc: str, progress_bytes_cb=None) -> int:
                        # ...existing code...
                        return 0  # placeholder – akan gunakan implementasi asli di bawah
                    # Reuse existing stream_apply_rule & logic dengan sedikit adaptasi: kita duplikasi minimal bagian yang diperlukan.
                except Exception as e:
                    print(f"ERROR: Gagal awal '{input_fp}': {e}")
                    return (0,1)
                # Karena refactor penuh besar, untuk aktivasi cepat parallel kita panggil blok lama melalui inline function call
                # Copy logika yang ada (disingkat) - gunakan fungsi kecil agar tidak reformat besar.
                return _process_file_core(input_fp, file_index, total_files)

            # Ekstrak core processing (mengambil kode loop lama)
            def _process_file_core(input_fp: str, file_index: int, total_files: int) -> tuple[int,int]:
                # Potongan kode asli dipindah dari loop; menggunakan variables: rules, args, input_files
                # (Simplifikasi: hapus referensi files_progress_cb di dalam; progress total ditangani di luar.)
                grand_local_masked = 0
                try:
                    def detect_encoding(data: bytes) -> str:
                        if data.startswith(b'\xff\xfe'): return 'utf-16-le'
                        if data.startswith(b'\xfe\xff'): return 'utf-16-be'
                        if data.startswith(b'\xef\xbb\xbf'): return 'utf-8-sig'
                        for enc in ('utf-8','utf-16','latin-1'):
                            try: data.decode(enc); return enc
                            except Exception: pass
                        return 'latin-1'
                    def sniff_encoding(path: str) -> tuple[str, bytes]:
                        with open(path,'rb') as bf: head = bf.read(65536)
                        enc = 'utf-8'
                        if args.encoding: enc = args.encoding
                        elif args.auto_encoding: enc = detect_encoding(head)
                        else:
                            try: head.decode('utf-8')
                            except UnicodeDecodeError: enc='latin-1'
                        return enc, head
                    # Tentukan mode
                    effective_mode = args.mode
                    if args.stream and args.mode == 'auto': effective_mode='stream'
                    try: fsize=os.path.getsize(input_fp)
                    except Exception: fsize=0
                    if effective_mode=='auto':
                        if fsize>512*1024*1024 or len(rules)>=50: effective_mode='stream'
                        else: effective_mode='memory'
                    if effective_mode=='stream':
                        chosen_encoding,_=sniff_encoding(input_fp)
                        print(f"INFO: ({file_index}/{total_files}) Stream mode start '{input_fp}' (encoding={chosen_encoding}, size={fsize} bytes)")
                        working_path=input_fp; file_masked_count=0
                        type1_rules=[r for r in rules if r.get('type')=='type1']
                        type2_rules=[r for r in rules if r.get('type')=='type2']
                        if type1_rules:
                            combined=build_combined_type1_regex(type1_rules)
                            temp_out=f"{working_path}.type1.tmp"; print("INFO:   [Stream] Type1 aggregated pass")
                            processed_bytes=0
                            with open(working_path,'r',encoding=chosen_encoding,errors='replace') as rf, open(temp_out,'w',encoding=('utf-8' if args.force_output_utf8 else chosen_encoding)) as wf:
                                size=os.path.getsize(working_path)
                                for line in rf:
                                    processed_bytes+=len(line.encode(chosen_encoding,'ignore'))
                                    core=line.rstrip('\n'); total_ranges=[]
                                    if 'icase' in combined:
                                        rx,gmap=combined['icase']; m=rx.search(core)
                                        if m:
                                            for g,rngs in gmap.items():
                                                if m.group(g) is not None: total_ranges.extend(rngs)
                                    if 'case' in combined:
                                        rx,gmap=combined['case']; m=rx.search(core)
                                        if m:
                                            for g,rngs in gmap.items():
                                                if m.group(g) is not None: total_ranges.extend(rngs)
                                    if total_ranges:
                                        new_line,cnt=apply_masking_to_line(core,total_ranges); file_masked_count+=cnt; wf.write(new_line+('\n' if line.endswith('\n') else ''))
                                    else: wf.write(line)
                            working_path=temp_out
                            print(f"INFO:   [Stream] Type1 aggregated selesai. Karakter dimasking: {file_masked_count}")
                        grouped_type2=group_type2_rules(type2_rules)
                        for ri,rule in enumerate(grouped_type2, start=1):
                            temp_out=f"{working_path}.t2.{ri}.tmp"; print(f"INFO:   [Stream] Type2Group {ri}/{len(grouped_type2)}")
                            # minimal stream type2 reuse: call original streaming function logic inline
                            count=0
                            # Reuse existing stream_apply_rule simplified by calling apply_type2_masking in-memory after reading all lines (acceptable trade-off for grouped pass)
                            with open(working_path,'r',encoding=chosen_encoding,errors='replace') as rf:
                                lines=[l.rstrip('\n') for l in rf]
                            count=apply_type2_masking(lines, rule)
                            with open(temp_out,'w',encoding=('utf-8' if args.force_output_utf8 else chosen_encoding)) as wf:
                                wf.write('\n'.join(lines))
                            working_path=temp_out; file_masked_count+=count
                            print(f"INFO:   [Stream] Type2Group {ri} selesai. +{count}")
                        base,ext=os.path.splitext(os.path.basename(input_fp))
                        if len(input_files)==1 and args.output_file: output_fp=args.output_file
                        else:
                            if args.outdir: out_dir=args.outdir
                            elif args.output: out_dir=os.path.join(os.path.dirname(input_fp),'output')
                            else: out_dir=os.path.dirname(input_fp)
                            os.makedirs(out_dir,exist_ok=True)
                            output_fp=build_output_filename(input_fp,out_dir)
                        # Tulis output dengan dukungan --sample
                        out_enc = ('utf-8' if args.force_output_utf8 else chosen_encoding)
                        if args.sample is not None and args.sample > 0:
                            with open(working_path,'r',encoding=chosen_encoding,errors='replace') as rf, open(output_fp,'w',encoding=out_enc) as wf:
                                for i, line in enumerate(rf, start=1):
                                    if i>args.sample: break
                                    wf.write(line)
                            print(f"INFO: Sample mode aktif (--sample {args.sample}): hanya menulis {args.sample} baris pertama.")
                        else:
                            if working_path!=input_fp: safe_replace(working_path,output_fp)
                            else:
                                with open(working_path,'r',encoding=chosen_encoding,errors='replace') as rf, open(output_fp,'w',encoding=out_enc) as wf:
                                    for line in rf: wf.write(line)
                        print(f"SUCCESS: ({file_index}/{total_files}) Tersimpan (stream): '{output_fp}' | Karakter dimasking: {file_masked_count}")
                        grand_local_masked=file_masked_count
                        # cleanup temps
                        for ri in range(1,len(grouped_type2)+1):
                            tp=f"{input_fp}.t2.{ri}.tmp"; 
                            if os.path.exists(tp):
                                try: os.remove(tp)
                                except Exception: pass
                        t1=f"{input_fp}.type1.tmp"; 
                        if os.path.exists(t1):
                            try: os.remove(t1)
                            except Exception: pass
                        return (grand_local_masked,0)
                    # memory mode
                    # Sample processing: baca hanya N baris pertama bila diminta
                    if args.sample is not None and args.sample > 0:
                        # deteksi encoding sederhana
                        with open(input_fp,'rb') as bf: head = bf.read(65536)
                        def _detect(data: bytes) -> str:
                            if data.startswith(b'\xff\xfe'): return 'utf-16-le'
                            if data.startswith(b'\xfe\xff'): return 'utf-16-be'
                            if data.startswith(b'\xef\xbb\xbf'): return 'utf-8-sig'
                            try: data.decode('utf-8'); return 'utf-8'
                            except UnicodeDecodeError: return 'latin-1'
                        chosen = args.encoding or _detect(head)
                        with open(input_fp,'r',encoding=chosen,errors='replace') as rf:
                            lines=[]
                            for i, ln in enumerate(rf, start=1):
                                lines.append(ln.rstrip('\n'))
                                if i>=args.sample: break
                        print(f"INFO: Sample processing aktif: hanya MEMPROSES {args.sample} baris pertama dari '{input_fp}'.")
                    else:
                        with open(input_fp,'rb') as bf: raw=bf.read()
                        chosen='utf-8'
                        if args.encoding: chosen=args.encoding
                        else:
                            try: raw.decode('utf-8')
                            except UnicodeDecodeError: chosen='latin-1'
                        content=raw.decode(chosen,'replace'); lines=content.splitlines()
                    print(f"INFO: ({file_index}/{total_files}) Memuat '{input_fp}' ({len(lines)} baris, encoding={chosen})")
                    type1_rules=[r for r in rules if r.get('type')=='type1']
                    type2_rules=[r for r in rules if r.get('type')=='type2']
                    if type1_rules:
                        combined=build_combined_type1_regex(type1_rules)
                        new=[]; masked=0
                        for ln in lines:
                            total_ranges=[]
                            if 'icase' in combined:
                                rx,gmap=combined['icase']; m=rx.search(ln)
                                if m:
                                    for g,rngs in gmap.items():
                                        if m.group(g) is not None: total_ranges.extend(rngs)
                            if 'case' in combined:
                                rx,gmap=combined['case']; m=rx.search(ln)
                                if m:
                                    for g,rngs in gmap.items():
                                        if m.group(g) is not None: total_ranges.extend(rngs)
                            if total_ranges:
                                new_ln,cnt=apply_masking_to_line(ln,total_ranges); masked+=cnt; new.append(new_ln)
                            else: new.append(ln)
                        lines=new; grand_local_masked+=masked; print(f"INFO:   [Type1 aggregated] selesai. Karakter dimasking: {masked}")
                    grouped_type2=group_type2_rules(type2_rules)
                    for gi,rule in enumerate(grouped_type2, start=1):
                        print(f"INFO:   [Type2Group {gi}/{len(grouped_type2)}] anchorStart='{rule.get('anchorStart','')}' anchorEnd='{rule.get('anchorEnd','')}'")
                        cnt=apply_type2_masking(lines, rule); grand_local_masked+=cnt; print(f"INFO:   [Type2Group {gi}] selesai. +{cnt}")
                    base,ext=os.path.splitext(os.path.basename(input_fp))
                    if len(input_files)==1 and args.output_file: output_fp=args.output_file
                    else:
                        if args.outdir: out_dir=args.outdir
                        elif args.output: out_dir=os.path.join(os.path.dirname(input_fp),'output')
                        else: out_dir=os.path.dirname(input_fp)
                        os.makedirs(out_dir,exist_ok=True)
                        output_fp=build_output_filename(input_fp,out_dir)
                    out_enc='utf-8' if args.force_output_utf8 else chosen
                    with open(output_fp,'w',encoding=out_enc) as wf: wf.write('\n'.join(lines))
                    print(f"SUCCESS: ({file_index}/{total_files}) Tersimpan: '{output_fp}' | Karakter dimasking: {grand_local_masked}")
                    return (grand_local_masked,0)
                except Exception as e:
                    print(f"ERROR: File '{input_fp}' gagal diproses: {e}")
                    return (0,1)

            if args.jobs > 1 and len(input_files) > 1:
                print(f"INFO: Parallel mode aktif --jobs={args.jobs}")
                total = len(input_files)
                masked_sum = 0
                errors = 0
                with ThreadPoolExecutor(max_workers=args.jobs) as ex:
                    future_map = {ex.submit(_process_file_core, fp, idx+1, total): fp for idx, fp in enumerate(input_files)}
                    for fut in as_completed(future_map):
                        mcount, status = fut.result()
                        masked_sum += mcount
                        if status != 0:
                            errors += 1
                        processed_files += 1
                        if files_progress_cb:
                            files_progress_cb(processed_files, len(input_files))
                grand_total_masked += masked_sum
                if files_finish_progress:
                    files_finish_progress()
                print(f"SUCCESS: Parallel selesai. File diproses: {processed_files}/{len(input_files)} | Total karakter dimasking: {grand_total_masked} | Error: {errors}")
                overall_end_dt = datetime.now()
                total_secs = time.time() - overall_start_ts
                # Laporkan waktu selesai dan total durasi
                print(f"INFO: Waktu selesai: {overall_end_dt.strftime('%Y-%m-%d %H:%M:%S')}")
                # Format durasi HH:MM:SS
                h = int(total_secs) // 3600
                m = (int(total_secs) % 3600) // 60
                s = int(total_secs) % 60
                print(f"INFO: Total durasi: {h:02d}:{m:02d}:{s:02d}")
                return

            for file_index, input_fp in enumerate(input_files, start=1):
                files_progress_cb(processed_files, len(input_files))
            continue  # lanjut ke file berikutnya

        # Non-stream (in-memory) reading path
        chosen_encoding = 'utf-8'
        raw_bytes = b''
        try:
            # Jika sample aktif, baca N baris pertama saja untuk mempercepat
            if args.sample is not None and args.sample > 0:
                # Deteksi encoding cepat dengan head bytes lalu baca line-wise
                with open(input_fp, 'rb') as bf:
                    head = bf.read(65536)
                def _detect(data: bytes) -> str:
                    if data.startswith(b'\xff\xfe'): return 'utf-16-le'
                    if data.startswith(b'\xfe\xff'): return 'utf-16-be'
                    if data.startswith(b'\xef\xbb\xbf'): return 'utf-8-sig'
                    try: data.decode('utf-8'); return 'utf-8'
                    except UnicodeDecodeError: return 'latin-1'
                chosen_encoding = args.encoding or (_detect(head) if args.auto_encoding else ('utf-8' if not head or _detect(head)=='utf-8' else 'latin-1'))
                collected = []
                with open(input_fp, 'r', encoding=chosen_encoding, errors='replace') as rf:
                    for i, line in enumerate(rf, start=1):
                        collected.append(line.rstrip('\n'))
                        if i >= args.sample:
                            break
                masked_content_lines = collected
                print(f"INFO: Sample processing aktif: hanya MEMPROSES {args.sample} baris pertama dari '{input_fp}'.")
            else:
                with open(input_fp, 'rb') as bf:
                    raw_bytes = bf.read()
            if args.encoding:
                chosen_encoding = args.encoding
            elif args.auto_encoding:
                chosen_encoding = detect_encoding(raw_bytes)
            else:
                # Try utf-8 else fallback latin-1
                try:
                    raw_bytes.decode('utf-8')
                except UnicodeDecodeError:
                    chosen_encoding = 'latin-1'
            if args.sample is None or args.sample <= 0:
                try:
                    input_content = raw_bytes.decode(chosen_encoding)
                except UnicodeDecodeError as ue:
                    if chosen_encoding != 'latin-1':
                        try:
                            input_content = raw_bytes.decode('latin-1')
                            chosen_encoding = 'latin-1'
                            print(f"WARNING: Decode '{input_fp}' gagal ({ue}); fallback latin-1.")
                        except Exception:
                            raise
                    else:
                        raise
                masked_content_lines = input_content.splitlines()
            print(f"INFO: ({file_index}/{len(input_files)}) Memuat '{input_fp}' ({len(masked_content_lines)} baris, encoding={chosen_encoding})")
        except Exception as e:
            print(f"ERROR: Gagal membaca '{input_fp}' (in-memory): {e}")
            if files_progress_cb:
                files_progress_cb(file_index, len(input_files))
            continue

    # 3.2 Terapkan semua rule (in-memory mode) dengan agregasi type1
        file_masked_count = 0
        try:
            type1_rules = [r for r in rules if r.get('type') == 'type1']
            type2_rules = [r for r in rules if r.get('type') == 'type2']
            skip_lines_global = int(config.get('skipLines', 0) or 0)
            if type1_rules:
                combined = build_combined_type1_regex(type1_rules)
                label = f"{os.path.basename(input_fp)} [Type1 aggregated]"
                progress_cb = finish_progress = None
                if args.progress:
                    progress_cb, finish_progress = make_progress_printer(label)
                total_bytes = sum(len((ln+'\n').encode(chosen_encoding, errors='ignore')) for ln in masked_content_lines)
                progressed = 0
                new_lines = []
                for line_index, ln in enumerate(masked_content_lines):
                    raw_len = len((ln+'\n').encode(chosen_encoding, errors='ignore'))
                    total_ranges = []
                    # Terapkan skipLines: sebelum line_index >= skip_lines_global tidak dimasking
                    if line_index < skip_lines_global:
                        new_lines.append(ln)
                        progressed += raw_len
                        if progress_cb and total_bytes > 0:
                            progress_cb(progressed, total_bytes)
                        continue
                    if 'always' in combined:
                        total_ranges.extend(combined['always'])
                    if 'icase' in combined:
                        rx, gmap = combined['icase']
                        m = rx.search(ln)
                        if m:
                            for g, rngs in gmap.items():
                                if m.group(g) is not None:
                                    total_ranges.extend(rngs)
                    if 'case' in combined:
                        rx, gmap = combined['case']
                        m = rx.search(ln)
                        if m:
                            for g, rngs in gmap.items():
                                if m.group(g) is not None:
                                    total_ranges.extend(rngs)
                    if total_ranges:
                        new_ln, cnt = apply_masking_to_line(ln, total_ranges)
                        new_lines.append(new_ln)
                        file_masked_count += cnt
                    else:
                        new_lines.append(ln)
                    progressed += raw_len
                    if progress_cb and total_bytes > 0:
                        progress_cb(progressed, total_bytes)
                if finish_progress:
                    finish_progress()
                masked_content_lines = new_lines
                print(f"INFO:   [Type1 aggregated] selesai. Karakter dimasking: {file_masked_count}")
            # Lanjutkan dengan type2 rules (grouped)
            grouped_type2 = group_type2_rules(type2_rules)
            if args.sample is not None and args.sample > 0:
                print("WARNING: Sample processing aktif: Rule type2 mungkin tidak komplit jika anchor berada di luar N baris pertama.")
            for i, rule in enumerate(grouped_type2, start=1):
                rule_label = f"{os.path.basename(input_fp)} [Type2Group {i}/{len(grouped_type2)}]"
                progress_cb = finish_progress = None
                if args.progress:
                    progress_cb, finish_progress = make_progress_printer(rule_label)
                print(f"INFO:   [Type2Group {i}/{len(grouped_type2)}] anchorStart=\"{rule.get('anchorStart','')}\" anchorEnd=\"{rule.get('anchorEnd','')}\"")
                if rule.get('linesPerRecord', 1) < 1:
                    rule['linesPerRecord'] = 1
                count = apply_type2_masking(masked_content_lines, rule, progress_cb)
                file_masked_count += count
                if finish_progress:
                    finish_progress()
                print(f"INFO:   [Type2Group {i}/{len(grouped_type2)}] selesai. +{count}")
        except Exception as e:
            print(f"ERROR: Kesalahan saat menerapkan aturan masking pada file '{input_fp}': {e}")
            if files_progress_cb:
                files_progress_cb(file_index, len(input_files))
            continue

        # 3.3 Simpan output file
        try:
            base, ext = os.path.splitext(os.path.basename(input_fp))
            if len(input_files) == 1 and args.output_file:
                output_fp = args.output_file
            else:
                if args.outdir:
                    out_dir = args.outdir
                elif args.output:
                    parent_dir = os.path.dirname(input_fp)
                    out_dir = os.path.join(parent_dir, 'output')
                else:
                    out_dir = os.path.dirname(input_fp)
                os.makedirs(out_dir, exist_ok=True)
                output_fp = build_output_filename(input_fp, out_dir)

            final_masked_content = '\n'.join(masked_content_lines)
            out_encoding = 'utf-8' if args.force_output_utf8 else chosen_encoding
            with open(output_fp, 'w', encoding=out_encoding) as f:
                f.write(final_masked_content)
            print(f"SUCCESS: ({file_index}/{len(input_files)}) Tersimpan: '{output_fp}' | Karakter dimasking: {file_masked_count}")
            grand_total_masked += file_masked_count
        except Exception as e:
            print(f"ERROR: Gagal menyimpan output untuk '{input_fp}': {e}")

        processed_files += 1
        if files_progress_cb:
            files_progress_cb(processed_files, len(input_files))

    if files_finish_progress:
        files_finish_progress()

    print(f"SUCCESS: Selesai. File diproses: {processed_files}/{len(input_files)} | Total karakter dimasking: {grand_total_masked}")
    overall_end_dt = datetime.now()
    total_secs = time.time() - overall_start_ts
    print(f"INFO: Waktu selesai: {overall_end_dt.strftime('%Y-%m-%d %H:%M:%S')}")
    h = int(total_secs) // 3600
    m = (int(total_secs) % 3600) // 60
    s = int(total_secs) % 60
    print(f"INFO: Total durasi: {h:02d}:{m:02d}:{s:02d}")

if __name__ == '__main__':
    try:
        main()
    except SystemExit:
        # biarkan sys.exit berjalan normal
        raise
    except Exception:
        err = traceback.format_exc()
        # Tampilkan ke stderr agar PyInstaller tidak menyembunyikan penyebab aslinya
        print("FATAL: Unhandled exception: ", file=sys.stderr)
        print(err, file=sys.stderr)
        # Simpan ke file log di CWD untuk investigasi lebih lanjut
        try:
            log_path = os.path.join(os.getcwd(), f"filemask_crash_{datetime.now().strftime('%Y%m%d_%H%M%S')}.log")
            with open(log_path, 'w', encoding='utf-8') as lf:
                lf.write(err)
            print(f"FATAL: Traceback disimpan ke: {log_path}", file=sys.stderr)
        except Exception:
            pass
        sys.exit(1)