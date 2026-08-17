# ota.py (Shared OTA update library)
import urequests
import os
import machine
import ubinascii
import uhashlib
import gc
import time

def _dirname(path):
    if '/' not in path:
        return ""
    return "/".join(path.split("/")[:-1])

def _makedirs(path):
    if not path or path == ".":
        return
    parts = path.split("/")
    current = ""
    for part in parts:
        if not part:
            continue
        current = current + "/" + part if current else part
        try:
            os.mkdir(current)
        except:
            pass

def sha256_file(path):
    """Calculates SHA256 in chunks to save memory."""
    h = uhashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(512)
                if not chunk: break
                h.update(chunk)
        return ubinascii.hexlify(h.digest()).decode()
    except:
        return ""

def _download_stream(url, dest_tmp):
    """Streams file from web to flash memory directly."""
    gc.collect()
    r = urequests.get(url, stream=True)
    if r.status_code != 200:
        r.close()
        raise Exception("HTTP %d" % r.status_code)

    # Ensure parent directories exist
    parent_dir = _dirname(dest_tmp)
    if parent_dir and parent_dir != "/":
        _makedirs(parent_dir)

    with open(dest_tmp, "wb") as f:
        while True:
            chunk = r.raw.read(512)
            if not chunk: break
            f.write(chunk)
    r.close()
    gc.collect()

def download_and_stage(base_url, fname, server_path=None):
    download_path = server_path if server_path else fname
    url = "%s/%s" % (base_url.rstrip("/"), download_path.lstrip("/"))
    tmp = fname + ".new"
    print("📡 OTA Downloading:", fname, "from", download_path)
    _download_stream(url, tmp)
    return tmp

def apply_staged(fname):
    """Atomic swap of the new file into place."""
    tmp = fname + ".new"
    bak = fname + ".bak"
    
    # Ensure nested directories exist for final destination
    parent_dir = _dirname(fname)
    if parent_dir and parent_dir != "/":
        _makedirs(parent_dir)

    try:
        if bak in os.listdir(parent_dir or "."): 
            os.remove(bak)
    except: pass
    try:
        if fname in os.listdir(parent_dir or "."): 
            os.rename(fname, bak)
    except: pass
    os.rename(tmp, fname)

def rollback(files):
    """Restores .bak files if update fails."""
    for fname in files:
        tmp, bak = fname + ".new", fname + ".bak"
        parent_dir = _dirname(fname)
        try:
            if tmp in os.listdir(parent_dir or "."): 
                os.remove(tmp)
        except: pass
        try:
            if bak in os.listdir(parent_dir or "."):
                if fname in os.listdir(parent_dir or "."): 
                    os.remove(fname)
                os.rename(bak, fname)
        except: pass

def fetch_manifest(base_url, manifest_name="manifest.json"):
    gc.collect()
    url = "%s/%s" % (base_url.rstrip("/"), manifest_name)
    r = urequests.get(url)
    if r.status_code != 200:
        r.close()
        raise Exception("Manifest HTTP %d" % r.status_code)
    data = r.json()
    r.close()
    return data

def ota_update(base_url, files=None, hashes=None, manifest=None):
    if manifest:
        files = manifest.get("files", [])
        hashes = manifest.get("sha256", {})
        server_paths = manifest.get("server_paths", {})
    else:
        server_paths = {}

    if not files:
        raise Exception("OTA: No files provided")

    staged = []
    skipped = []
    try:
        for fname in files:
            # Check if local file already matches the manifest hash
            is_synced = False
            if hashes and fname in hashes:
                expected_hash = hashes[fname]
                local_hash = sha256_file(fname)
                if local_hash == expected_hash:
                    is_synced = True
            
            if is_synced:
                print(f" - Up to date (skipped download): {fname}")
                skipped.append(fname)
            else:
                print(f" - Fetching out-of-sync file: {fname}")
                server_path = server_paths.get(fname)
                download_and_stage(base_url, fname, server_path)
                staged.append(fname)
                
                # Verify the downloaded file hash
                if hashes and fname in hashes:
                    actual_hash = sha256_file(fname + ".new")
                    if actual_hash != hashes[fname]:
                        raise Exception("Hash mismatch: %s" % fname)

        # Apply changes to the filesystem (only for newly downloaded files!)
        for fname in staged:
            apply_staged(fname)

        print(f"✅ OTA complete. Synced: {len(staged)} files, Skipped: {len(skipped)} files.")
        return True  # Return True so the caller knows it's safe to reboot

    except Exception as e:
        print("❌ OTA Error - rolling back:", e)
        rollback(staged)
        raise e
