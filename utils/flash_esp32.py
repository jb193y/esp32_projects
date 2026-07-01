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

def get_device_files_metadata(mpremote):
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
            [mpremote, 'connect', 'COM3', 'exec', device_script],
            capture_output=True,
            text=True,
            check=True
        )
        output = proc.stdout
    except subprocess.CalledProcessError as e:
        print(f"Warning: Failed to query device filesystem metadata: {e.stderr or e}")
        return {}

    if '__JSON_START__' not in output or '__JSON_END__' not in output:
        print("Warning: Could not parse device metadata response.")
        return {}
        
    json_str = output.split('__JSON_START__')[1].split('__JSON_END__')[0].strip()
    try:
        return json.loads(json_str)
    except Exception as e:
        print(f"Warning: Error decoding device files JSON: {e}")
        return {}

def main():
    # Resolve project root (parent of utils) and mpremote path
    utils_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(utils_dir)
    mpremote = os.path.join(project_root, '.venv', 'Scripts', 'mpremote.exe')
    
    if not os.path.exists(mpremote):
        print(f"Error: mpremote not found at {mpremote}")
        sys.exit(1)
        
    # --- 1. Query Device Files for Sync & Cleanup ---
    print("Scanning ESP32 device filesystem...")
    device_files = get_device_files_metadata(mpremote)
    
    # Compile the set of local files we expect to find on the device
    expected_files = {}
    
    # Root configs
    for f in glob.glob(os.path.join(project_root, 'config*.json')):
        rel = os.path.basename(f)
        expected_files[rel] = f
        
    # Root python files (excluding utility/helper scripts in utils/)
    for f in glob.glob(os.path.join(project_root, '*.py')):
        basename = os.path.basename(f)
        if basename not in ('flash_esp32.py', 'verify_device.py', 'pack_code.py', 'unpack_code.py'):
            expected_files[basename] = f
        
    # Lib python files
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
            subprocess.run([mpremote, 'connect', 'COM3', 'fs', 'rm', f':{f}'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    else:
        print("Device filesystem clean (no unwanted files).")

    # --- 3. Create remote lib folder if needed ---
    if any(rel.startswith('lib/') for rel in expected_files.keys()):
        # Quick check if lib directory needs creation
        lib_exists = any(dev_f.startswith('lib/') for dev_f in device_files.keys())
        if not lib_exists:
            print("\nCreating remote :lib directory...")
            subprocess.run([mpremote, 'connect', 'COM3', 'fs', 'mkdir', ':lib'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    
    # --- 4. Delta Sync Files ---
    print("\nSynchronizing out-of-sync files to ESP32...")
    sync_count = 0
    skip_count = 0
    
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
            # File matches, skip copying
            print(f" - Up to date: {rel_path}")
            skip_count += 1
        else:
            # File is out of sync or missing, copy it
            print(f" - Copying: {rel_path} (out of sync)...")
            # If copying a lib file, use correct target path
            target_path = f":{rel_path}"
            subprocess.run([mpremote, 'connect', 'COM3', 'fs', 'cp', local_abs_path, target_path])
            sync_count += 1

    print(f"\nESP32 sync complete! Synced: {sync_count} files, Skipped: {skip_count} files.")

if __name__ == '__main__':
    main()
