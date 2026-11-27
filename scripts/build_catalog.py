#!/usr/bin/env python3
import argparse
import json
import os
import re
import subprocess
import urllib.error
import urllib.request

ASSET_PLATFORM_MAP = {
    "linux-amd64": "linux/amd64",
    "linux-arm64": "linux/arm64",
    "darwin-amd64": "darwin/amd64",
    "darwin-arm64": "darwin/arm64",
    "windows-amd64.exe": "windows/amd64",
}

CONTROL_ENUM_MAP = {
    "ADMIN_FORM_CONTROL_UNSPECIFIED": 0,
    "ADMIN_FORM_CONTROL_TEXT": 1,
    "ADMIN_FORM_CONTROL_TEXTAREA": 2,
    "ADMIN_FORM_CONTROL_PASSWORD": 3,
    "ADMIN_FORM_CONTROL_NUMBER": 4,
    "ADMIN_FORM_CONTROL_CHECKBOX": 5,
    "ADMIN_FORM_CONTROL_SELECT": 6,
    "UNSPECIFIED": 0,
    "TEXT": 1,
    "TEXTAREA": 2,
    "PASSWORD": 3,
    "NUMBER": 4,
    "CHECKBOX": 5,
    "SELECT": 6,
}

PRESERVED_MANIFEST_FIELDS = (
    "presentation",
    "capabilities",
    "http_routes",
    "global_config_schema",
)

SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:-([0-9A-Za-z.-]+))?(?:\+.*)?$")


def parse_semver(version):
    if not isinstance(version, str):
        return None
    m = SEMVER_RE.match(version.strip())
    if not m:
        return None
    major, minor, patch = int(m.group(1)), int(m.group(2)), int(m.group(3))
    prerelease = m.group(4)
    if prerelease is None:
        return (major, minor, patch, 1, ())
    parts = tuple(
        (0, int(part)) if part.isdigit() else (1, part)
        for part in prerelease.split(".")
    )
    return (major, minor, patch, 0, parts)


def is_newer_version(candidate, baseline):
    c_ver = parse_semver(candidate)
    b_ver = parse_semver(baseline)
    if c_ver is None or b_ver is None:
        return False
    return c_ver > b_ver


def detect_git_branch():
    env_branch = os.environ.get("GITHUB_REF_NAME", "").strip()
    if env_branch:
        return env_branch
    try:
        res = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
        return res.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def gh_get_json(url):
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "silo-plugins-catalog-builder",
        },
    )
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def gh_get_text(url):
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "silo-plugins-catalog-builder"},
    )
    tok = os.environ.get("GITHUB_TOKEN")
    if tok:
        req.add_header("Authorization", f"Bearer {tok}")
    with urllib.request.urlopen(req, timeout=20) as r:
        return r.read().decode("utf-8")


def normalize_control_value(ctrl):
    if isinstance(ctrl, int) and not isinstance(ctrl, bool):
        return ctrl
    if isinstance(ctrl, str):
        cleaned = ctrl.strip()
        if cleaned.isdigit():
            return int(cleaned)
        upper = cleaned.upper()
        if upper in CONTROL_ENUM_MAP:
            return CONTROL_ENUM_MAP[upper]
    return ctrl


def normalize_manifest(manifest, binaries, supported, existing_manifest=None):
    if isinstance(existing_manifest, dict):
        for field in PRESERVED_MANIFEST_FIELDS:
            if field not in manifest and field in existing_manifest:
                manifest[field] = existing_manifest[field]

    if "linux/amd64" in binaries and "checksum" in binaries["linux/amd64"]:
        manifest["checksum"] = binaries["linux/amd64"]["checksum"]
    elif not manifest.get("checksum"):
        manifest.pop("checksum", None)

    if supported:
        manifest["supported_platforms"] = sorted(
            supported,
            key=lambda item: (item.get("os", ""), item.get("arch", "")),
        )

    for cap in manifest.get("capabilities", []):
        if cap.get("type") == "http_routes.v1":
            nested_routes = cap.pop("http_routes", None)
            if (
                "http_routes" not in manifest
                and isinstance(nested_routes, dict)
                and isinstance(nested_routes.get("routes"), list)
            ):
                manifest["http_routes"] = nested_routes["routes"]

    for grp in manifest.get("global_config_schema", []):
        form = grp.get("admin_form")
        if isinstance(form, dict):
            for field in form.get("fields", []):
                if "control" in field:
                    field["control"] = normalize_control_value(field["control"])

    for rt in manifest.get("http_routes", []):
        if rt.get("navigation_kind") == "":
            rt.pop("navigation_kind", None)
        if rt.get("navigation_label") == "":
            rt.pop("navigation_label", None)

    return manifest


def load_existing_catalog(path="repository.json"):
    if not os.path.exists(path):
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    by_repo = {}
    for entry in data.get("plugins", []):
        repo_url = entry.get("repo_url")
        if isinstance(repo_url, str) and repo_url:
            by_repo[repo_url] = entry
    return by_repo


def normalize_existing_entry(entry):
    manifest = dict(entry.get("manifest", {}))
    binaries = dict(entry.get("binaries", {}))
    supported = []
    for plat in binaries:
        if "/" in plat:
            os_name, arch_name = plat.split("/", 1)
            supported.append({"os": os_name, "arch": arch_name})
    if not supported and isinstance(manifest.get("supported_platforms"), list):
        supported = list(manifest["supported_platforms"])
    normalized_manifest = normalize_manifest(manifest, binaries, supported)
    return {
        **entry,
        "manifest": normalized_manifest,
        "binaries": {k: binaries[k] for k in sorted(binaries)},
    }


def fetch_latest_release(repo):
    try:
        return gh_get_json(f"https://api.github.com/repos/{repo}/releases/latest")
    except urllib.error.HTTPError as err:
        if err.code != 404:
            raise
    releases = gh_get_json(f"https://api.github.com/repos/{repo}/releases?per_page=20")
    non_draft = [r for r in releases if not r.get("draft")]
    stable = [r for r in non_draft if not r.get("prerelease")]
    if stable:
        return stable[0]
    if non_draft:
        return non_draft[0]
    return None


def main():
    parser = argparse.ArgumentParser(description="Build and normalize Silo plugin repository.json.")
    parser.add_argument(
        "--normalize-only",
        action="store_true",
        help="Normalize existing repository.json in place without fetching remote releases.",
    )
    args = parser.parse_args()

    with open("sources.json", "r", encoding="utf-8") as f:
        repos = json.load(f).get("repos", [])

    existing_by_repo = load_existing_catalog("repository.json")
    branch = detect_git_branch()
    preserve_branch_state = args.normalize_only or (branch not in ("", "main", "HEAD"))
    if preserve_branch_state and not args.normalize_only:
        print(
            f"Current branch '{branch}' is not 'main'; preserving existing repository.json entries and normalizing schema only."
        )

    plugins = []
    for repo in repos:
        repo_url = f"https://github.com/{repo}"
        existing_entry = existing_by_repo.get(repo_url)

        if preserve_branch_state:
            if existing_entry is not None:
                plugins.append(normalize_existing_entry(existing_entry))
            continue

        rel = fetch_latest_release(repo)
        if rel is None:
            if existing_entry is not None:
                plugins.append(normalize_existing_entry(existing_entry))
            continue

        tag = rel["tag_name"]
        release_version = tag.lstrip("v")
        existing_manifest = existing_entry.get("manifest") if isinstance(existing_entry, dict) else None
        existing_version = existing_manifest.get("version") if isinstance(existing_manifest, dict) else None

        if existing_entry is not None and (
            (rel.get("prerelease") and existing_version)
            or is_newer_version(existing_version, release_version)
        ):
            plugins.append(normalize_existing_entry(existing_entry))
            continue

        manifest_raw = gh_get_text(f"https://raw.githubusercontent.com/{repo}/{tag}/manifest.json")
        manifest = json.loads(manifest_raw)
        manifest["version"] = release_version

        assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}
        checksums = {}
        if "SHA256SUMS.txt" in assets:
            for line in gh_get_text(assets["SHA256SUMS.txt"]).splitlines():
                parts = line.strip().split()
                if len(parts) >= 2:
                    checksums[parts[1].lstrip("*")] = parts[0]

        binaries = {}
        supported = []
        for asset_name, dl_url in assets.items():
            for suffix, plat in ASSET_PLATFORM_MAP.items():
                if asset_name.endswith(suffix):
                    entry = {"url": dl_url}
                    if asset_name in checksums:
                        entry["checksum"] = checksums[asset_name]
                    binaries[plat] = entry
                    os_name, arch_name = plat.split("/")
                    supported.append({"os": os_name, "arch": arch_name})

        binaries = {k: binaries[k] for k in sorted(binaries)}
        manifest = normalize_manifest(manifest, binaries, supported, existing_manifest=existing_manifest)

        plugins.append({
            "manifest": manifest,
            "repo_url": repo_url,
            "binaries": binaries,
        })

    out = {"plugins": plugins}
    with open("repository.json", "w", encoding="utf-8") as f:
        json.dump(out, f, indent=2)
        f.write("\n")
    print(f"Generated repository.json with {len(plugins)} plugin(s).")


if __name__ == "__main__":
    main()
