import serial
import time
import os
import sys
import glob

def send_command(ser, cmd):
    # Enters raw code, executes it, and exits raw code
    # raw REPL code blocks are terminated by Ctrl-D (0x04)
    ser.write(cmd.encode('utf-8') + b'\x04')
    
    # Read response
    response = ser.read_until(b'\x04>')
    if b'Traceback' in response or not response.endswith(b'\x04>'):
        print(f"Warning: Command execution might have failed!")
        print("Command:", repr(cmd))
        print("Response:", response.decode('utf-8', errors='ignore'))
    return response

def upload_file(ser, local_path, remote_path):
    print(f"Uploading {local_path} -> {remote_path} ...")
    with open(local_path, 'rb') as f:
        data = f.read()
        
    # Write in chunks of 64 bytes to prevent UART buffer overrun
    chunk_size = 64
    # Ensure file is opened and emptied
    send_command(ser, f"f = open('{remote_path}', 'wb')\n")
    
    for i in range(0, len(data), chunk_size):
        chunk = data[i:i+chunk_size]
        bytes_repr = list(chunk)
        send_command(ser, f"f.write(bytes({bytes_repr}))\n")
        time.sleep(0.01)
        
    send_command(ser, "f.close()\n")
    print(f"Uploaded {remote_path} ({len(data)} bytes) successfully!")

def main():
    if len(sys.argv) < 3:
        print("Usage: python upload_serial.py <PROJECT_DIR> <COM_PORT>")
        sys.exit(1)
        
    project_dir = sys.argv[1]
    port = sys.argv[2]
    
    utils_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(utils_dir)
    target_dir = os.path.join(project_root, project_dir)
    
    if not os.path.exists(target_dir):
        print(f"Error: Target directory {target_dir} does not exist.")
        sys.exit(1)
        
    print(f"Connecting to {port} at 115200...")
    # Open the serial port first. The rts/dtr arguments are not supported
    # in the constructor for all pyserial versions.
    ser = serial.Serial(port, 115200, timeout=2)
    
    # --- Force a hardware reset using DTR/RTS lines ---
    # This is a robust way to interrupt a device stuck in a boot loop.
    print("Forcing hardware reset...")
    # Set initial DTR/RTS state before toggling for reset.
    ser.rts = False
    ser.dtr = False
    ser.setRTS(False)
    ser.setDTR(False)  # IO0=HIGH
    time.sleep(0.1)
    ser.setRTS(True)  # RST=LOW -> Reset
    time.sleep(0.2)
    ser.setRTS(False) # RST=HIGH -> Normal boot
    time.sleep(0.2)
    ser.reset_input_buffer() # Clear any boot-up garbage after reset
    time.sleep(0.1)
    
    # Enters raw REPL mode:
    # 1. Send Ctrl-C (0x03) to interrupt any script that auto-started after reset
    print("Sending interrupts to stop running threads...")
    for _ in range(3):
        ser.write(b'\r\x03\x03')
        time.sleep(0.2)
    ser.reset_input_buffer()
    
    # 2. Send Ctrl-A (0x01) to enter raw REPL
    entered_raw = False
    for _ in range(5):
        ser.write(b'\r\x01')
        time.sleep(0.3)
        resp = ser.read_all()
        if b'raw REPL' in resp:
            entered_raw = True
            break
        ser.write(b'\x03')
        time.sleep(0.2)
    
    if not entered_raw:
        print("Failed to enter raw REPL. Response:", resp)
        ser.close()
        sys.exit(1)
    print("Entered raw REPL successfully.")
    
    # 3. Clean up files of the OTHER project to ensure clean swap
    if project_dir in ('valve_controller', 'vc'):
        # Remove Hub files if we are flashing a valve controller
        hub_files_to_remove = ['espnow_master.py', 'mqtt_client.py', 'network_manager.py', 'scheduler.py', 'nodes.json']
        print("Cleaning up Hub files from Valve Controller device...")
        for f in hub_files_to_remove:
            send_command(ser, f"import os\ntry: os.remove('{f}')\nexcept: pass\n")
    elif project_dir == 'hub':
        # Remove any valve specific files if present
        print("Cleaning up any valve specific states...")
        send_command(ser, "import os\ntry: os.remove('valve_states.json')\nexcept: pass\n")
        
    # 4. Create lib directory if needed
    send_command(ser, "import os\ntry: os.mkdir('lib')\nexcept: pass\n")
    
    # Compile files to upload
    files_to_upload = []
    
    def prepare_upload(local_path, remote_name):
        base = os.path.basename(local_path)
        if base in ('boot.py', 'main.py') or not local_path.endswith('.py'):
            return local_path, remote_name, False
        mpy_path = local_path[:-3] + '.mpy'
        try:
            import mpy_cross
            proc = mpy_cross.run(local_path)
            if proc.wait() == 0 and os.path.exists(mpy_path):
                remote_mpy = remote_name[:-3] + '.mpy'
                print(f" [Bytecode] Compiled {base} -> {os.path.basename(remote_mpy)}")
                return mpy_path, remote_mpy, True
            else:
                print(f" [Bytecode Warning] mpy_cross returncode != 0 for {base}")
        except Exception as e:
            print(f" [Bytecode Warning] mpy_cross error on {base}: {e}")
        return local_path, remote_name, False

    # Configs
    for f in glob.glob(os.path.join(target_dir, 'config*.json')):
        rel = os.path.basename(f)
        files_to_upload.append((f, rel, False))
        
    # Project python files
    for f in glob.glob(os.path.join(target_dir, '*.py')):
        basename = os.path.basename(f)
        if basename not in ('flash_esp32.py', 'verify_device.py', 'pack_code.py', 'unpack_code.py'):
            files_to_upload.append(prepare_upload(f, basename))
            
    # Shared lib python files
    for f in glob.glob(os.path.join(project_root, 'lib', '*.py')):
        rel = f"lib/{os.path.basename(f)}"
        files_to_upload.append(prepare_upload(f, rel))
        
    # Upload all files
    for local, remote, is_mpy in files_to_upload:
        if is_mpy:
            # Delete stale .py version from ESP32 flash so MicroPython prioritizes the .mpy bytecode
            old_py = remote[:-4] + '.py'
            send_command(ser, f"import os\ntry: os.remove('{old_py}')\nexcept: pass\n")
        upload_file(ser, local, remote)
        
    # Exit raw REPL and soft reset: send Ctrl-B (0x02) then Ctrl-D (0x04)
    print("Resetting board...")
    ser.write(b'\x02\x04')
    time.sleep(0.5)
    ser.close()
    print(f"Done! {project_dir} files loaded onto {port} and device reset.")

if __name__ == '__main__':
    main()
