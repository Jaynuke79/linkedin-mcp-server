#!/usr/bin/env python3
"""MCP server exposing LinkedIn's official Posts API for the authenticated member."""

import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LINKEDIN_VERSION = "202608"
API_ROOT = "https://api.linkedin.com"
PROTOCOL_VERSION = "2025-06-18"
MAX_COMMENTARY = 3000
MAX_ALT_TEXT = 4086
MAX_IMAGES = 20
IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif"}
CONFIG_HOME = Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
TOKEN_PATHS = (
    CONFIG_HOME / "linkedin-mcp" / "token",
    Path(__file__).resolve().parent / ".token",
)
TOKEN_LIFETIME_DAYS = 60

_member_urn = None


def log(msg):
    print(msg, file=sys.stderr, flush=True)


def token_file():
    return next((path for path in TOKEN_PATHS if path.is_file()), None)


def token():
    value = os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip()
    if value:
        return value

    fetch = os.environ.get("LINKEDIN_TOKEN_CMD", "").strip()
    if fetch:
        try:
            result = subprocess.run(
                shlex.split(fetch), capture_output=True, text=True, timeout=30, check=True
            )
        except subprocess.SubprocessError as error:
            raise RuntimeError(f"LINKEDIN_TOKEN_CMD failed: {error}") from None
        value = result.stdout.strip()
        if value:
            return value
        raise RuntimeError("LINKEDIN_TOKEN_CMD produced no output")

    path = token_file()
    if path:
        value = path.read_text().strip()
        if value:
            return value
        raise RuntimeError(f"{path} is empty")

    raise RuntimeError(
        f"No token available. Write one to {TOKEN_PATHS[0]}, set "
        "LINKEDIN_ACCESS_TOKEN, or set LINKEDIN_TOKEN_CMD to a command that "
        "prints the token. "
        "Generate a token at "
        "https://www.linkedin.com/developers/tools/oauth/token-generator "
        "with the w_member_social, openid, and profile scopes."
    )


def call_api(method, path, body=None, versioned=True, extra_headers=None):
    headers = {
        "Authorization": f"Bearer {token()}",
        "X-Restli-Protocol-Version": "2.0.0",
    }
    if versioned:
        headers["LinkedIn-Version"] = LINKEDIN_VERSION
    if extra_headers:
        headers.update(extra_headers)
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"

    request = urllib.request.Request(
        API_ROOT + path, data=data, headers=headers, method=method
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            payload = response.read().decode() or "{}"
            return response.status, dict(response.headers), json.loads(payload)
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:800]
        raise RuntimeError(f"LinkedIn API {error.code}: {detail}") from None


def member_urn():
    global _member_urn
    if _member_urn is None:
        _, _, info = call_api("GET", "/v2/userinfo", versioned=False)
        _member_urn = f"urn:li:person:{info['sub']}"
    return _member_urn


def to_wsl_path(raw):
    match = re.fullmatch(r"([A-Za-z]):[\\/](.*)", raw.strip())
    if not match:
        return raw.strip()
    drive, remainder = match.groups()
    return f"/mnt/{drive.lower()}/{remainder.replace(chr(92), '/')}"


def resolve_image(entry):
    if isinstance(entry, str):
        entry = {"path": entry}
    raw = (entry.get("path") or "").strip()
    if not raw:
        raise RuntimeError("each image needs a path")

    path = Path(to_wsl_path(raw)).expanduser()
    if not path.is_file():
        raise RuntimeError(f"no such image file: {path}")
    if path.suffix.lower() not in IMAGE_SUFFIXES:
        raise RuntimeError(
            f"{path.name} is not a JPG, PNG, or GIF; LinkedIn accepts only those"
        )

    alt_text = (entry.get("alt_text") or "").strip()
    if len(alt_text) > MAX_ALT_TEXT:
        raise RuntimeError(f"alt_text for {path.name} exceeds {MAX_ALT_TEXT} characters")
    return path, alt_text


def put_bytes(url, payload):
    request = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token()}",
            "Content-Type": "application/octet-stream",
        },
        method="PUT",
    )
    try:
        with urllib.request.urlopen(request, timeout=180) as response:
            return response.status
    except urllib.error.HTTPError as error:
        detail = error.read().decode(errors="replace")[:400]
        raise RuntimeError(f"image upload rejected with {error.code}: {detail}") from None


def upload_image(path):
    _, _, payload = call_api(
        "POST",
        "/rest/images?action=initializeUpload",
        {"initializeUploadRequest": {"owner": member_urn()}},
    )
    registration = payload["value"]
    put_bytes(registration["uploadUrl"], path.read_bytes())
    return registration["image"]


def image_content(uploaded):
    if len(uploaded) == 1:
        urn, alt_text = uploaded[0]
        media = {"id": urn}
        if alt_text:
            media["altText"] = alt_text
        return {"media": media}

    images = []
    for urn, alt_text in uploaded:
        entry = {"id": urn}
        if alt_text:
            entry["altText"] = alt_text
        images.append(entry)
    return {"multiImage": {"images": images}}


def tool_whoami(_args):
    _, _, info = call_api("GET", "/v2/userinfo", versioned=False)
    return json.dumps(
        {
            "name": info.get("name"),
            "author_urn": f"urn:li:person:{info['sub']}",
        },
        indent=2,
    )


def token_source():
    if os.environ.get("LINKEDIN_ACCESS_TOKEN", "").strip():
        return "LINKEDIN_ACCESS_TOKEN"
    if os.environ.get("LINKEDIN_TOKEN_CMD", "").strip():
        return "LINKEDIN_TOKEN_CMD"
    path = token_file()
    return str(path) if path else "none"


def tool_check_access(_args):
    report = {"token_source": token_source()}

    path = token_file()
    if path and report["token_source"] == str(path):
        age = (time.time() - path.stat().st_mtime) / 86400
        report["token_age_days"] = round(age, 1)
        report["expires_in_days"] = round(TOKEN_LIFETIME_DAYS - age, 1)

    try:
        _, _, info = call_api("GET", "/v2/userinfo", versioned=False)
    except Exception as error:
        report["read_access"] = f"failed: {error}"
        report["verdict"] = (
            "The token is invalid or expired. Regenerate it at "
            "https://www.linkedin.com/developers/tools/oauth/token-generator"
        )
        return json.dumps(report, indent=2)

    report["read_access"] = "ok"
    report["name"] = info.get("name")
    report["author_urn"] = f"urn:li:person:{info['sub']}"

    try:
        call_api(
            "POST",
            "/rest/images?action=initializeUpload",
            {"initializeUploadRequest": {"owner": report["author_urn"]}},
        )
    except Exception as error:
        report["write_access"] = f"failed: {error}"
        report["verdict"] = (
            "Read works but writing does not, so posting will fail. The token is "
            "missing the w_member_social scope, which means the app does not hold "
            "the Share on LinkedIn product. Add it on the app's Products tab, then "
            "regenerate the token. The scope list in LinkedIn's token generator is "
            "authoritative: if w_member_social is not offered as a checkbox there, "
            "the product is not on the app yet."
        )
        return json.dumps(report, indent=2)

    report["write_access"] = "ok"
    report["verdict"] = "Ready to post."
    return json.dumps(report, indent=2)


def tool_create_post(args):
    text = args.get("text", "").strip()
    if not text:
        raise RuntimeError("text is required and cannot be empty")
    if len(text) > MAX_COMMENTARY:
        raise RuntimeError(
            f"text is {len(text)} characters; LinkedIn allows {MAX_COMMENTARY}"
        )

    visibility = args.get("visibility", "PUBLIC").upper()
    if visibility not in ("PUBLIC", "CONNECTIONS"):
        raise RuntimeError("visibility must be PUBLIC or CONNECTIONS")

    requested = args.get("images") or []
    if len(requested) > MAX_IMAGES:
        raise RuntimeError(f"{len(requested)} images requested; LinkedIn allows {MAX_IMAGES}")
    resolved = [resolve_image(entry) for entry in requested]

    body = {
        "author": member_urn(),
        "commentary": text,
        "visibility": visibility,
        "distribution": {
            "feedDistribution": "MAIN_FEED",
            "targetEntities": [],
            "thirdPartyDistributionChannels": [],
        },
        "lifecycleState": "PUBLISHED",
        "isReshareDisabledByAuthor": bool(args.get("disable_reshare", False)),
    }

    uploaded = [(upload_image(path), alt_text) for path, alt_text in resolved]
    if uploaded:
        body["content"] = image_content(uploaded)

    _, headers, _ = call_api("POST", "/rest/posts", body)
    urn = next((v for k, v in headers.items() if k.lower() == "x-restli-id"), "unknown")
    return json.dumps(
        {
            "post_urn": urn,
            "url": f"https://www.linkedin.com/feed/update/{urn}/",
            "visibility": visibility,
            "images": [urn for urn, _ in uploaded],
        },
        indent=2,
    )


def post_path(urn):
    if not urn.startswith("urn:li:"):
        raise RuntimeError("post_urn must look like urn:li:share:… or urn:li:ugcPost:…")
    return f"/rest/posts/{urllib.parse.quote(urn, safe='')}"


def tool_edit_post(args):
    path = post_path(args.get("post_urn", "").strip())

    text = args.get("text", "").strip()
    if not text:
        raise RuntimeError("text is required and cannot be empty")
    if len(text) > MAX_COMMENTARY:
        raise RuntimeError(
            f"text is {len(text)} characters; LinkedIn allows {MAX_COMMENTARY}"
        )

    call_api(
        "POST",
        path,
        {"patch": {"$set": {"commentary": text}}},
        extra_headers={"X-RestLi-Method": "PARTIAL_UPDATE"},
    )
    urn = args["post_urn"].strip()
    return json.dumps(
        {
            "edited": urn,
            "url": f"https://www.linkedin.com/feed/update/{urn}/",
            "note": "LinkedIn shows edited posts as edited to everyone who sees them.",
        },
        indent=2,
    )


def tool_delete_post(args):
    urn = args.get("post_urn", "").strip()
    call_api(
        "DELETE",
        post_path(urn),
        extra_headers={"X-RestLi-Method": "DELETE"},
    )
    return json.dumps({"deleted": urn}, indent=2)


TOOLS = {
    "linkedin_whoami": {
        "handler": tool_whoami,
        "description": (
            "Verify the LinkedIn access token and return the authenticated member's "
            "name, email, and author URN. Use this to confirm setup before posting."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "linkedin_check_access": {
        "handler": tool_check_access,
        "description": (
            "Check that posting will actually work, before relying on it. Verifies "
            "read access and, separately, write access, and reports how many days "
            "are left on the token. Prefer this over linkedin_whoami when "
            "diagnosing: whoami passes on a read-only token while every write "
            "fails. Publishes nothing, but does register one throwaway image "
            "upload that is never referenced and simply expires."
        ),
        "inputSchema": {"type": "object", "properties": {}, "additionalProperties": False},
    },
    "linkedin_create_post": {
        "handler": tool_create_post,
        "description": (
            "Publish a post to the authenticated member's own LinkedIn feed, with "
            "optional images. This is immediately visible to the chosen audience; "
            "always confirm the exact wording with the user first."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "text": {
                    "type": "string",
                    "description": f"Post body, max {MAX_COMMENTARY} characters. Mention an organization with @[Name](urn:li:organization:123).",
                },
                "visibility": {
                    "type": "string",
                    "enum": ["PUBLIC", "CONNECTIONS"],
                    "description": "Audience for the post. Defaults to PUBLIC.",
                },
                "disable_reshare": {
                    "type": "boolean",
                    "description": "Prevent others from resharing. Defaults to false.",
                },
                "images": {
                    "type": "array",
                    "maxItems": MAX_IMAGES,
                    "description": (
                        "Up to 20 JPG, PNG, or GIF files to attach. One image renders "
                        "as a single image post; two or more render as a swipeable "
                        "carousel. Windows paths are accepted and translated."
                    ),
                    "items": {
                        "type": "object",
                        "properties": {
                            "path": {
                                "type": "string",
                                "description": "Absolute path to the image file, e.g. /home/me/chart.png or C:\\Users\\me\\chart.png",
                            },
                            "alt_text": {
                                "type": "string",
                                "description": "Screen-reader description. Recommended under 120 characters.",
                            },
                        },
                        "required": ["path"],
                        "additionalProperties": False,
                    },
                },
            },
            "required": ["text"],
            "additionalProperties": False,
        },
    },
    "linkedin_edit_post": {
        "handler": tool_edit_post,
        "description": (
            "Replace the text of a post already published by the authenticated "
            "member. Only the text can be changed: LinkedIn does not allow images "
            "to be added, removed, or swapped after publishing, so a wrong image "
            "means deleting and reposting. The edit is immediately visible, and "
            "LinkedIn labels edited posts as edited to everyone who sees them."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "post_urn": {
                    "type": "string",
                    "description": "URN of the post to edit, e.g. urn:li:share:6844785523593134080",
                },
                "text": {
                    "type": "string",
                    "description": f"Replacement body, max {MAX_COMMENTARY} characters. This replaces the existing text entirely rather than appending to it.",
                },
            },
            "required": ["post_urn", "text"],
            "additionalProperties": False,
        },
    },
    "linkedin_delete_post": {
        "handler": tool_delete_post,
        "description": "Delete one of the authenticated member's posts by its URN.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "post_urn": {
                    "type": "string",
                    "description": "URN returned by linkedin_create_post, e.g. urn:li:share:6844785523593134080",
                }
            },
            "required": ["post_urn"],
            "additionalProperties": False,
        },
    },
}


def handle(method, params):
    if method == "initialize":
        requested = params.get("protocolVersion", PROTOCOL_VERSION)
        return {
            "protocolVersion": requested,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "linkedin-post", "version": "1.4.0"},
        }

    if method == "tools/list":
        return {
            "tools": [
                {
                    "name": name,
                    "description": spec["description"],
                    "inputSchema": spec["inputSchema"],
                }
                for name, spec in TOOLS.items()
            ]
        }

    if method == "tools/call":
        name = params.get("name")
        if name not in TOOLS:
            raise RuntimeError(f"unknown tool: {name}")
        try:
            text = TOOLS[name]["handler"](params.get("arguments") or {})
            return {"content": [{"type": "text", "text": text}]}
        except Exception as error:
            return {
                "content": [{"type": "text", "text": str(error)}],
                "isError": True,
            }

    if method == "ping":
        return {}

    raise LookupError(method)


def main():
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            log("dropped unparseable line")
            continue

        message_id = message.get("id")
        if message_id is None:
            continue

        try:
            result = handle(message.get("method"), message.get("params") or {})
            response = {"jsonrpc": "2.0", "id": message_id, "result": result}
        except LookupError as error:
            response = {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32601, "message": f"method not found: {error}"},
            }
        except Exception as error:
            response = {
                "jsonrpc": "2.0",
                "id": message_id,
                "error": {"code": -32603, "message": str(error)},
            }

        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()


if __name__ == "__main__":
    main()
