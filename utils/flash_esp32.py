import os
import glob
import json
import hashlib
import sys
import time
import serial

r"""
Usage:
    python utils/flash_esp32.py valve_controller COM21
    python utils/flash_esp32.py hub COM24
"""

def send_command(ser, cmd, timeout=5):
    """Execute raw Python code in Raw REPL and return response."""
    ser.timeout = timeout
    ser.write(cmd.encode('utf-8') + b'\x04')
    response = ser.read_until(b'\x04>')
    if b'Traceback' in response:
        print("Command notice:", response.decode('utf-8', errors='ignore'))
    return response

def enter_raw_repl(ser):
    """Enter raw REPL with hardware reset and aggressive interrupt stream."""
    print("Resetting ESP32 and capturing REPL prompt...")
    
    # 1. Hardware reset via RTS/DTR toggle
    ser.rts = False
    ser.dtr = False
    ser.setRTS(False)
    ser.setDTR(False)
    time.sleep(0.05)
    ser.setRTS(True)
    time.sleep(0.2)
    ser.setRTS(False)
    time.sleep(0.2)
    ser.reset_input_buffer()
    
    # 2. Send Ctrl-C interrupts during boot.py safe-boot window
    for _ in range(8):
        ser.write(b'\x03\x03')
        time.sleep(0.1)
    
    ser.reset_input_buffer()
    time.sleep(0.2)
    
    # 3. Send Ctrl-A to enter raw REPL
    ser.write(b'\x01')
    time.sleep(0.4)
    
    resp = ser.read_until(b'raw REPL; CTRL-B to exit\r\n>')
    if b'raw REPL' not in resp:
        # Retry Ctrl-A once more
        ser.write(b'\x03\x01')
        time.sleep(0.5)
        resp += ser.read_until(b'raw REPL; CTRL-B to exit\r\n>')
        
    if b'raw REPL' not in resp:
        raise RuntimeError(f"Could not enter raw REPL. Device response: {resp}")
        
    print("Connected to Raw REPL successfully.")

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

def get_device_files_metadata(ser):
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
    
    resp_bytes = send_command(ser, device_script, timeout=6)
    output = resp_bytes.decode('utf-8', errors='ignore')
    
    if '__JSON_START__' not in output or '__JSON_END__' not in output:
        print("Error: Could not parse device metadata response. Raw output:", output)
        return None
        
    json_str = output.split('__JSON_START__')[1].split('__JSON_END__')[0].strip()
    try:
        return json.loads(json_str)
    except Exception as e:
        print(f"Error decoding device files JSON: {e}")
        return None

def upload_file_stream(ser, local_path, remote_path):
    """Stream file content via base64 chunks over open raw REPL."""
    import binascii
    with open(local_path, 'rb') as f:
        data = f.read()
        
    b64_data = binascii.b2a_base64(data).decode('ascii').replace('\n', '')
    
    # Ensure directory exists if needed
    if '/' in remote_path:
        dir_name = '/'.join(remote_path.split('/')[:-1])
        send_command(ser, f"import os\ntry: os.mkdir('{dir_name}')\nexcept: pass\n")
        
    send_command(ser, f"import ubinascii\nf = open('{remote_path}', 'wb')\n")
    
    chunk_size = 256
    for i in range(0, len(b64_data), chunk_size):
        b64_chunk = b64_data[i:i+chunk_size]
        send_command(ser, f"f.write(ubinascii.a2b_base64('{b64_chunk}'))\n")
        
    send_command(ser, "f.close()\n")

def main():
    import argparse

    utils_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(utils_dir)
    
    parser = argparse.ArgumentParser(description="Reliable persistent delta-sync for ESP32 devices.")
    parser.add_argument("type", help="The project component to deploy (e.g., 'hub', 'valve_controller').")
    parser.add_argument("port", help="The COM port of the ESP32 device (e.g., 'COM21').")
    args = parser.parse_args()

    target_dir = os.path.join(project_root, args.type)
    if not os.path.exists(target_dir):
        print(f"Error: Target directory {target_dir} does not exist.")
        sys.exit(1)

    print(f"Connecting to {args.port} at 115200...")
    ser = serial.Serial(args.port, 115200, timeout=2)
    
    try:
        # --- 1. Enter Raw REPL ---
        enter_raw_repl(ser)
        
        # --- 2. Query Device Files for Delta Sync ---
        print(f"Scanning filesystem on {args.port}...")
        device_files = get_device_files_metadata(ser)
        if device_files is None:
            print("[ERROR] Failed to query device metadata.")
            sys.exit(1)
            
        # Compile local expected files
        expected_files = {}
        
        # Project configs
        for f in glob.glob(os.path.join(target_dir, 'config*.json')):
            rel = os.path.basename(f)
            expected_files[rel] = f
            
        # Project python files
        for f in glob.glob(os.path.join(target_dir, '*.py')):
            basename = os.path.basename(f)
            if basename not in ('flash_esp32.py', 'verify_device.py', 'pack_code.py', 'unpack_code.py'):
                expected_files[basename] = f
                
        # Shared lib python files
        for f in glob.glob(os.path.join(project_root, 'lib', '*.py')):
            rel = f"lib/{os.path.basename(f)}"
            expected_files[rel] = f
            
        # --- 3. Clean up unwanted files ---
        preserve_list = {'events.jsonl', 'faults.jsonl', 'config.json'}
        unwanted_files = []
        for dev_file in device_files.keys():
            if dev_file.endswith('.bak'):
                continue
            if dev_file not in expected_files and dev_file not in preserve_list:
                unwanted_files.append(dev_file)
                
        if unwanted_files:
            print(f"Cleanup: Deleting {len(unwanted_files)} obsolete files from device...")
            for f in unwanted_files:
                print(f" - Removing {f}...")
                send_command(ser, f"import os\ntry: os.remove('{f}')\nexcept: pass\n")
        else:
            print("Filesystem clean (no obsolete files).")
            
        # --- 4. Ensure :lib directory exists ---
        send_command(ser, "import os\ntry: os.mkdir('lib')\nexcept: pass\n")
        
        # --- 5. Delta Sync Files ---
        print("\nChecking delta file sync status...")
        up_to_date_files = []
        out_of_sync_files = []
        
        for rel_path, local_abs_path in sorted(expected_files.items()):
            local_size = os.path.getsize(local_abs_path)
            local_hash = get_local_file_hash(local_abs_path)
            
            is_synced = False
            if rel_path in device_files:
                dev_meta = device_files[rel_path]
                if dev_meta.get('size') == local_size and dev_meta.get('sha256') == local_hash:
                    is_synced = True
                    
            if is_synced:
                up_to_date_files.append(rel_path)
            else:
                out_of_sync_files.append((rel_path, local_abs_path))
                
        if up_to_date_files:
            print("\nUp-to-date files (skipped):")
            for rel_path in up_to_date_files:
                print(f" [=] {rel_path}")
                
        if out_of_sync_files:
            print(f"\nFlashing {len(out_of_sync_files)} updated/missing files...")
            for rel_path, local_abs_path in out_of_sync_files:
                print(f" [^] Uploading {rel_path}...")
                upload_file_stream(ser, local_abs_path, rel_path)
            print("All updated files uploaded successfully.")
        else:
            print("\nAll files are already up-to-date! No transfer needed.")
            
        # --- 6. Reset Device ---
        print("\nSync completed. Resetting device...")
        ser.write(b'\x02\x04') # Ctrl-B (exit raw REPL) + Ctrl-D (soft reboot)
        time.sleep(0.5)
        print("Done!")
        
    finally:
        ser.close()

if __name__ == '__main__':
    main()
