import os
import glob
import subprocess
import json
import hashlib
import sys

def get_local_file_hash(path):
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

def main():
    if len(sys.argv) < 2:
        print("Usage: python utils/verify_device.py <COM_PORT>")
        sys.exit(1)
    port = sys.argv[1]

    # Resolve project root (parent of utils) and mpremote path
    utils_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(utils_dir)
    mpremote = 'mpremote'

    print(f"Fetching file list and checksums from ESP32 device on {port}...")
    
    # MicroPython script to run on the device
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
        "try:\n"
        "    for f in os.listdir('/'):\n"
        "        s = os.stat('/' + f)\n"
        "        if s[0] & 0x4000:\n"
        "            if f == 'lib':\n"
        "                try:\n"
        "                    for lf in os.listdir('/lib'):\n"
        "                        p = 'lib/' + lf\n"
        "                        res[p] = {'size': os.stat('/' + p)[6], 'sha256': hash_file('/' + p)}\n"
        "                except: pass\n"
        "        else:\n"
        "            res[f] = {'size': s[6], 'sha256': hash_file('/' + f)}\n"
        "except: pass\n"
        "print('__JSON_START__')\n"
        "print(json.dumps(res))\n"
        "print('__JSON_END__')\n"
    )

    # Run the script on the device
    try:
        proc = subprocess.run(
            [mpremote, 'connect', port, 'exec', device_script],
            capture_output=True,
            text=True,
            check=True
        )
        output = proc.stdout
    except subprocess.CalledProcessError as e:
        print(f"Error connecting to ESP32: {e.stderr or e}")
        sys.exit(1)

    # Parse output to extract JSON string
    if '__JSON_START__' not in output or '__JSON_END__' not in output:
        print("Error: Could not parse device output. Device response:")
        print(output)
        sys.exit(1)
        
    json_str = output.split('__JSON_START__')[1].split('__JSON_END__')[0].strip()
    try:
        device_files = json.loads(json_str)
    except Exception as e:
        print(f"Error decoding JSON from device: {e}")
        sys.exit(1)

    # Walk local codebase
    local_files = {}
    
    # 1. config files
    for f in glob.glob(os.path.join(project_root, 'config*.json')):
        rel_path = os.path.relpath(f, project_root).replace('\\', '/')
        local_files[rel_path] = {
            'size': os.path.getsize(f),
            'sha256': get_local_file_hash(f)
        }
        
    # 2. root py files
    for f in glob.glob(os.path.join(project_root, '*.py')):
        basename = os.path.basename(f)
        if basename in ('flash_esp32.py', 'verify_device.py', 'pack_code.py', 'unpack_code.py'):
            continue
        rel_path = basename
        local_files[rel_path] = {
            'size': os.path.getsize(f),
            'sha256': get_local_file_hash(f)
        }
        
    # 3. lib py files
    for f in glob.glob(os.path.join(project_root, 'lib', '*.py')):
        rel_path = os.path.relpath(f, project_root).replace('\\', '/')
        local_files[rel_path] = {
            'size': os.path.getsize(f),
            'sha256': get_local_file_hash(f)
        }

    # Compare
    print("\n--- Verification Report ---")
    print(f"{'File Path':<35} | {'Status':<15} | {'Local Size':<10} | {'Device Size':<10}")
    print("-" * 80)

    all_match = True
    mismatches = []
    missing_on_device = []
    extra_on_device = []

    # Check local files against device
    for rel_path, local_meta in sorted(local_files.items()):
        if rel_path not in device_files:
            print(f"{rel_path:<35} | Missing on Dev | {local_meta['size']:<10} | {'-':<10}")
            missing_on_device.append(rel_path)
            all_match = False
        else:
            dev_meta = device_files[rel_path]
            size_match = local_meta['size'] == dev_meta['size']
            hash_match = local_meta['sha256'] == dev_meta['sha256']
            
            if size_match and hash_match:
                print(f"{rel_path:<35} | Match           | {local_meta['size']:<10} | {dev_meta['size']:<10}")
            else:
                print(f"{rel_path:<35} | Mismatch        | {local_meta['size']:<10} | {dev_meta['size']:<10}")
                mismatches.append(rel_path)
                all_match = False

    # Check for extra files on device
    for rel_path in sorted(device_files.keys()):
        if rel_path.endswith('.bak'):
            continue
        if rel_path not in local_files:
            print(f"{rel_path:<35} | Extra on Dev    | {'-':<10} | {device_files[rel_path]['size']:<10}")
            extra_on_device.append(rel_path)

    print("-" * 80)
    if all_match:
        print("Success: All local files match the files on the ESP32 exactly!")
    else:
        print("Warning: Some differences were found between local codebase and ESP32.")
        if mismatches:
            print(f" - Mismatched files: {len(mismatches)}")
        if missing_on_device:
            print(f" - Missing on device: {len(missing_on_device)}")
        if extra_on_device:
            print(f" - Extra on device: {len(extra_on_device)}")

if __name__ == '__main__':
    main()
