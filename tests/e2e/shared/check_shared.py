"""Consistency checks for several storage pods on one shared volume, called by
tests/e2e/shared/run.sh.

  check_shared.py write MARKER   parallel uploads into a new folder, then
                                 parallel renames, each verified by listing
                                 and download
  check_shared.py verify MARKER  the folder from write is complete and every
                                 file has its content (after pod replacement)

Environment as for ../check.py: OC (OpenCloud base URL), KC (Keycloak).
"""
import concurrent.futures
import os
import sys
import urllib.parse
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
import check  # noqa: E402  (login, request, check, failures)

FILES = int(os.environ.get("FILES", "40"))
WORKERS = int(os.environ.get("WORKERS", "16"))


def content(marker, i):
    return f"shared storage {marker} file {i}\n".encode() * 64


def names(token, folder):
    status, raw = check.request("PROPFIND", f"{check.OC}/remote.php/webdav/{folder}/", token, headers={"Depth": "1"})
    if status != 207:
        return status, set()
    hrefs = [e.text for e in ET.fromstring(raw).iter("{DAV:}href")]
    return status, {urllib.parse.unquote(h.rstrip("/").rsplit("/", 1)[-1]) for h in hrefs[1:]}


def expected(marker):
    # Even files are renamed by write; odd ones keep their name.
    return {f"{'renamed' if i % 2 == 0 else 'file'}-{i}.txt": content(marker, i) for i in range(FILES)}


def verify(token, marker):
    folder = f"shared-{marker}"
    want = expected(marker)
    status, found = names(token, folder)
    check.check(f"{folder}: listing", status == 207, status)
    check.check(f"{folder}: exactly the {len(want)} expected files", found == set(want), sorted(found ^ set(want))[:10])

    def download(name):
        status, raw, waited = check.download(f"{check.OC}/remote.php/webdav/{folder}/{name}", token)
        return name, status, status == 200 and raw == want[name], waited

    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        results = list(pool.map(download, sorted(want)))
    bad = [(name, status) for name, status, ok, _ in results if not ok]
    check.check(f"{folder}: every file downloads with its content", not bad, bad[:10])
    longest = max((waited for *_, waited in results), default=0)
    if longest >= 1:
        print(f"     longest wait for postprocessing: {longest:.0f}s")


def write(marker):
    token, _ = check.login("alice")
    folder = f"shared-{marker}"
    status, raw = check.request("MKCOL", f"{check.OC}/remote.php/webdav/{folder}", token)
    check.check(f"create {folder}", status == 201, (status, raw[:200]))

    def upload(i):
        status, _ = check.request("PUT", f"{check.OC}/remote.php/webdav/{folder}/file-{i}.txt", token, data=content(marker, i))
        return i, status

    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        bad = [(i, s) for i, s in pool.map(upload, range(FILES)) if s not in (201, 204)]
    check.check(f"{FILES} parallel uploads with {WORKERS} workers", not bad, bad[:10])

    def rename(i):
        status, _ = check.request(
            "MOVE", f"{check.OC}/remote.php/webdav/{folder}/file-{i}.txt", token,
            headers={"Destination": f"{check.OC}/remote.php/webdav/{folder}/renamed-{i}.txt", "Overwrite": "F"})
        return i, status

    with concurrent.futures.ThreadPoolExecutor(WORKERS) as pool:
        bad = [(i, s) for i, s in pool.map(rename, range(0, FILES, 2)) if s not in (201, 204)]
    check.check("parallel renames", not bad, bad[:10])
    verify(token, marker)


if __name__ == "__main__":
    mode, marker = sys.argv[1], sys.argv[2]
    if mode == "write":
        write(marker)
    else:
        verify(check.login("alice")[0], marker)
    if check.failures:
        sys.exit(f"{len(check.failures)} check(s) failed")
