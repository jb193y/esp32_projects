import os
import glob
import subprocess
import json
import sys

def get_device_files(mpremote):
    """Query the ESP32 for a list of all files recursively."""
    device_script = (
        "import os, json\n"
        "res = []\n"
        "def walk(path):\n"
        "    try:\n"
        "        for f in os.listdir(path):\n"
        "            p = path + '/' + f if path != '/' else '/' + f\n"
        "            s = os.stat(p)\n"
        "            if s[0] & 0x4000:\n"
        "                walk(p)\n"
        "            else:\n"
        "                res.append(p.lstrip('/'))\n"
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
        print(f"Warning: Failed to query device files for cleanup: {e.stderr or e}")
        return []

    if '__JSON_START__' not in output or '__JSON_END__' not in output:
        return []
        
    json_str = output.split('__JSON_START__')[1].split('__JSON_END__')[0].strip()
    try:
        return json.loads(json_str)
    except:
        return []

def main():
    # Resolve project root (parent of utils) and mpremote path
    utils_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(utils_dir)
    mpremote = os.path.join(project_root, '.venv', 'Scripts', 'mpremote.exe')
    
    if not os.path.exists(mpremote):
        print(f"Error: mpremote not found at {mpremote}")
        sys.exit(1)
        
    # --- 1. Query Device Files for Cleanup ---
    print("Checking for unwanted files on device filesystem...")
    device_files = get_device_files(mpremote)
    
    # Compile the set of local files we expect to find on the device
    expected_files = set()
    
    # Root configs
    for f in glob.glob(os.path.join(project_root, 'config*.json')):
        expected_files.add(os.path.basename(f))
        
    # Root python files (excluding utility/helper scripts in utils/)
    for f in glob.glob(os.path.join(project_root, '*.py')):
        expected_files.add(os.path.basename(f))
        
    # Lib python files
    for f in glob.glob(os.path.join(project_root, 'lib', '*.py')):
        expected_files.add(f"lib/{os.path.basename(f)}")
        
    # Preserve List (Files generated on-device that must not be deleted)
    preserve_list = {
        'events.jsonl',
        'faults.jsonl'
    }
    
    # Detect unwanted files
    unwanted_files = []
    for dev_file in device_files:
        if dev_file not in expected_files and dev_file not in preserve_list:
            unwanted_files.append(dev_file)
            
    # Delete unwanted files
    if unwanted_files:
        print(f"Cleanup: Found {len(unwanted_files)} unwanted files on the ESP32. Deleting them...")
        for f in unwanted_files:
            print(f" - Deleting {f}...")
            # Run mpremote rm command
            # Note: mpremote cp/rm command expects a leading colon (:) or direct path depending on version
            # Standard mpremote fs rm syntax is: mpremote fs rm :path
            subprocess.run([mpremote, 'connect', 'COM3', 'fs', 'rm', f':{f}'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    else:
        print("Device filesystem is clean. No unwanted files found.")

    # --- 2. Create remote lib folder ---
    print("\nCreating remote :lib directory (if not exists)...")
    subprocess.run([mpremote, 'connect', 'COM3', 'fs', 'mkdir', ':lib'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    
    # --- 3. Copy files to device ---
    # Copy config*.json
    print("\nUploading JSON configuration files...")
    for f in glob.glob(os.path.join(project_root, 'config*.json')):
        print(f"Copying {os.path.basename(f)} to device root...")
        subprocess.run([mpremote, 'connect', 'COM3', 'fs', 'cp', f, ':'])
        
    # Copy root python files
    print("\nUploading root Python files...")
    for f in glob.glob(os.path.join(project_root, '*.py')):
        basename = os.path.basename(f)
        if basename in ('flash_esp32.py', 'verify_device.py', 'pack_code.py', 'unpack_code.py'):
            continue
        print(f"Copying {basename} to device root...")
        subprocess.run([mpremote, 'connect', 'COM3', 'fs', 'cp', f, ':'])
        
    # Copy lib python files
    print("\nUploading lib Python files...")
    for f in glob.glob(os.path.join(project_root, 'lib', '*.py')):
        basename = os.path.basename(f)
        print(f"Copying {basename} to :lib/{basename}...")
        subprocess.run([mpremote, 'connect', 'COM3', 'fs', 'cp', f, f':lib/{basename}'])

    print("\nESP32 sync complete!")

if __name__ == '__main__':
    main()
