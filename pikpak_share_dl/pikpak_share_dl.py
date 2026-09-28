#!/usr/bin/env python3
"""
PikPak share-link downloader (personal use).

Usage:
  python pikpak_share_dl.py "https://mypikpak.com/s/<share_id>[/<parent_id>[/<file_id>]]" [options]

Flow:
  1. Anonymous: captcha/init -> GET /drive/v1/share (gets pass_code_token + file list)
     -> walk folders via /drive/v1/share/detail
     -> GET /drive/v1/share/file_info for each video to obtain a direct link.
  2. If anonymous file_info gives no link (PikPak often requires an account),
     log in with --user/--password, "restore" (save) the files into your own
     drive, then fetch direct links via GET /drive/v1/files/{id}.
"""
import argparse
import hashlib
import json
import os
import re
import sys
import time
import uuid
from urllib.parse import urlparse

import requests

USER_HOST = "https://user.mypikpak.com"
DRIVE_HOST = "https://api-drive.mypikpak.com"

# Android client constants (same values used by the open-source PikPakAPI project).
CLIENT_ID = "YNxT9w7GMdWvEOKa"
CLIENT_SECRET = "dbw2OtmVEeuUvIptb1Coyg"
CLIENT_VERSION = "1.47.1"
PACKAGE_NAME = "com.pikcloud.pikpak"
SDK_VERSION = "2.0.4.204000"
# Salts for captcha_sign. If PikPak rotates them, update this list.
ALGORITHMS = [
    "Gez0T9ijiI9WCeTsKSg3SMlx",
    "zQdbalsolyb1R/",
    "ftOjr52zt51JD68C3s",
    "yeOBMH0JkbQdEFNNwQ0RI9T3wU/v",
    "BRJrQZiTQ65WtMvwO",
    "je8fqxKPdQVJiy1DM6Bc9Nb1",
    "niV",
    "9hFCW2R1",
    "sHKHpe2i96",
    "p7c5E6AcXQ/IJUuAEC9W6",
    "",
    "aRv9hjc9P+Pbn+u3krN6",
    "BzStcgE8qVdqjEH16l4",
    "SqgeZvL5j9zoHP95xWHt",
    "zVof5yaJkPe3VFpadPof",
]

VIDEO_EXT = (".mp4", ".mkv", ".mov", ".avi", ".wmv", ".flv", ".webm", ".m4v", ".ts", ".rmvb")

DEBUG = False


def log(*a):
    print(*a, file=sys.stderr)


def dbg(label, obj):
    if DEBUG:
        log(f"[debug] {label}: {json.dumps(obj, ensure_ascii=False)[:3000]}")


def parse_share_url(url):
    parts = [p for p in urlparse(url).path.split("/") if p]
    if len(parts) < 2 or parts[0] != "s":
        raise SystemExit(f"Not a PikPak share URL: {url}")
    share_id = parts[1]
    parent_id = parts[2] if len(parts) > 2 else ""
    file_id = parts[3] if len(parts) > 3 else ""
    return share_id, parent_id, file_id


class PikPak:
    def __init__(self, proxy=None):
        self.s = requests.Session()
        if proxy:
            self.s.proxies = {"http": proxy, "https": proxy}
        self.device_id = uuid.uuid4().hex
        self.access_token = None
        self.user_id = ""
        self.captcha_token = ""
        self.s.headers.update({
            "User-Agent": self.user_agent(),
            "X-Client-Id": CLIENT_ID,
            "X-Client-Version": CLIENT_VERSION,
            "X-Device-Id": self.device_id,
            "Content-Type": "application/json; charset=utf-8",
        })

    def user_agent(self):
        return (
            f"ANDROID-{PACKAGE_NAME}/{CLIENT_VERSION} protocolVersion/200 accesstype/ "
            f"clientid/{CLIENT_ID} clientversion/{CLIENT_VERSION} action_type/ "
            f"networktype/WIFI sessionid/ deviceid/{self.device_id} providername/NONE "
            f"devicesign/div101.{self.device_id}{hashlib.md5(self.device_id.encode()).hexdigest()} "
            f"refresh_token/ sdkversion/{SDK_VERSION} datetime/{int(time.time()*1000)} "
            f"usrno/ appname/android-{PACKAGE_NAME} session_origin/ grant_type/ appid/ "
            f"clientip/ devicename/Xiaomi_M2004j7ac osversion/13 platformversion/10 "
            f"accessmode/ devicemodel/M2004J7AC"
        )

    # ---------- captcha ----------
    def captcha_sign(self, ts):
        s = f"{CLIENT_ID}{CLIENT_VERSION}{PACKAGE_NAME}{self.device_id}{ts}"
        for salt in ALGORITHMS:
            s = hashlib.md5((s + salt).encode()).hexdigest()
        return "1." + s

    def captcha_init(self, action, meta=None):
        ts = str(int(time.time() * 1000))
        meta = dict(meta or {})
        meta.setdefault("captcha_sign", self.captcha_sign(ts))
        meta.setdefault("client_version", CLIENT_VERSION)
        meta.setdefault("package_name", PACKAGE_NAME)
        meta.setdefault("user_id", self.user_id)
        meta.setdefault("timestamp", ts)
        body = {
            "client_id": CLIENT_ID,
            "action": action,
            "device_id": self.device_id,
            "captcha_token": self.captcha_token,
            "meta": meta,
        }
        r = self.s.post(f"{USER_HOST}/v1/shield/captcha/init", json=body)
        j = r.json()
        dbg("captcha_init", j)
        if "captcha_token" not in j:
            raise RuntimeError(f"captcha init failed: {j}")
        if j.get("url"):
            log("PikPak requested interactive verification. Open this URL in a browser, "
                "solve it, and retry:\n  " + j["url"])
        self.captcha_token = j["captcha_token"]
        self.s.headers["X-Captcha-Token"] = self.captcha_token
        return self.captcha_token

    # ---------- auth ----------
    def login(self, username, password):
        if re.match(r"^\S+@\S+\.\S+$", username):
            meta = {"email": username}
        elif re.match(r"^\+?\d{8,15}$", username):
            meta = {"phone_number": username}
        else:
            meta = {"username": username}
        self.captcha_init("POST:/v1/auth/signin", meta)
        r = self.s.post(f"{USER_HOST}/v1/auth/signin", json={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "username": username,
            "password": password,
            "captcha_token": self.captcha_token,
        })
        j = r.json()
        dbg("signin", j)
        if "access_token" not in j:
            raise RuntimeError(f"login failed: {j}")
        self.access_token = j["access_token"]
        self.user_id = j.get("sub", "")
        self.s.headers["Authorization"] = f"Bearer {self.access_token}"

    # ---------- generic request ----------
    def api(self, method, path, action=None, **kw):
        if action:
            self.captcha_init(action)
        r = self.s.request(method, f"{DRIVE_HOST}{path}", **kw)
        try:
            j = r.json()
        except ValueError:
            raise RuntimeError(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
        dbg(f"{method} {path}", j)
        if isinstance(j, dict) and j.get("error"):
            # captcha expired -> retry once with a fresh token
            if j.get("error") in ("captcha_invalid", "captcha_required") and action:
                self.captcha_init(action)
                r = self.s.request(method, f"{DRIVE_HOST}{path}", **kw)
                j = r.json()
                dbg(f"{method} {path} (retry)", j)
            if j.get("error"):
                raise RuntimeError(f"{method} {path}: {j}")
        return j

    # ---------- share ----------
    def share_root(self, share_id, pass_code="", parent_id=""):
        params = {
            "share_id": share_id,
            "pass_code": pass_code,
            "thumbnail_size": "SIZE_LARGE",
            "limit": "100",
        }
        if parent_id:
            params["parent_id"] = parent_id
        j = self.api("GET", "/drive/v1/share", action="GET:/drive/v1/share", params=params)
        status = j.get("share_status")
        if status and status != "OK":
            raise RuntimeError(f"share status: {status} {j.get('share_status_text', '')}")
        return j

    def share_list(self, share_id, pass_code_token, parent_id):
        files, page = [], ""
        while True:
            j = self.api("GET", "/drive/v1/share/detail", params={
                "share_id": share_id,
                "parent_id": parent_id,
                "pass_code_token": pass_code_token,
                "limit": "100",
                "page_token": page,
                "thumbnail_size": "SIZE_LARGE",
            })
            files += j.get("files", [])
            page = j.get("next_page_token", "")
            if not page:
                return files

    def share_file_info(self, share_id, pass_code_token, file_id):
        j = self.api("GET", "/drive/v1/share/file_info", params={
            "share_id": share_id,
            "file_id": file_id,
            "pass_code_token": pass_code_token,
        })
        return j.get("file_info", j)

    def restore(self, share_id, pass_code_token, file_ids):
        j = self.api("POST", "/drive/v1/share/restore", json={
            "share_id": share_id,
            "pass_code_token": pass_code_token,
            "file_ids": file_ids,
            "params": {"trace_file_ids": ",".join(file_ids)},
        })
        return j

    def wait_task(self, task_id, timeout=120):
        end = time.time() + timeout
        while time.time() < end:
            j = self.api("GET", f"/drive/v1/tasks/{task_id}")
            if j.get("phase") == "PHASE_TYPE_COMPLETE":
                return j
            if j.get("phase") == "PHASE_TYPE_ERROR":
                raise RuntimeError(f"restore task failed: {j}")
            time.sleep(2)
        raise RuntimeError("restore task timed out")

    def my_file(self, file_id):
        return self.api("GET", f"/drive/v1/files/{file_id}", params={"usage": "FETCH"})


def best_link(info):
    """Pick a direct download URL from a file object."""
    if info.get("web_content_link"):
        return info["web_content_link"]
    for m in info.get("medias") or []:
        url = (m.get("link") or {}).get("url")
        if url and m.get("is_origin"):
            return url
    for m in info.get("medias") or []:
        url = (m.get("link") or {}).get("url")
        if url:
            return url
    for v in (info.get("links") or {}).values():
        if v.get("url"):
            return v["url"]
    return None


def is_video(f):
    return f.get("mime_type", "").startswith("video/") or f.get("name", "").lower().endswith(VIDEO_EXT)


def walk(pk, share_id, token, parent_id, rel=""):
    for f in pk.share_list(share_id, token, parent_id):
        path = os.path.join(rel, f["name"])
        if f.get("kind") == "drive#folder":
            yield from walk(pk, share_id, token, f["id"], path)
        else:
            yield f, path


def all_targets(pk, share_id, token, root):
    targets = []
    for f in root.get("files", []):
        if f.get("kind") == "drive#folder":
            targets += list(walk(pk, share_id, token, f["id"], f["name"]))
        else:
            targets.append((f, f["name"]))
    return targets


def resolve_targets(pk, share_id, token, root, ids):
    """Resolve the IDs in the URL path (/s/<share>/<id>/<id>...) to files.

    The trailing segments may be folders, a folder + file, or share-scoped IDs
    that file_info does not accept, so try several strategies from the most
    specific to the whole share.
    """
    for target_id in reversed(ids):
        # 1) it is a folder -> list it
        try:
            items = pk.share_list(share_id, token, target_id)
            if items:
                log(f"URL id {target_id} is a folder")
                return list(walk(pk, share_id, token, target_id))
        except RuntimeError as e:
            dbg(f"list {target_id} failed", str(e))
        # 2) it is a file -> file_info
        try:
            info = pk.share_file_info(share_id, token, target_id)
            if info.get("id") or info.get("name"):
                return [(info, info.get("name", target_id))]
        except RuntimeError as e:
            dbg(f"file_info {target_id} failed", str(e))
    everything = all_targets(pk, share_id, token, root)
    # 3) match the ID anywhere in the share
    for target_id in reversed(ids):
        hit = [(f, p) for f, p in everything if f.get("id") == target_id]
        if hit:
            return hit
    if ids:
        log("could not resolve the IDs in the URL; using every file in the share")
    return everything


def download(url, dest, session):
    os.makedirs(os.path.dirname(dest) or ".", exist_ok=True)
    tmp = dest + ".part"
    have = os.path.getsize(tmp) if os.path.exists(tmp) else 0
    headers = {"Range": f"bytes={have}-"} if have else {}
    with session.get(url, headers=headers, stream=True, timeout=60) as r:
        if r.status_code == 416:
            os.replace(tmp, dest)
            return
        r.raise_for_status()
        mode = "ab" if have and r.status_code == 206 else "wb"
        if mode == "wb":
            have = 0
        total = have + int(r.headers.get("Content-Length", 0))
        done = have
        with open(tmp, mode) as fh:
            for chunk in r.iter_content(1 << 20):
                fh.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done/1e6:8.1f} / {total/1e6:.1f} MB ({done*100/total:5.1f}%)",
                          end="", file=sys.stderr)
    log("")
    os.replace(tmp, dest)


def main():
    global DEBUG
    ap = argparse.ArgumentParser(description="Download videos from a PikPak share link")
    ap.add_argument("url")
    ap.add_argument("-o", "--out", default="downloads")
    ap.add_argument("-p", "--pass-code", default="", help="share password, if any")
    ap.add_argument("-u", "--user", default=os.environ.get("PIKPAK_USER"))
    ap.add_argument("-P", "--password", default=os.environ.get("PIKPAK_PASS"))
    ap.add_argument("--all", action="store_true", help="download all files, not only videos")
    ap.add_argument("--list", action="store_true", help="only list files / links, don't download")
    ap.add_argument("--proxy")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    DEBUG = a.debug

    share_id, parent_id, file_id = parse_share_url(a.url)
    pk = PikPak(a.proxy)
    if a.user and a.password:
        pk.login(a.user, a.password)
        log("logged in")

    root = pk.share_root(share_id, a.pass_code)
    token = root.get("pass_code_token", "")

    # Build the target list
    dbg("root files", [(f.get("id"), f.get("name"), f.get("kind")) for f in root.get("files", [])])
    targets = resolve_targets(pk, share_id, token, root, [i for i in (parent_id, file_id) if i])
    if not a.all:
        targets = [(f, p) for f, p in targets if is_video(f)]
    if not targets:
        raise SystemExit("no (video) files found in the share")
    log(f"{len(targets)} file(s):")
    for f, p in targets:
        log(f"  {p}  ({int(f.get('size', 0))/1e6:.1f} MB)")

    # Resolve links: anonymous share/file_info first
    links = {}
    for f, p in targets:
        info = f
        if not best_link(f):
            try:
                info = pk.share_file_info(share_id, token, f["id"])
            except RuntimeError as e:
                log(f"  file_info failed for {p}: {e}")
        link = best_link(info)
        if link:
            links[f["id"]] = link

    # Fallback: save into own drive and read the link from there
    missing = [f["id"] for f, _ in targets if f["id"] not in links]
    if missing:
        if not pk.access_token:
            raise SystemExit(
                f"{len(missing)} file(s) have no anonymous link. Re-run with "
                "--user/--password (or PIKPAK_USER/PIKPAK_PASS) so the files can be "
                "saved to your drive and downloaded from there.")
        log(f"saving {len(missing)} file(s) to your drive ...")
        res = pk.restore(share_id, token, missing)
        task_id = res.get("restore_task_id") or (res.get("task") or {}).get("id")
        mapping = {}
        if task_id:
            task = pk.wait_task(task_id)
            trace = (task.get("params") or {}).get("trace_file_ids")
            if trace:
                mapping = json.loads(trace) if isinstance(trace, str) else trace
        if not mapping:
            raise SystemExit("restore finished but no file-id mapping was returned; "
                             "files are in your drive — run with --debug and check the output.")
        for old, new in mapping.items():
            link = best_link(pk.my_file(new))
            if link:
                links[old] = link

    for f, p in targets:
        link = links.get(f["id"])
        if a.list or not link:
            print(f"{p}\t{link or 'NO LINK'}")
            continue
        dest = os.path.join(a.out, p)
        if os.path.exists(dest):
            log(f"skip (exists): {dest}")
            continue
        log(f"downloading {p}")
        download(link, dest, requests.Session())
    log("done")


if __name__ == "__main__":
    main()
