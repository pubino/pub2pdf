#!/usr/bin/env python3
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

def scan_share(root_path, max_workers=16, progress_interval=5, echo=True):
    # Standardize path
    root_path = os.path.abspath(root_path)
    if not os.path.exists(root_path):
        print(f"Error: Path '{root_path}' does not exist.", file=sys.stderr)
        sys.exit(1)

    print(f"Starting search in: {root_path}", file=sys.stderr)
    print("Finding .pub files...", file=sys.stderr)

    # State tracking
    dirs_to_visit = [root_path]
    directories_scanned = 0
    files_checked = 0
    matches_found = []
    
    start_time = time.time()
    last_progress_time = start_time

    def process_dir(directory):
        local_dirs = []
        local_files = []
        try:
            # os.scandir is significantly faster over SMB than os.walk
            with os.scandir(directory) as entries:
                for entry in entries:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            local_dirs.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            local_files.append(entry.path)
                    except OSError:
                        # Handle permission errors or transient SMB network drops
                        continue
        except OSError:
            pass
        return local_dirs, local_files

    # ThreadPool handles concurrent SMB directory requests over the VPN tunnel
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        while dirs_to_visit:
            # Dispatch a batch of pending directories
            current_batch = dirs_to_visit[:max_workers * 2]
            dirs_to_visit = dirs_to_visit[max_workers * 2:]

            futures = {executor.submit(process_dir, d): d for d in current_batch}
            
            for future in as_completed(futures):
                directories_scanned += 1
                subdirs, files = future.result()
                
                # Add discovered subdirectories back to the queue
                dirs_to_visit.extend(subdirs)
                
                # Process files found in this directory
                for file_path in files:
                    files_checked += 1
                    if file_path.lower().endswith('.pub'):
                        matches_found.append(file_path)
                        # Print match immediately to stdout
                        if echo:
                            print(file_path, flush=True)

                # Periodic progress output to stderr (every N seconds)
                now = time.time()
                if now - last_progress_time >= progress_interval:
                    elapsed = now - start_time
                    rate = files_checked / elapsed if elapsed > 0 else 0
                    print(
                        f"[PROGRESS] Directories scanned: {directories_scanned:,} | "
                        f"Files checked: {files_checked:,} | "
                        f"Queue size: {len(dirs_to_visit):,} | "
                        f"Matches: {len(matches_found)} | "
                        f"Speed: {rate:.1f} files/sec",
                        file=sys.stderr,
                        flush=True
                    )
                    last_progress_time = now

    elapsed_total = time.time() - start_time
    print(
        f"\n[COMPLETE] Scanned {directories_scanned:,} directories and {files_checked:,} files in {elapsed_total:.1f}s. "
        f"Found {len(matches_found)} .pub files.",
        file=sys.stderr
    )
    return matches_found

if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Usage: python3 find_pub.py /path/to/mounted/share", file=sys.stderr)
        sys.exit(1)
    
    target_dir = sys.argv[1]
    scan_share(target_dir)
