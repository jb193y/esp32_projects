# espnow_ota.py (ESP-NOW Firmware Over-The-Air Update Library)
import os
import time
import gc
try:
    import ubinascii
except ImportError:
    import binascii as ubinascii

try:
    import uhashlib
except ImportError:
    import hashlib as uhashlib

try:
    import machine
except ImportError:
    class MockMachine:
        def reset(self):
            pass
    machine = MockMachine()
import config

CHUNK_SIZE = 160  # Raw bytes per chunk -> ~214 base64 chars (fits in 250-byte ESP-NOW MTU)
MAX_RETRIES = 5
CHUNK_TIMEOUT_SEC = 2.0

def _dirname(path):
    if '/' not in path:
        return ""
    return "/".join(path.split("/")[:-1])

def _makedirs(path):
    if not path or path == ".":
        return
    parts = path.split("/")
    current = ""
    for part in parts:
        if not part:
            continue
        current = current + "/" + part if current else part
        try:
            os.mkdir(current)
        except:
            pass

def sha256_file(path):
    """Calculates SHA-256 in 512-byte blocks."""
    h = uhashlib.sha256()
    try:
        with open(path, "rb") as f:
            while True:
                chunk = f.read(512)
                if not chunk:
                    break
                h.update(chunk)
        return ubinascii.hexlify(h.digest()).decode()
    except Exception:
        return ""

def apply_staged(fname):
    """Atomically replaces old file with new file, saving .bak copy."""
    tmp = fname + ".new"
    bak = fname + ".bak"
    parent_dir = _dirname(fname)
    if parent_dir and parent_dir != "/":
        _makedirs(parent_dir)

    try:
        if bak in os.listdir(parent_dir or "."):
            os.remove(bak)
    except:
        pass
    try:
        if fname in os.listdir(parent_dir or "."):
            os.rename(fname, bak)
    except:
        pass
    os.rename(tmp, fname)

def rollback_files(files):
    """Rollback .new files or restore .bak files upon failure."""
    for fname in files:
        tmp, bak = fname + ".new", fname + ".bak"
        parent_dir = _dirname(fname)
        try:
            if tmp in os.listdir(parent_dir or "."):
                os.remove(tmp)
        except:
            pass
        try:
            if bak in os.listdir(parent_dir or "."):
                if fname in os.listdir(parent_dir or "."):
                    os.remove(fname)
                os.rename(bak, fname)
        except:
            pass


# ==============================================================================
# OTAReceiver (Runs on Valve / Pump / Field Nodes)
# ==============================================================================
class OTAReceiver:
    def __init__(self, send_fn):
        self.send_fn = send_fn  # Callback: send_fn(msg_type, payload, target_mac)
        self.state = "IDLE"     # IDLE, RECEIVING, VERIFIED
        self.target_version = None
        self.manifest_files = {}  # fname -> {"size": N, "sha256": "...", "total_chunks": M}
        self.current_file = None
        self.current_fd = None
        self.expected_seq = 0
        self.received_files = []
        self.last_activity = 0
        self.sender_mac = None

    def is_in_progress(self):
        return self.state in ("RECEIVING", "VERIFIED")

    def handle_packet(self, data, sender_mac=None):
        """Processes incoming OTA protocol packet from Hub."""
        action = data.get("action")
        self.last_activity = time.time()
        if sender_mac:
            self.sender_mac = sender_mac

        if action == "OTA_START":
            return self._handle_start(data)
        elif action == "OTA_CHUNK":
            return self._handle_chunk(data)
        elif action == "OTA_VERIFY":
            return self._handle_verify(data)
        elif action == "OTA_APPLY":
            return self._handle_apply(data)
        elif action == "OTA_ABORT":
            return self._handle_abort(data)
        return False

    def _handle_start(self, data):
        self.target_version = data.get("version", "unknown")
        files_info = data.get("files", [])
        self.manifest_files = {f["name"]: f for f in files_info}
        self.received_files = []
        self.current_file = None
        if self.current_fd:
            try:
                self.current_fd.close()
            except:
                pass
            self.current_fd = None

        print(f" [OTA Node] Started OTA Update session for version {self.target_version} ({len(files_info)} files)")
        
        # Clean any stale .new files
        for fname in self.manifest_files.keys():
            tmp = fname + ".new"
            try:
                os.remove(tmp)
            except:
                pass

        self.state = "RECEIVING"
        self._send_ack({"status": "OTA_READY", "version": self.target_version})
        return True

    def _handle_chunk(self, data):
        if self.state != "RECEIVING":
            return False

        fname = data.get("file")
        seq = int(data.get("seq", 0))
        total_seq = int(data.get("total_seq", 1))
        b64_data = data.get("data", "")

        # Open file descriptor for current file if changed
        if self.current_file != fname:
            if self.current_fd:
                try:
                    self.current_fd.close()
                except:
                    pass
            self.current_file = fname
            self.expected_seq = 0
            
            parent_dir = _dirname(fname)
            if parent_dir and parent_dir != "/":
                _makedirs(parent_dir)
            self.current_fd = open(fname + ".new", "wb")

        # Deduplicate or handle expected sequence
        if seq == self.expected_seq:
            try:
                raw_bytes = ubinascii.a2b_base64(b64_data)
                self.current_fd.write(raw_bytes)
                self.expected_seq += 1
            except Exception as write_err:
                print(f" [OTA Node] File write error for {fname} (seq {seq}):", write_err)
                self._send_ack({"status": "CHUNK_ERR", "file": fname, "seq": seq, "err": str(write_err)})
                return False

        # Send ACK for chunk
        self._send_ack({
            "status": "CHUNK_ACK",
            "file": fname,
            "seq": seq,
            "next_seq": self.expected_seq
        })

        if seq + 1 >= total_seq:
            if self.current_fd:
                self.current_fd.flush()
                self.current_fd.close()
                self.current_fd = None
            if fname not in self.received_files:
                self.received_files.append(fname)
            print(f" [OTA Node] Completed receiving all chunks for {fname}")

        return True

    def _handle_verify(self, data):
        fname = data.get("file")
        expected_sha = data.get("sha256", "")

        if self.current_fd:
            try:
                self.current_fd.close()
            except:
                pass
            self.current_fd = None

        tmp_path = fname + ".new"
        calc_sha = sha256_file(tmp_path)

        if expected_sha and calc_sha.lower() != expected_sha.lower():
            print(f" [OTA Node] SHA-256 mismatch for {fname}! Expected: {expected_sha}, Got: {calc_sha}")
            self._send_ack({"status": "VERIFY_FAILED", "file": fname, "expected": expected_sha, "got": calc_sha})
            self.state = "IDLE"
            rollback_files(list(self.manifest_files.keys()))
            return False

        print(f" [OTA Node] Verified SHA-256 for {fname} successfully ({calc_sha[:8]}...)")
        self._send_ack({"status": "VERIFY_OK", "file": fname})
        return True

    def _handle_apply(self, data):
        version = data.get("version") or self.target_version
        print(f" [OTA Node] Applying all staged files for version {version}...")
        try:
            for fname in self.received_files:
                apply_staged(fname)
                print(f"  Applied {fname}")

            # Update firmware version in local config.json
            config.update_config({"client": {"firmware_version": version}})
            self.state = "VERIFIED"
            
            # Send final success confirmation
            self._send_ack({"status": "OTA_SUCCESS", "version": version})
            
            # Allow time for ACK transmission before reset
            print(" [OTA Node] Firmware update complete! Rebooting in 500ms...")
            time.sleep_ms(500)
            machine.reset()
            return True
        except Exception as apply_err:
            print(" [OTA Node] Failed to apply staged firmware:", apply_err)
            self._send_ack({"status": "APPLY_FAILED", "err": str(apply_err)})
            rollback_files(self.received_files)
            self.state = "IDLE"
            return False

    def _handle_abort(self, data):
        print(" [OTA Node] OTA Abort received from Hub.")
        if self.current_fd:
            try:
                self.current_fd.close()
            except:
                pass
            self.current_fd = None
        rollback_files(list(self.manifest_files.keys()))
        self.state = "IDLE"
        self._send_ack({"status": "OTA_ABORTED"})
        return True

    def _send_ack(self, payload):
        if self.send_fn:
            payload["ota_proto"] = "espnow_v1"
            self.send_fn("ACK", payload, target_mac=self.sender_mac)


# ==============================================================================
# OTASender (Runs on Master Hub in Background Thread)
# ==============================================================================
class OTASender:
    def __init__(self, send_espnow_fn, log_callback=None):
        self.send_espnow_fn = send_espnow_fn
        self.log_callback = log_callback
        self.pending_ack = None
        self._ack_received = False
        self._ack_data = {}

    def notify_ack(self, ack_data):
        """Called by Hub's ESP-NOW receiver loop when an OTA ACK arrives."""
        self._ack_data = ack_data
        self._ack_received = True

    def _log(self, msg):
        print(f" [Hub OTA] {msg}")
        if self.log_callback:
            self.log_callback(msg)

    def fetch_manifest_and_stage(self, base_url, manifest_name="manifest.json"):
        """Downloads manifest and required files over Wi-Fi HTTP into Hub cache."""
        import urequests
        gc.collect()
        base_clean = base_url.rstrip("/")
        url = f"{base_clean}/{manifest_name}"
        self._log(f"Fetching OTA manifest from {url}...")

        r = urequests.get(url)
        if r.status_code != 200:
            r.close()
            raise Exception(f"HTTP {r.status_code} fetching manifest")
        manifest = r.json()
        r.close()

        files = manifest.get("files", [])
        hashes = manifest.get("sha256", {})
        server_paths = manifest.get("server_paths", {})
        version = manifest.get("version", "v1.0.0")

        _makedirs("/ota_cache")
        cached_files = []

        for fname in files:
            server_path = server_paths.get(fname, fname)
            f_url = f"{base_clean}/{server_path.lstrip('/')}"
            local_cache_path = f"/ota_cache/{fname.replace('/', '_')}"
            
            self._log(f"Caching file: {fname} from {f_url}")
            r = urequests.get(f_url, stream=True)
            if r.status_code != 200:
                r.close()
                raise Exception(f"HTTP {r.status_code} downloading {fname}")

            with open(local_cache_path, "wb") as out_f:
                while True:
                    chunk = r.raw.read(256)
                    if not chunk:
                        break
                    out_f.write(chunk)
            r.close()

            # Verify cached file sha256
            expected_sha = hashes.get(fname)
            if expected_sha:
                calc_sha = sha256_file(local_cache_path)
                if calc_sha.lower() != expected_sha.lower():
                    raise Exception(f"SHA mismatch for cached {fname}")

            f_size = os.stat(local_cache_path)[6]
            cached_files.append({
                "name": fname,
                "cache_path": local_cache_path,
                "size": f_size,
                "sha256": expected_sha or sha256_file(local_cache_path)
            })
            gc.collect()

        return version, cached_files

    def _wait_for_ack(self, expected_status=None, expected_seq=None, timeout_sec=CHUNK_TIMEOUT_SEC):
        start_t = time.time()
        self._ack_received = False
        while time.time() - start_t < timeout_sec:
            if self._ack_received:
                status = self._ack_data.get("status")
                seq = self._ack_data.get("seq")
                if expected_status and status != expected_status:
                    time.sleep_ms(10)
                    continue
                if expected_seq is not None and seq != expected_seq:
                    time.sleep_ms(10)
                    continue
                return True, self._ack_data
            time.sleep_ms(20)
        return False, {}

    def stream_firmware_to_node(self, target_mac, target_id, version, cached_files):
        """Streams cached firmware files to peer node over ESP-NOW."""
        self._log(f"Initiating ESP-NOW OTA stream to {target_id} ({target_mac}) for version {version}")

        # 1. Send OTA_START
        files_meta = [{"name": f["name"], "size": f["size"], "sha256": f["sha256"]} for f in cached_files]
        start_payload = {
            "cmd": "OTA",
            "action": "OTA_START",
            "version": version,
            "files": files_meta
        }

        ready = False
        for attempt in range(MAX_RETRIES):
            self._log(f"Sending OTA_START (attempt {attempt + 1}/{MAX_RETRIES})...")
            self.send_espnow_fn(target_mac, {"msg_type": "COMMAND", "payload": start_payload}, target_id=target_id)
            ok, ack = self._wait_for_ack(expected_status="OTA_READY", timeout_sec=3.0)
            if ok:
                ready = True
                break
            time.sleep_ms(100)

        if not ready:
            raise Exception("Peer node failed to acknowledge OTA_START")

        # 2. Stream Each File Chunks
        for f_idx, f_info in enumerate(cached_files):
            fname = f_info["name"]
            cache_path = f_info["cache_path"]
            f_size = f_info["size"]
            sha256_val = f_info["sha256"]

            total_seq = (f_size + CHUNK_SIZE - 1) // CHUNK_SIZE
            self._log(f"Streaming {fname} ({f_size} bytes in {total_seq} chunks)...")

            with open(cache_path, "rb") as f:
                seq = 0
                while True:
                    raw_chunk = f.read(CHUNK_SIZE)
                    if not raw_chunk:
                        break

                    b64_str = ubinascii.b2a_base64(raw_chunk).decode().strip()
                    chunk_payload = {
                        "cmd": "OTA",
                        "action": "OTA_CHUNK",
                        "file": fname,
                        "seq": seq,
                        "total_seq": total_seq,
                        "data": b64_str
                    }

                    chunk_ok = False
                    for retry in range(MAX_RETRIES):
                        self.send_espnow_fn(target_mac, {"msg_type": "COMMAND", "payload": chunk_payload}, target_id=target_id)
                        ok, ack = self._wait_for_ack(expected_status="CHUNK_ACK", expected_seq=seq, timeout_sec=CHUNK_TIMEOUT_SEC)
                        if ok:
                            chunk_ok = True
                            break
                        time.sleep_ms(50)

                    if not chunk_ok:
                        raise Exception(f"Failed to deliver chunk {seq} of {fname} after {MAX_RETRIES} retries")

                    seq += 1
                    # Small yield to prevent saturating radio and allow other Hub tasks
                    time.sleep_ms(20)

            # 3. Send OTA_VERIFY for the completed file
            self._log(f"Verifying {fname} SHA-256 on target node...")
            verify_payload = {
                "cmd": "OTA",
                "action": "OTA_VERIFY",
                "file": fname,
                "sha256": sha256_val
            }
            
            verified = False
            for attempt in range(MAX_RETRIES):
                self.send_espnow_fn(target_mac, {"msg_type": "COMMAND", "payload": verify_payload}, target_id=target_id)
                ok, ack = self._wait_for_ack(expected_status="VERIFY_OK", timeout_sec=4.0)
                if ok:
                    verified = True
                    break
                time.sleep_ms(100)

            if not verified:
                raise Exception(f"Node verification failed for {fname}")

        # 4. Send OTA_APPLY
        self._log(f"All files verified! Sending OTA_APPLY for version {version}...")
        apply_payload = {
            "cmd": "OTA",
            "action": "OTA_APPLY",
            "version": version
        }
        
        applied = False
        for attempt in range(MAX_RETRIES):
            self.send_espnow_fn(target_mac, {"msg_type": "COMMAND", "payload": apply_payload}, target_id=target_id)
            ok, ack = self._wait_for_ack(expected_status="OTA_SUCCESS", timeout_sec=5.0)
            if ok:
                applied = True
                break
            time.sleep_ms(100)

        if not applied:
            raise Exception("Node failed to acknowledge final OTA_APPLY")

        self._log(f"ESP-NOW OTA Update to {target_id} completed successfully!")
        
        # Cleanup temporary cache files on Hub
        for f in cached_files:
            try:
                os.remove(f["cache_path"])
            except:
                pass
        return True
