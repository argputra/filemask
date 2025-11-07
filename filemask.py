import sys
import json
import re
import argparse
import os # Import modul os untuk manipulasi path

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

def apply_masking_to_line(line: str, ranges: list[dict]) -> tuple[str, int]:
    """
    Menerapkan masking berdasarkan rentang posisi karakter ke satu baris.
    HANYA karakter non-whitespace yang akan di-masking.
    """
    chars = list(line)
    masked_count = 0

    for char_index in range(len(chars)):
        is_masked = False
        
        # Cek apakah posisi karakter masuk dalam rentang masking
        for pos in ranges:
            if char_index >= pos['start'] and char_index <= pos['end']:
                is_masked = True
                break

        # Terapkan masking jika di rentang posisi DAN bukan spasi (whitespace)
        if is_masked and not chars[char_index].isspace():
            chars[char_index] = '*'
            masked_count += 1

    return "".join(chars), masked_count

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
    
    # Argumen: config opsional, input path wajib (file|folder|wildcard)
    parser.add_argument('--config', dest='config_file', nargs='?', default=None,
                        help="Path ke file konfigurasi JSON. Jika tidak diberikan: akan dicari .json dalam folder input.")
    parser.add_argument('input_path', help="Path input: file, folder, atau wildcard (cth: ./data/*.txt atau Dat*)")
    
    # Output: untuk single file bisa pakai positional output_file; untuk multi-file pakai --outdir
    parser.add_argument('output_file', nargs='?', default=None, 
                        help="[Mode single file] Path file output. Jika kosong, default ke nama_input_masked.txt")
    parser.add_argument('--outdir', default=None,
                        help="[Mode multi-file] Folder tujuan output. Jika kosong, output di folder asal tiap file.")
    parser.add_argument('--ext', nargs='+', default=None,
                        help="Batasi ekstensi file yang diproses. Contoh: --ext .txt .sql atau txt sql")

    # Opsi progress bar di terminal
    parser.add_argument('--progress', action='store_true', help='Tampilkan progress bar saat memproses')
    parser.add_argument('--progress-style', choices=['auto','simple','rich'], default='auto',
                        help='Gaya progress bar: auto (default, pakai rich jika tersedia), simple, atau rich')

    if len(sys.argv) == 1:
        parser.print_help(sys.stderr)
        sys.exit(1)

    args = parser.parse_args()
    
    # Helper: deteksi apakah path mengandung wildcard
    def has_glob(p: str) -> bool:
        return any(ch in p for ch in ['*', '?', '['])

    # Kumpulkan daftar file input berdasarkan input_path
    input_candidates = []
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
                import time
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

        # Simple progress (ANSI teks)
        is_tty = sys.stdout.isatty()
        bar_width = 40

        def _print(current: int, total: int):
            if total <= 0:
                return
            ratio = max(0.0, min(1.0, current / total))
            filled = int(ratio * bar_width)
            bar = '[' + ('#' * filled) + ('-' * (bar_width - filled)) + ']'
            pct = f"{ratio * 100:5.1f}%"
            line = f"{label} {bar} {pct} ({current}/{total})"
            # Gunakan carriage return untuk update baris yang sama jika TTY
            if is_tty:
                sys.stdout.write('\r' + line)
                sys.stdout.flush()
            else:
                # Jika bukan TTY (misal dialihkan ke file), print ringkas
                if current == total:
                    print(line)

        def _finish():
            # Pastikan baris baru setelah progress selesai
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
    
    print("INFO: Memulai proses masking...")
    processed_files = 0
    for file_index, input_fp in enumerate(input_files, start=1):
        # 3.1 Baca file input
        try:
            with open(input_fp, 'r', encoding='utf-8') as f:
                input_content = f.read()
            masked_content_lines = input_content.splitlines()
            print(f"INFO: ({file_index}/{len(input_files)}) Memuat '{input_fp}' ({len(masked_content_lines)} baris)")
        except Exception as e:
            print(f"ERROR: Gagal membaca '{input_fp}': {e}")
            # Lanjutkan ke file berikutnya
            if files_progress_cb:
                files_progress_cb(file_index, len(input_files))
            continue

        # 3.2 Terapkan semua rule pada file ini
        file_masked_count = 0
        try:
            for i, rule in enumerate(rules):
                rule_type = rule.get('type')
                rule_label = f"{os.path.basename(input_fp)} [Rule {i+1}/{len(rules)}: {rule_type}]"
                progress_cb = None
                finish_progress = None
                if args.progress:
                    progress_cb, finish_progress = make_progress_printer(rule_label)
                
                if rule_type == 'type1':
                    print(f"INFO:   [Rule {i+1}/{len(rules)}] type1 - anchor=\"{rule.get('anchor','')}\"")
                    count = apply_type1_masking(masked_content_lines, rule, progress_cb)
                elif rule_type == 'type2':
                    print(f"INFO:   [Rule {i+1}/{len(rules)}] type2 - anchorStart=\"{rule.get('anchorStart','')}\" anchorEnd=\"{rule.get('anchorEnd','')}\"")
                    if rule.get('linesPerRecord', 1) < 1:
                        print(f"WARNING: linesPerRecord < 1 pada rule {i+1}. Diset ke 1.")
                        rule['linesPerRecord'] = 1
                    count = apply_type2_masking(masked_content_lines, rule, progress_cb)
                else:
                    print(f"WARNING: Melewati aturan {i+1} dengan tipe tidak dikenal: {rule_type}")
                    count = 0
                
                file_masked_count += count
                if finish_progress:
                    finish_progress()
                print(f"INFO:   [Rule {i+1}/{len(rules)}] selesai. Karakter dimasking: {count}")
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
                out_dir = args.outdir if args.outdir else os.path.dirname(input_fp)
                os.makedirs(out_dir, exist_ok=True)
                output_fp = os.path.join(out_dir, f"{base}_masked{ext}")

            final_masked_content = '\n'.join(masked_content_lines)
            with open(output_fp, 'w', encoding='utf-8') as f:
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

if __name__ == '__main__':
    main()