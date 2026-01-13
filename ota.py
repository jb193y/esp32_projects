# ota.py
import urequests
import os
import machine
import ubinascii
import uhashlib

def sha256_file(path):
    h = uhashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(512)
            if not chunk:
                break
            h.update(chunk)
    return ubinascii.hexlify(h.digest()).decode()

def _download_stream(url, dest_tmp):
    r = urequests.get(url)
    if r.status_code != 200:
        try:
            r.close()
        except:
            pass
        raise Exception("HTTP %d" % r.status_code)

    # stream from socket to file (low memory)
    with open(dest_tmp, "wb") as f:
        while True:
            chunk = r.raw.read(512)
            if not chunk:
                break
            f.write(chunk)

    try:
        r.close()
    except:
        pass

def download_and_stage(base_url, fname):
    url = "%s/%s" % (base_url.rstrip("/"), fname)
    tmp = fname + ".new"
    print("OTA downloading:", url)
    _download_stream(url, tmp)
    return tmp

def apply_staged(fname):
    tmp = fname + ".new"
    bak = fname + ".bak"

    # remove older bak
    try:
        if bak in os.listdir():
            os.remove(bak)
    except:
        pass

    # backup current
    try:
        if fname in os.listdir():
            os.rename(fname, bak)
    except:
        pass

    # activate new
    os.rename(tmp, fname)

def rollback(files):
    for fname in files:
        tmp = fname + ".new"
        bak = fname + ".bak"
        try:
            if tmp in os.listdir():
                os.remove(tmp)
        except:
            pass
        try:
            if bak in os.listdir():
                # restore backup
                if fname in os.listdir():
                    os.remove(fname)
                os.rename(bak, fname)
        except:
            pass

def fetch_manifest(base_url, manifest_name="manifest.json"):
    url = "%s/%s" % (base_url.rstrip("/"), manifest_name)
    r = urequests.get(url)
    if r.status_code != 200:
        try:
            r.close()
        except:
            pass
        raise Exception("Manifest HTTP %d" % r.status_code)
    data = r.json()
    try:
        r.close()
    except:
        pass
    return data

def ota_update(base_url, files=None, hashes=None, manifest=None):
    """
    Preferred: pass manifest dict:
      {"files":[...], "sha256":{ "main.py":"...", ... }}
    Or pass files + hashes explicitly.
    """
    if manifest:
        files = manifest.get("files", [])
        hashes = manifest.get("sha256", {})

    if not files:
        raise Exception("OTA: no files specified")

    staged = []
    try:
        # download all first
        for fname in files:
            download_and_stage(base_url, fname)
            staged.append(fname)

            if hashes and fname in hashes:
                h = sha256_file(fname + ".new")
                if h != hashes[fname]:
                    raise Exception("Hash mismatch for %s" % fname)

        # apply all
        for fname in staged:
            apply_staged(fname)

    except Exception as e:
        print("OTA failed:", e)
        rollback(staged)
        raise

    print("OTA success, rebooting")
    machine.reset()
