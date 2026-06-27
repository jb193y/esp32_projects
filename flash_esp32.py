import os
import glob
import subprocess
import sys

def main():
    # Resolve project root and mpremote path
    project_root = os.path.dirname(os.path.abspath(__file__))
    mpremote = os.path.join(project_root, '.venv', 'Scripts', 'mpremote.exe')
    
    if not os.path.exists(mpremote):
        print(f"Error: mpremote not found at {mpremote}")
        sys.exit(1)
        
    print("Creating remote :lib directory (if not exists)...")
    # Create lib directory on device (ignore error if it already exists)
    subprocess.run([mpremote, 'connect', 'COM3', 'fs', 'mkdir', ':lib'], stderr=subprocess.DEVNULL, stdout=subprocess.DEVNULL)
    
    # 1. Copy config*.json to :
    print("\nUploading JSON configuration files...")
    for f in glob.glob(os.path.join(project_root, 'config*.json')):
        print(f"Copying {os.path.basename(f)} to device root...")
        subprocess.run([mpremote, 'connect', 'COM3', 'fs', 'cp', f, ':'])
        
    # 2. Copy *.py in root to : (excluding helper scripts)
    print("\nUploading root Python files...")
    for f in glob.glob(os.path.join(project_root, '*.py')):
        basename = os.path.basename(f)
        if basename in ('flash_esp32.py', 'pack_code.py', 'unpack_code.py'):
            continue
        print(f"Copying {basename} to device root...")
        subprocess.run([mpremote, 'connect', 'COM3', 'fs', 'cp', f, ':'])
        
    # 3. Copy lib/*.py to :lib/
    print("\nUploading lib Python files...")
    for f in glob.glob(os.path.join(project_root, 'lib', '*.py')):
        basename = os.path.basename(f)
        print(f"Copying {basename} to :lib/{basename}...")
        subprocess.run([mpremote, 'connect', 'COM3', 'fs', 'cp', f, f':lib/{basename}'])

    print("\nESP32 sync complete!")

if __name__ == '__main__':
    main()
