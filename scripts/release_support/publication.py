"""Resumable public-source/tag/asset promotion, with remote reconciliation.

The controller release publishes four assets as Latest; the node release reuses
the same push/tag/draft/upload/verify path with its own tag, five assets and
make_latest=false. A beta or rc of either line is a GitHub pre-release and never
Latest.
"""
from __future__ import annotations

import base64
import urllib.parse
from pathlib import Path

from .common import ReleaseError, clean_environment, sha


def asset_metadata(directory: Path, expected: dict) -> bool:
    return bool(expected) and all((directory / name).is_file()
        and (directory / name).stat().st_size == item["bytes"]
        and sha((directory / name).read_bytes()) == item["sha256"] for name, item in expected.items())


def publish(directory: Path, state, command, github, *, tag=None, name=None, assets=None, latest=True, prerelease=False) -> dict:
    value = state.value
    candidate = value["candidate"]
    source = directory / "source"
    if assets is None:
        assets = value["stages"]["package"]["result"]["assets"]
    commit = candidate["publicCommit"]
    tag = tag or "v" + value["version"]
    title = name or "OpenBear " + tag
    notes = value["notes"]
    latest = latest and not prerelease  # GitHub never makes a pre-release Latest.
    if not asset_metadata(directory / "assets", assets):
        raise ReleaseError("Release asset bytes differ from the accepted ZIP")

    def git(*args, auth=False, check=True):
        env = clean_environment()
        if auth:
            encoded = base64.b64encode(("x-access-token:" + github.token).encode()).decode()
            env.update(GIT_CONFIG_COUNT="2", GIT_CONFIG_KEY_0="http.https://github.com/.extraheader",
                       GIT_CONFIG_VALUE_0="Authorization: Basic " + encoded,
                       GIT_CONFIG_KEY_1="credential.helper", GIT_CONFIG_VALUE_1="", GIT_TERMINAL_PROMPT="0")
        return command(["git", *args], "publish-git", cwd=source, env=env, check=check)

    if git("rev-parse", "HEAD") != commit or git("status", "--porcelain", "--untracked-files=all"):
        raise ReleaseError("Public checkout changed after acceptance")
    value.setdefault("publicationIntent", {"commit": commit, "tag": tag, "notesSha256": sha(notes.encode()), "assets": assets})
    intent = value["publicationIntent"]
    if intent != {"commit": commit, "tag": tag, "notesSha256": sha(notes.encode()), "assets": assets}:
        raise ReleaseError("Publication intent no longer matches accepted inputs", code=2)
    state.save()
    remote = github.api("/git/ref/heads/main")["object"]["sha"]
    if remote not in {value["publicBase"], commit}:
        raise ReleaseError("Public main moved; refusing to overwrite another publisher", code=2)
    remote_release = github.api("/releases/tags/" + tag, missing=True)
    if remote_release and not value.get("releaseCreateIntent"):
        raise ReleaseError("A release for this version already exists and is not owned by this run", code=2)

    existing = git("tag", "--list", tag)
    note_path = directory / "notes.md"
    if not existing:
        git("-c", "user.name=OpenBear Release", "-c", "user.email=release@openbear.invalid", "tag", "-a", tag, commit,
            "--cleanup=verbatim", "-F", str(note_path))
    if git("rev-parse", tag + "^{commit}") != commit:
        raise ReleaseError("Existing local tag points to another candidate", code=2)
    # git helper strips terminal whitespace; compare body through file-format
    # normalization shared with notes, never normalize Markdown itself.
    tag_data = git("cat-file", "-p", tag)
    header, annotation = tag_data.split("\n\n", 1)
    if annotation + "\n" != notes or "tagger OpenBear Release <release@openbear.invalid>" not in header:
        raise ReleaseError("Annotated tag differs from frozen release notes", code=2)
    object_id = git("rev-parse", tag)
    remote_tag = github.api("/git/ref/tags/" + tag, missing=True)
    if remote_tag and remote_tag["object"]["sha"] != object_id:
        raise ReleaseError("Remote tag exists with a different object; never overwrite", code=2)
    if remote != commit:
        git("push", "origin", commit + ":refs/heads/main", auth=True)
        if github.api("/git/ref/heads/main")["object"]["sha"] != commit:
            raise ReleaseError("Cannot confirm public-source push", code=2)
    value["publicSourcePushed"] = True
    state.save()
    if not remote_tag:
        git("push", "origin", "refs/tags/" + tag, auth=True)
    if github.api("/git/ref/tags/" + tag)["object"]["sha"] != object_id:
        raise ReleaseError("Cannot confirm annotated tag push", code=2)
    state.value["tagObject"] = object_id
    state.save()

    release = github.api("/releases/tags/" + tag, missing=True)
    if release is None and value.get("releaseCreateIntent"):
        # The tag endpoint does not serve drafts; an unknown POST outcome is
        # reconciled from the saved ID or the release list, never by a new POST.
        if value.get("releaseId"):
            release = github.api(f"/releases/{value['releaseId']}", missing=True)
        else:
            matches = [item for item in github.api("/releases?per_page=100") or [] if item.get("tag_name") == tag]
            if len(matches) > 1:
                raise ReleaseError("Several releases claim this tag; inspect before resuming", code=2)
            release = matches[0] if matches else None
    if release is None:
        # Persist before the POST: a timed-out response is not permission to
        # create a duplicate. resume first GETs the unique tag's release.
        value["releaseCreateIntent"] = True
        state.save()
        data = {"tag_name": tag, "target_commitish": commit, "name": title, "body": notes, "draft": True,
                "prerelease": prerelease, "generate_release_notes": False}
        if not latest:
            data["make_latest"] = "false"
        release = github.api("/releases", method="POST", data=data)
    elif not value.get("releaseCreateIntent"):
        raise ReleaseError("A release for this version already exists and is not owned by this run", code=2)
    if (release["tag_name"] != tag or release["body"] != notes or bool(release["prerelease"]) != prerelease
            or release.get("target_commitish") != commit):
        raise ReleaseError("Remote release identity/body does not match this run", code=2)
    if value.get("releaseId") not in {None, release["id"]}:
        raise ReleaseError("Release identity changed", code=2)
    value["releaseId"] = release["id"]
    state.save()
    present = {item["name"]: item for item in release["assets"]}
    if len(present) != len(release["assets"]) or set(present) - set(assets):
        raise ReleaseError("Release has foreign or duplicate assets; inspect before resuming", code=2)
    if not release["draft"] and set(present) != set(assets):
        raise ReleaseError("Published release is incomplete; never mutate published assets", code=2)
    for name, expected in assets.items():
        item = present.get(name)
        if item is None:
            value["uploadIntent"] = {"name": name, **expected}
            state.save()
            item = github.request(release["upload_url"].split("{", 1)[0] + "?name=" + urllib.parse.quote(name),
                                  method="POST", data=(directory / "assets" / name).read_bytes())
        if item.get("state") != "uploaded" or item["size"] != expected["bytes"]:
            raise ReleaseError(f"Remote asset differs or has incomplete upload: {name}", code=2)
        content = github.download_asset(item)
        if len(content) != expected["bytes"] or sha(content) != expected["sha256"]:
            raise ReleaseError(f"Downloaded release asset hash mismatch: {name}", code=2)
        value.setdefault("verifiedAssets", {})[name] = {**expected, "assetId": item["id"]}
        value.pop("uploadIntent", None)
        state.save()
    if release["draft"]:
        value["makePublicIntent"] = True
        state.save()
        release = github.api(f"/releases/{release['id']}", method="PATCH",
                             data={"draft": False, "prerelease": prerelease, "make_latest": "true" if latest else "false"})
    if latest:
        public = github.api("/releases/latest")
        if public["id"] != release["id"] or public["tag_name"] != tag or public["draft"] or public["prerelease"] or public["body"] != notes:
            raise ReleaseError("Release public/Latest state is not confirmed; inspect before resuming", code=2)
    else:
        public = github.api(f"/releases/{release['id']}")
        if (public["id"] != release["id"] or public["tag_name"] != tag or public["draft"] or bool(public["prerelease"]) != prerelease
                or public["body"] != notes):
            raise ReleaseError("Release public state is not confirmed; inspect before resuming", code=2)
        top = github.api("/releases/latest", missing=True)
        if top and top.get("id") == release["id"]:
            raise ReleaseError("This release must never be Latest; restore the previous Latest release before resuming", code=2)
    urls = {}
    for item in public["assets"]:
        if item["name"] not in value["verifiedAssets"] or item["id"] != value["verifiedAssets"][item["name"]]["assetId"]:
            raise ReleaseError("Assets changed during publication", code=2)
        if github.request(item["browser_download_url"], method="HEAD", binary=True, authenticated=False) != 200:
            raise ReleaseError("Public asset download is not available")
        urls[item["name"]] = item["browser_download_url"]
    if set(urls) != set(assets):
        raise ReleaseError("Published asset set changed", code=2)
    return {"ok": True, "url": release["html_url"], "latest": latest, "prerelease": prerelease, "localCommit": candidate["localCommit"],
            "publicCommit": commit, "tagObject": object_id, "assets": {name: {**item, "url": urls[name]} for name, item in assets.items()}}
