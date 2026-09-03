import os
import sys
import time
import json
import requests
from pathlib import Path

CHUNK_SIZE = 1024 * 1024 # 1 MB chunks to avoid network adapter TLS MAC corruption

def get_wheel_url(package_name: str, version: str = None) -> tuple[str, str]:
    headers = {"User-Agent": "Mozilla/5.0"}
    r = requests.get(f"https://pypi.org/pypi/{package_name}/json", headers=headers, timeout=10)
    data = r.json()
    
    files = data["urls"] if not version else data["releases"].get(version, [])
    
    # Priority matching for cp312 and manylinux_x86_64, or py3-none-any
    chosen_url = None
    chosen_filename = None
    
    # Try manylinux cp312 first (explicitly exclude macosx and win)
    for f in files:
        fname = f["filename"]
        if fname.endswith(".whl") and "cp312" in fname and "manylinux" in fname and "x86_64" in fname:
            chosen_url = f["url"]
            chosen_filename = fname
            break
            
    if not chosen_url:
        for f in files:
            fname = f["filename"]
            if fname.endswith(".whl") and "py3-none-any" in fname:
                chosen_url = f["url"]
                chosen_filename = fname
                break
                
    if not chosen_url:
        # Fallback to any .whl
        for f in files:
            if f["filename"].endswith(".whl"):
                chosen_url = f["url"]
                chosen_filename = f["filename"]
                break

    if not chosen_url:
        raise ValueError(f"No suitable wheel found for {package_name}")

    return chosen_url, chosen_filename

def download_file_chunked(url: str, dest_path: Path) -> Path:
    headers = {"User-Agent": "Mozilla/5.0"}
    
    # Get total file size
    r_head = requests.head(url, headers=headers, timeout=10, allow_redirects=True)
    total_size = int(r_head.headers.get("content-length", 0))
    print(f"Downloading {dest_path.name} ({total_size / (1024*1024):.2f} MB) in 1MB chunks...")

    # Check if already fully downloaded
    if dest_path.exists() and dest_path.stat().st_size == total_size:
        print(f"File {dest_path.name} already fully downloaded.")
        return dest_path

    # Start or resume
    start_byte = dest_path.stat().st_size if dest_path.exists() else 0
    with open(dest_path, "ab") as f:
        while start_byte < total_size:
            end_byte = min(start_byte + CHUNK_SIZE - 1, total_size - 1)
            chunk_headers = {"User-Agent": "Mozilla/5.0", "Range": f"bytes={start_byte}-{end_byte}"}
            
            success = False
            for attempt in range(5):
                try:
                    # New connection each chunk
                    r = requests.get(url, headers=chunk_headers, timeout=15)
                    if r.status_code in [200, 206]:
                        f.write(r.content)
                        start_byte += len(r.content)
                        percent = (start_byte / total_size) * 100
                        print(f"  Progress: {start_byte}/{total_size} bytes ({percent:.1f}%)", end="\r", flush=True)
                        success = True
                        break
                except Exception as e:
                    print(f"\n  Chunk retry {attempt+1}/5 due to {e}")
                    time.sleep(1)
            
            if not success:
                raise RuntimeError(f"Failed to download chunk starting at {start_byte}")
                
    print(f"\nSuccessfully downloaded {dest_path.name}!")
    return dest_path

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python chunk_downloader.py <package_name> [version]")
        sys.exit(1)
    
    pkg = sys.argv[1]
    ver = sys.argv[2] if len(sys.argv) > 2 else None
    
    dest_dir = Path("/tmp/wheels")
    dest_dir.mkdir(exist_ok=True)
    
    url, filename = get_wheel_url(pkg, ver)
    dest_file = dest_dir / filename
    download_file_chunked(url, dest_file)
