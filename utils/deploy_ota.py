import os
import glob
import json
import hashlib
import shutil
import subprocess
import sys

def sha256_file(path):
    """Calculate the SHA256 hash of a file."""
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(4096)
                if not chunk:
                    break
                h.update(chunk)
        return h.hexdigest()
    except Exception as e:
        print(f"Error hashing {path}: {e}")
        return ""

def main():
    utils_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(utils_dir)
    
    # 1. Load config to get type, hardware_version, firmware_version
    config_path = os.path.join(project_root, "config.defaults.json")
    if not os.path.exists(config_path):
        print(f"Error: {config_path} not found")
        sys.exit(1)
        
    with open(config_path, "r") as f:
        cfg = json.load(f)
        
    client_cfg = cfg.get("client", {})
    device_type = client_cfg.get("type", "pump")
    hw_ver = client_cfg.get("hardware_version", "esp32_1.0")
    fw_ver = client_cfg.get("firmware_version", "firmesp32_v2")
    
    print(f"📦 Packaging OTA Release for type '{device_type}', HW version '{hw_ver}', FW version '{fw_ver}'...")
    
    # 2. Create local staging directory
    staging_dir = os.path.join(project_root, ".ota_staging")
    if os.path.exists(staging_dir):
        try:
            shutil.rmtree(staging_dir)
        except Exception as e:
            print(f"Warning: Failed to clean staging directory: {e}")
            
    os.makedirs(staging_dir, exist_ok=True)
    
    # 3. Compile list of files to package
    files_to_copy = []
    
    # Config files (config.defaults.json, config.json)
    for f in glob.glob(os.path.join(project_root, "config*.json")):
        files_to_copy.append((f, os.path.basename(f)))
        
    # Root python files (excluding utility/helper files)
    for f in glob.glob(os.path.join(project_root, "*.py")):
        basename = os.path.basename(f)
        if basename not in ("flash_esp32.py", "verify_device.py", "pack_code.py", "unpack_code.py", "deploy_ota.py"):
            files_to_copy.append((f, basename))
            
    # Lib python files
    for f in glob.glob(os.path.join(project_root, "lib", "*.py")):
        files_to_copy.append((f, f"lib/{os.path.basename(f)}"))
        
    # Copy files and calculate hashes for manifest
    manifest_files = []
    manifest_hashes = {}
    
    for src, rel_dest in files_to_copy:
        dest_path = os.path.join(staging_dir, rel_dest)
        # Create parent directories (like lib/) in staging if they don't exist
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)
        shutil.copy2(src, dest_path)
        
        # Calculate hash on the copied file
        h = sha256_file(dest_path)
        if h:
            manifest_files.append(rel_dest)
            manifest_hashes[rel_dest] = h
            print(f" - Staged: {rel_dest} (SHA256: {h[:8]}...)")
        
    # 4. Generate manifest.json inside the staging folder
    manifest = {
        "files": manifest_files,
        "sha256": manifest_hashes
    }
    
    manifest_path = os.path.join(staging_dir, "manifest.json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(" - Generated manifest.json")
    
    # 5. Connect and upload to OTA server
    # Load MQTT/OTA config to get the ota details
    ota_cfg = cfg.get("ota", {})
    # Default server config matching task.json
    ssh_user_host = ota_cfg.get("ssh_target", "sysadmin@10.10.10.211")
    remote_root = ota_cfg.get("remote_path", "/srv/ota/fw")
    
    # Structured target folder: /srv/ota/fw/pump/esp32_1.0/firmesp32_v2
    remote_dest_dir = f"{remote_root}/{device_type}/{hw_ver}/{fw_ver}"
    
    print(f"\n📡 Connecting to server {ssh_user_host} to create path {remote_dest_dir}...")
    try:
        subprocess.run(
            ["ssh", ssh_user_host, f"mkdir -p {remote_dest_dir}"],
            check=True
        )
    except Exception as e:
        print(f"❌ Error: Failed to create remote directory via SSH. Ensure SSH access is working. Details: {e}")
        sys.exit(1)
        
    print("📤 Uploading firmware files recursively...")
    try:
        # Use scp -r with staging_dir contents
        # On Windows, using "." ensures we copy the folder contents without copying the folder name itself
        subprocess.run(
            ["scp", "-r", os.path.join(staging_dir, "."), f"{ssh_user_host}:{remote_dest_dir}/"],
            check=True
        )
        print(f"\n🎉 Success: Firmware release version '{fw_ver}' successfully deployed to OTA server!")
        print(f"Remote Destination: {remote_dest_dir}/")
        
        # 6. Publish MQTT Trigger Command
        mqtt_cfg = cfg.get("mqtt", {})
        mqtt_server = ota_cfg.get("mqtt_server") or mqtt_cfg.get("server", "10.10.10.211")
        mqtt_port = int(ota_cfg.get("mqtt_port") or mqtt_cfg.get("port", 1883))
        
        broadcast_topic = f"{device_type}/{hw_ver}/command"
        update_payload = json.dumps({
            "command": "OTA",
            "version": fw_ver,
            "manifest": True
        })
        
        print(f"\n📢 Publishing OTA trigger to MQTT broker ({mqtt_server}:{mqtt_port}) on topic '{broadcast_topic}'...")
        try:
            import paho.mqtt.publish as publish
            publish.single(
                topic=broadcast_topic,
                payload=update_payload,
                hostname=mqtt_server,
                port=mqtt_port,
                retain=False
            )
            print("✅ MQTT trigger published successfully!")
        except Exception as mq_err:
            print(f"⚠️ Warning: Failed to publish MQTT trigger: {mq_err}")

    except Exception as e:
        print(f"❌ Error: Failed to copy files via SCP: {e}")
        sys.exit(1)
    finally:
        # Clean up local staging directory
        try:
            shutil.rmtree(staging_dir)
        except:
            pass

if __name__ == '__main__':
    main()
