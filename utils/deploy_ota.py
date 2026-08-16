import os
import glob
import json
import hashlib
import shutil
import subprocess
import sys
import time

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
    if len(sys.argv) < 2:
        print("❌ Error: Missing component name.")
        print("Usage: python utils/deploy_ota.py [hub | valve_controller] [optional_version]")
        sys.exit(1)
        
    component = sys.argv[1].lower()
    if component not in ("hub", "valve_controller"):
        print(f"❌ Error: Invalid component '{component}'. Must be 'hub' or 'valve_controller'.")
        sys.exit(1)

    utils_dir = os.path.dirname(os.path.abspath(__file__))
    project_root = os.path.dirname(utils_dir)
    component_dir = os.path.join(project_root, component)
    
    if not os.path.exists(component_dir):
        print(f"❌ Error: Component directory {component_dir} not found")
        sys.exit(1)
        
    # 1. Load configuration
    config_defaults_path = os.path.join(component_dir, "config.defaults.json")
    config_live_path = os.path.join(component_dir, "config.json")
    
    cfg = {}
    if os.path.exists(config_defaults_path):
        with open(config_defaults_path, "r") as f:
            cfg.update(json.load(f))
    if os.path.exists(config_live_path):
        try:
            with open(config_live_path, "r") as f:
                cfg.update(json.load(f))
        except Exception as e:
            print(f"Warning: Failed to load config.json: {e}")
            
    client_cfg = cfg.get("client", {})
    device_type = client_cfg.get("type", "hub" if component == "hub" else "valve")
    hw_ver = client_cfg.get("hardware_version", "esp32_1.0")
    
    if len(sys.argv) >= 3:
        fw_ver = sys.argv[2]
    else:
        fw_ver = client_cfg.get("firmware_version", "v1.0.0")
        
    print(f"📦 Packaging OTA Release for '{component}'...")
    print(f" - Device Type: {device_type}")
    print(f" - HW Version:  {hw_ver}")
    print(f" - FW Version:  {fw_ver}")
    
    # 2. Setup Staging Directory
    staging_dir = os.path.join(project_root, ".ota_staging")
    if os.path.exists(staging_dir):
        try:
            shutil.rmtree(staging_dir)
        except Exception as e:
            print(f"Warning: Failed to clean staging directory: {e}")
            
    os.makedirs(staging_dir, exist_ok=True)
    
    # Target folders inside staging mapping to remote folders
    comp_staging_dir = os.path.join(staging_dir, component)
    lib_staging_dir = os.path.join(staging_dir, "lib")
    os.makedirs(comp_staging_dir, exist_ok=True)
    os.makedirs(lib_staging_dir, exist_ok=True)
    
    manifest_files = []
    manifest_hashes = {}
    manifest_server_paths = {}
    
    # Pack Component Config and Python files (which will sit in root of device)
    comp_files = glob.glob(os.path.join(component_dir, "config*.json")) + glob.glob(os.path.join(component_dir, "*.py"))
    for f in comp_files:
        basename = os.path.basename(f)
        dest_path = os.path.join(comp_staging_dir, basename)
        shutil.copy2(f, dest_path)
        h = sha256_file(dest_path)
        if h:
            manifest_files.append(basename)
            manifest_hashes[basename] = h
            manifest_server_paths[basename] = f"{component}/{basename}"
            print(f" - Staged Component File: {basename} -> {component}/{basename} (SHA256: {h[:8]}...)")
            
    # Pack Shared Lib files (which sit in lib/ directory of device)
    lib_files = glob.glob(os.path.join(project_root, "lib", "*.py"))
    for f in lib_files:
        basename = os.path.basename(f)
        rel_device_path = f"lib/{basename}"
        dest_path = os.path.join(lib_staging_dir, basename)
        shutil.copy2(f, dest_path)
        h = sha256_file(dest_path)
        if h:
            manifest_files.append(rel_device_path)
            manifest_hashes[rel_device_path] = h
            manifest_server_paths[rel_device_path] = f"lib/{basename}"
            print(f" - Staged Shared Lib File: {rel_device_path} -> lib/{basename} (SHA256: {h[:8]}...)")
            
    # 3. Generate manifest name
    manifest_name = f"manifest_{component}.json"
    manifest_path = os.path.join(staging_dir, manifest_name)
    
    manifest = {
        "files": manifest_files,
        "sha256": manifest_hashes,
        "server_paths": manifest_server_paths
    }
    
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    print(f" - Generated {manifest_name}")
    
    # 4. Connect and upload to OTA server
    ota_cfg = cfg.get("ota", {})
    ssh_user_host = ota_cfg.get("ssh_target", "sysadmin@10.10.10.211")
    remote_root = ota_cfg.get("remote_path", "/srv/ota/fw")
    ota_url = ota_cfg.get("base_url", "http://10.10.10.211:8000/fw")
    
    print(f"\n📡 Connecting to server {ssh_user_host} to create path {remote_root}...")
    try:
        subprocess.run(
            ["ssh", ssh_user_host, f"mkdir -p {remote_root}"],
            check=True
        )
    except Exception as e:
        print(f"❌ Error: Failed to create remote directory via SSH. Details: {e}")
        sys.exit(1)
        
    print("📤 Uploading firmware files recursively to remote root...")
    try:
        # Upload staged folder contents to remote root
        # Staging folder has: hub/ (or valve_controller/), lib/, manifest_{component}.json
        subprocess.run(
            ["scp", "-r", os.path.join(staging_dir, "."), f"{ssh_user_host}:{remote_root}/"],
            check=True
        )
        print(f"\n🎉 Success: Firmware release successfully deployed to OTA server!")
        print(f"Remote Path: {remote_root}/")
        
        # 5. Publish MQTT Trigger Command in Standard JSON Envelope
        mqtt_cfg = cfg.get("mqtt", {})
        mqtt_server = ota_cfg.get("mqtt_server") or mqtt_cfg.get("server", "10.10.10.211")
        mqtt_port = int(ota_cfg.get("mqtt_port") or mqtt_cfg.get("port", 1883))
        
        site = client_cfg.get("site", "loc001")
        group = client_cfg.get("group", "all")
        target_device_id = client_cfg.get("id", "all")
        
        # Topic structure: site/group/device_type/device_id/command
        command_topic = f"{site}/{group}/{device_type}/{target_device_id}/command"
        
        envelope_payload = json.dumps({
            "source": "backend_api",
            "target": target_device_id,
            "msg_type": "COMMAND",
            "timestamp": int(time.time()),
            "route": {
                "transport": "MQTT",
                "route_id": "ota_deploy",
                "current_hop_index": 0,
                "hops": ["backend_api"],
                "link_diagnostics": []
            },
            "data": {
                "cmd": "OTA",
                "version": fw_ver,
                "url": ota_url,
                "manifest_name": manifest_name
            }
        })
        
        print(f"\n📢 Publishing OTA trigger to MQTT broker ({mqtt_server}:{mqtt_port}) on topic '{command_topic}'...")
        try:
            import paho.mqtt.publish as publish
            publish.single(
                topic=command_topic,
                payload=envelope_payload,
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
