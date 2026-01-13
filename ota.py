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

def download_file(url, dest):
    r = urequests.get(url)
    if r.status_code != 200:
        r.close()
        raise Exception("HTTP %d" % r.status_code)

    with open(dest + ".new", "wb") as f:
        f.write(r.content)

    r.close()
    os.rename(dest + ".new", dest)

def ota_update(base_url, files, hashes=None):
    for fname in files:
        url = f"{base_url}/{fname}"
        print("OTA downloading:", url)
        download_file(url, fname)

        if hashes and fname in hashes:
            h = sha256_file(fname)
            if h != hashes[fname]:
                raise Exception("Hash mismatch for " + fname)

    print("OTA success, rebooting")
    machine.reset()
