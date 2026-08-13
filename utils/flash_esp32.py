import os
import glob
import subprocess
import json
import hashlib
import sys

def get_local_file_hash(path):
    """Calculate the SHA256 hash of a local file."""
    h = hashlib.sha256()
    try:
        with open(path, 'rb') as f:
            while True:
                chunk = f.read(4096)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        print(f"Error reading local file {path}: {e}")
        return None

def get_device_files_metadata(mpremote, port):
    """Query the ESP32 for sizes and SHA256 hashes of all files recursively."""
    device_script = (
        "import os, hashlib, json\n"
        "def hash_file(path):\n"
        "    h = hashlib.sha256()\n"
        "    try:\n"
        "        with open(path, 'rb') as f:\n"
        "            while True:\n"
        "                c = f.read(256)\n"
        "                if not c: break\n"
        "                h.update(c)\n"
        "        return h.digest().hex()\n"
        "    except: return None\n"
        "res = {}\n"
        "def walk(path):\n"
        "    try:\n"
        "        for f in os.listdir(path):\n"
        "            p = path + '/' + f if path != '/' else '/' + f\n"
        "            s = os.stat(p)\n"
        "            if s[0] & 0x4000:\n"
        "                walk(p)\n"
        "            else:\n"
        "                rel = p.lstrip('/')\n"
        "                res[rel] = {'size': s[6], 'sha256': hash_file(p)}\n"
        "    except: pass\n"
        "walk('/')\n"
        "print('__JSON_START__')\n"
        "print(json.dumps(res))\n"
        "print('__JSON_END__')\n"
    )
    try:
        proc = subprocess.run(
            [*mpremote, 'connect', port, 'exec', device_script],
            capture_output=True,
            text=True,
            check=True
        )
        output = proc.stdout
    except subprocess.CalledProcessError as e:
        print(f"Error: Failed to query device filesystem metadata on {port}: {e.stderr or str(e)}")
        return None

    if '__JSON_START__' not in output or '__JSON_END__' not in output:
        print("Error: Could not parse device metadata response.")
        return None
        
    json_str = output.split('__JSON_START__')[1].split('__JSON_END__')[0].strip()
    try:
        return json.loads(json_str)
    except Exception as e:
        print(f"Error: Error decoding device files JSON: {e}")
        return None

def main():
    # Resolve project root (parent of utils) and python/mpremote path
    utils_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(utils_dir)
    python_exe = os.path.join(project_root, '.venv', 'Scripts', 'python.exe')
    mpremote = [python_exe, '-m', 'mpremote']
    
    # Parse arguments
    port = 'COM10'
    project_dir = None
    for arg in sys.argv[1:]:
        if arg.upper().startswith('COM'):
            port = arg
        else:
            project_dir = arg
            
    target_dir = os.path.join(project_root, project_dir) if project_dir else project_root
    
    if not os.path.exists(python_exe):
        print(f"Error: Python not found at {python_exe}")
        sys.exit(1)
        
    # --- 1. Query Device Files for Sync & Cleanup ---
    print(f"Scanning ESP32 device filesystem on {port}...")
    device_files = get_device_files_metadata(mpremote, port)
    if device_files is None:
        print("[ERROR] Device connection failed. Aborting sync.")
        sys.exit(1)
    
    # Compile the set of local files we expect to find on the device
    expected_files = {}
    
    # Project configs
    for f in glob.glob(os.path.join(target_dir, 'config*.json')):
        rel = os.path.basename(f)
        expected_files[rel] = f
        
    # Project python files (excluding utility/helper scripts in utils/)
    for f in glob.glob(os.path.join(target_dir, '*.py')):
        basename = os.path.basename(f)
        if basename not in ('flash_esp32.py', 'verify_device.py', 'pack_code.py', 'unpack_code.py'):
            expected_files[basename] = f
        
    # Global shared lib python files
    for f in glob.glob(os.path.join(project_root, 'lib', '*.py')):
        rel = f"lib/{os.path.basename(f)}"
        expected_files[rel] = f
        
    # --- 2. Delete Unwanted Files (Cleanup) ---
    preserve_list = {
        'events.jsonl',
        'faults.jsonl'
    }
    
    unwanted_files = []
    for dev_file in device_files.keys():
        # Do not clean up .bak files to preserve local rollback snapshots
        if dev_file.endswith('.bak'):
            continue
        if dev_file not in expected_files and dev_file not in preserve_list:
            unwanted_files.append(dev_file)
            
    if unwanted_files:
        print(f"Cleanup: Found {len(unwanted_files)} unwanted files on the ESP32. Deleting...")
        for f in unwanted_files:
            print(f" - Deleting {f}...")
            subprocess.run([*mpremote, 'connect', port, 'fs', 'rm', f':{f}'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    else:
        print("Device filesystem clean (no unwanted files).")
 
    # --- 3. Create remote lib folder if needed ---
    if any(rel.startswith('lib/') for rel in expected_files.keys()):
        # Quick check if lib directory needs creation
        lib_exists = any(dev_f.startswith('lib/') for dev_f in device_files.keys())
        if not lib_exists:
            print(f"\nCreating remote :lib directory on {port}...")
            subprocess.run([*mpremote, 'connect', port, 'fs', 'mkdir', ':lib'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    
    # --- 4. Delta Sync Files ---
    print("\nChecking file sync status...")
    up_to_date_files = []
    out_of_sync_files = []
    
    for rel_path, local_abs_path in sorted(expected_files.items()):
        # Calculate local file metadata
        local_size = os.path.getsize(local_abs_path)
        local_hash = get_local_file_hash(local_abs_path)
        
        # Check if file exists on device and matches metadata
        is_synced = False
        if rel_path in device_files:
            dev_meta = device_files[rel_path]
            if dev_meta.get('size') == local_size and dev_meta.get('sha256') == local_hash:
                is_synced = True
                
        if is_synced:
            up_to_date_files.append(rel_path)
        else:
            out_of_sync_files.append((rel_path, local_abs_path))
 
    # Print up-to-date files first
    if up_to_date_files:
        print("\nUp-to-date files (skipped):")
        for rel_path in up_to_date_files:
            print(f" - Up to date: {rel_path}")
            
    # Print and copy out-of-sync files
    if out_of_sync_files:
        print(f"\nSynchronizing out-of-sync files to ESP32 on {port}...")
        for rel_path, local_abs_path in out_of_sync_files:
            print(f" - Copying: {rel_path} (out of sync)...")
            target_path = f":{rel_path}"
            subprocess.run([*mpremote, 'connect', port, 'fs', 'cp', local_abs_path, target_path], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
 
    print(f"\nESP32 sync complete! Synced: {len(out_of_sync_files)} files, Skipped: {len(up_to_date_files)} files.")

if __name__ == '__main__':
    main()
