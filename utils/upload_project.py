import serial
import time
import os
import sys

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
        
    # Write in chunks of 32 bytes to prevent UART buffer overrun
    chunk_size = 32
    # Ensure file is opened and emptied
    send_command(ser, f"f = open('{remote_path}', 'wb')\n")
    
    for i in range(0, len(data), chunk_size):
        chunk = data[i:i+chunk_size]
        # Format as list of ints to avoid escaping issues
        bytes_repr = list(chunk)
        send_command(ser, f"f.write(bytes({bytes_repr}))\n")
        time.sleep(0.02) # Short break for execution
        
    send_command(ser, "f.close()\n")
    print("Uploaded successfully!")

def main():
    port = 'COM3'
    if len(sys.argv) > 1:
        port = sys.argv[1]
        
    print(f"Connecting to {port} at 115200...")
    ser = serial.Serial(port, 115200, timeout=2)
    
    # Enters raw REPL mode:
    # 1. Send Ctrl-C (0x03) multiple times to interrupt any running script
    for _ in range(5):
        ser.write(b'\x03')
        time.sleep(0.1)
    time.sleep(0.5)
    # Clear input buffer
    ser.reset_input_buffer()
    
    # 2. Send Ctrl-A (0x01) to enter raw REPL
    ser.write(b'\x01')
    time.sleep(0.5)
    
    resp = ser.read(100)
    if b'raw REPL' not in resp:
        print("Failed to enter raw REPL. Response:", resp)
        ser.close()
        sys.exit(1)
    print("Entered raw REPL successfully.")
    
    # 3. Create lib directory if needed
    send_command(ser, "import os\ntry: os.mkdir('lib')\nexcept: pass\n")
    
    # Files to upload
    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    
    files = [
        (os.path.join(project_root, 'valve_controller', 'config.defaults.json'), 'config.defaults.json'),
        (os.path.join(project_root, 'valve_controller', 'config.json'), 'config.json'),
        (os.path.join(project_root, 'valve_controller', 'boot.py'), 'boot.py'),
        (os.path.join(project_root, 'valve_controller', 'main.py'), 'main.py'),
        (os.path.join(project_root, 'lib', 'config.py'), 'lib/config.py'),
        (os.path.join(project_root, 'lib', 'relay_engine.py'), 'lib/relay_engine.py'),
        (os.path.join(project_root, 'lib', 'esp_now_client.py'), 'lib/esp_now_client.py'),
        (os.path.join(project_root, 'lib', 'led_status.py'), 'lib/led_status.py'),
        (os.path.join(project_root, 'lib', 'ble_manager.py'), 'lib/ble_manager.py'),
    ]
    
    for local, remote in files:
        if os.path.exists(local):
            upload_file(ser, local, remote)
        else:
            print(f"File not found: {local}")
            
    # Exit raw REPL and soft reset: send Ctrl-B (0x02) then Ctrl-D (0x04)
    ser.write(b'\x02\x04')
    time.sleep(0.5)
    ser.close()
    print("Done! Files loaded and device reset.")

if __name__ == '__main__':
    main()
