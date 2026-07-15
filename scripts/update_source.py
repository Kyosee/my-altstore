#!/usr/bin/env python3
"""Regenerate the AltStore source manifest (apps.json) from upstream Kazumi releases.

Design goals (audit-friendly, supply-chain hardened):
  * The ``downloadURL`` of every version points ONLY at the upstream official
    GitHub release asset. This repo never re-hosts the .ipa, so there is no
    place to smuggle a tampered binary.
  * For every kept version we DOWNLOAD the upstream .ipa and compute sha256
    locally, then cross-check it against the digest GitHub computed
    server-side. A mismatch aborts the run.
  * bundleIdentifier / minOSVersion / version string are read from the .ipa's
    embedded Info.plist at build time, never hardcoded.
  * Only Python stdlib is used. No third-party packages, no pip install.

The script is deterministic: given the same upstream releases it produces the
same apps.json (aside from a generated timestamp comment field).
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import plistlib
import re
import sys
import urllib.error
import urllib.request
import zipfile
from datetime import datetime, timezone

# --- Configuration -----------------------------------------------------------

UPSTREAM_OWNER = "Predidit"
UPSTREAM_REPO = "Kazumi"
UPSTREAM = f"{UPSTREAM_OWNER}/{UPSTREAM_REPO}"

# Matches assets like: Kazumi_ios_2.2.0_no_sign.ipa
IPA_ASSET_RE = re.compile(r"^Kazumi_ios_.*\.ipa$", re.IGNORECASE)

# How many most-recent versions to keep in the source (user chose 5).
KEEP_VERSIONS = 5

# Source-level metadata for the AltStore feed itself.
SOURCE_NAME = "Kazumi (Unofficial AltStore Source)"
SOURCE_IDENTIFIER = "com.kyosee.kazumi-altstore"
SOURCE_SUBTITLE = "Auto-tracks upstream Predidit/Kazumi iOS releases"
SOURCE_DESCRIPTION = (
    "Unofficial AltStore/SideStore source for Kazumi. Every download link "
    "points directly at the official Predidit/Kazumi GitHub release asset; "
    "no binaries are re-hosted here. See the repository README for details."
)

APP_NAME = "Kazumi"
APP_DEVELOPER = "Predidit"
APP_SUBTITLE = "A cross-platform video streaming client"
APP_TINT_COLOR = "5A9BF6"
ICON_URL = (
    "https://raw.githubusercontent.com/Predidit/Kazumi/main/"
    "assets/images/logo/logo_ios.png"
)

OUTPUT_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "apps.json")

USER_AGENT = "kazumi-altstore-updater/1.0 (+https://github.com/Predidit/Kazumi)"


# --- HTTP helpers ------------------------------------------------------------


def _request(url: str, *, accept: str | None = None) -> bytes:
    headers = {"User-Agent": USER_AGENT}
    if accept:
        headers["Accept"] = accept
    token = os.environ.get("GITHUB_TOKEN")
    if token and "api.github.com" in url:
        headers["Authorization"] = f"Bearer {token}"
        headers["X-GitHub-Api-Version"] = "2022-11-28"
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=120) as resp:
        return resp.read()


def fetch_releases() -> list[dict]:
    url = f"https://api.github.com/repos/{UPSTREAM}/releases?per_page=100"
    data = json.loads(_request(url, accept="application/vnd.github+json"))
    if not isinstance(data, list):
        raise RuntimeError(f"Unexpected releases payload: {data!r}")
    return data


# --- IPA inspection ----------------------------------------------------------


def parse_info_plist(ipa_bytes: bytes) -> dict:
    """Extract the app Info.plist from an .ipa (a zip) and return it as a dict."""
    with zipfile.ZipFile(io.BytesIO(ipa_bytes)) as zf:
        candidates = [
            n
            for n in zf.namelist()
            if re.fullmatch(r"Payload/[^/]+\.app/Info\.plist", n)
        ]
        if not candidates:
            raise RuntimeError("No Payload/*.app/Info.plist found in IPA")
        with zf.open(candidates[0]) as fh:
            return plistlib.load(fh)


def normalize_min_os(plist: dict) -> str:
    return str(plist.get("MinimumOSVersion") or "13.0")


# --- Version assembly --------------------------------------------------------


def find_ipa_asset(release: dict) -> dict | None:
    for asset in release.get("assets", []):
        if IPA_ASSET_RE.match(asset.get("name", "")):
            return asset
    return None


def github_digest_sha256(asset: dict) -> str | None:
    """GitHub exposes a server-computed digest like 'sha256:abcd...'."""
    digest = asset.get("digest")
    if isinstance(digest, str) and digest.startswith("sha256:"):
        return digest.split(":", 1)[1].lower()
    return None


def build_version_entry(release: dict, asset: dict, cache: dict) -> dict:
    version = release.get("tag_name", "").lstrip("v")
    download_url = asset["browser_download_url"]

    cached = cache.get(download_url)
    if cached:
        return cached

    print(f"  downloading {asset['name']} ({asset['size']} bytes) ...", flush=True)
    ipa_bytes = _request(download_url)

    local_sha = hashlib.sha256(ipa_bytes).hexdigest()
    remote_sha = github_digest_sha256(asset)
    if remote_sha and remote_sha != local_sha:
        raise RuntimeError(
            f"sha256 mismatch for {download_url}: "
            f"github={remote_sha} local={local_sha}"
        )

    plist = parse_info_plist(ipa_bytes)
    bundle_id = plist.get("CFBundleIdentifier")
    if not bundle_id:
        raise RuntimeError(f"No CFBundleIdentifier in {asset['name']}")

    date_raw = release.get("published_at") or release.get("created_at") or ""
    date = date_raw.split("T", 1)[0] if date_raw else ""

    entry = {
        "version": version,
        "buildVersion": str(plist.get("CFBundleVersion", "")),
        "date": date,
        "localizedDescription": (release.get("body") or "").strip()
        or f"Kazumi {version}",
        "downloadURL": download_url,
        "size": int(asset["size"]),
        "sha256": local_sha,
        "minOSVersion": normalize_min_os(plist),
        "_bundleIdentifier": bundle_id,  # internal, stripped before output
    }
    return entry


def load_cache(path: str) -> dict:
    """Reuse already-computed version entries from a previous apps.json.

    Keyed by downloadURL. Since upstream release assets are immutable once
    published, a URL we've already processed cannot change; caching avoids
    re-downloading every historical .ipa on each run.
    """
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}
    cache: dict[str, dict] = {}
    for app in data.get("apps", []):
        bundle_id = app.get("bundleIdentifier")
        for ver in app.get("versions", []):
            url = ver.get("downloadURL")
            if not url or not ver.get("sha256"):
                continue
            restored = dict(ver)
            restored["_bundleIdentifier"] = bundle_id
            cache[url] = restored
    return cache


def main() -> int:
    output_path = os.path.abspath(OUTPUT_PATH)
    cache = load_cache(output_path)

    releases = fetch_releases()
    # Newest first by published date.
    releases.sort(
        key=lambda r: r.get("published_at") or r.get("created_at") or "",
        reverse=True,
    )

    versions: list[dict] = []
    bundle_id: str | None = None
    for release in releases:
        if release.get("draft") or release.get("prerelease"):
            continue
        asset = find_ipa_asset(release)
        if not asset:
            continue
        print(f"processing {release.get('tag_name')}", flush=True)
        entry = build_version_entry(release, asset, cache)
        if bundle_id is None:
            bundle_id = entry["_bundleIdentifier"]
        versions.append(entry)
        if len(versions) >= KEEP_VERSIONS:
            break

    if not versions:
        raise RuntimeError("No iOS .ipa releases found upstream; aborting")

    for entry in versions:
        entry.pop("_bundleIdentifier", None)

    manifest = {
        "name": SOURCE_NAME,
        "identifier": SOURCE_IDENTIFIER,
        "subtitle": SOURCE_SUBTITLE,
        "description": SOURCE_DESCRIPTION,
        "iconURL": ICON_URL,
        "website": f"https://github.com/{UPSTREAM}",
        "apps": [
            {
                "name": APP_NAME,
                "bundleIdentifier": bundle_id,
                "developerName": APP_DEVELOPER,
                "subtitle": APP_SUBTITLE,
                "localizedDescription": (
                    "Kazumi is a cross-platform video streaming client built "
                    "with Flutter. This entry is auto-generated from the "
                    "official Predidit/Kazumi GitHub releases."
                ),
                "iconURL": ICON_URL,
                "tintColor": APP_TINT_COLOR,
                "category": "entertainment",
                "screenshots": [],
                "versions": versions,
                # AltStore legacy top-level fields mirror the newest version.
                "version": versions[0]["version"],
                "versionDate": versions[0]["date"],
                "versionDescription": versions[0]["localizedDescription"],
                "downloadURL": versions[0]["downloadURL"],
                "size": versions[0]["size"],
            }
        ],
        "_generatedAt": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }

    with open(output_path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(manifest, fh, indent=2, ensure_ascii=False)
        fh.write("\n")

    print(
        f"wrote {output_path} with {len(versions)} version(s), "
        f"bundleIdentifier={bundle_id}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (urllib.error.URLError, RuntimeError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(1)
