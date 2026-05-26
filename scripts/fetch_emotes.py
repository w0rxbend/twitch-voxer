#!/usr/bin/env python3
"""One-shot script to fetch and cache Twitch emotes into a local pickledb file.

Run this once (or periodically) to populate emotes/emotes.db, which the bot
reads at startup to resolve Twitch emote names to image URLs for the overlay.

What it collects:
  - Global Twitch emotes (available in every channel)
  - Channel emotes for all channels the authenticated user follows
  - Channel emotes for all channels that follow the authenticated user

The result is a pickledb file where each key is an emote name and the value
is {"url_1x": "...", "url_2x": "...", "url_4x": "..."}.

Authentication:
  The script needs both an app token (client credentials flow, for most API
  calls) and a user token with `user:read:follows` and `moderator:read:followers`
  scopes (to list followed/follower channels).  It tries to refresh an existing
  token from TWITCH_REFRESH_TOKEN first; if that fails or the token lacks the
  required scopes it runs a local OAuth callback server to get a fresh one.

Usage:
    uv run voxer-fetch-emotes   (after `uv sync`)
    python scripts/fetch_emotes.py
"""

import http.server
import os
import secrets
import time
import urllib.parse
import webbrowser
import pickledb
import requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()


def _require_env(key: str) -> str:
    """Read a required environment variable, raising clearly if it is absent."""
    value = os.environ.get(key)
    if not value:
        raise RuntimeError(f"Required environment variable {key!r} is not set")
    return value


# ── Configuration ─────────────────────────────────────────────────────────────

CLIENT_ID: str      = _require_env("TWITCH_CLIENT_ID")
CLIENT_SECRET: str  = _require_env("TWITCH_CLIENT_SECRET")
# Optional: if set, the script tries to refresh this token before the full OAuth flow
REFRESH_TOKEN: str | None = os.environ.get("TWITCH_REFRESH_TOKEN")

BASE_URL = "https://api.twitch.tv/helix"
# The local redirect URI that Twitch sends the authorization code to.
# Must match one of the OAuth Redirect URLs registered in the Twitch Dev Console.
REDIRECT_URI = "http://localhost:1337/api/connect/twitch/callback"
# Minimum scopes needed to list followed and follower channels
SCOPES = ["user:read:follows", "moderator:read:followers"]
OUTPUT_FILE = Path("emotes/emotes.db")


# ── Authentication helpers ────────────────────────────────────────────────────

def get_app_token(session: requests.Session) -> str:
    """Obtain a client-credentials app token (no user context).

    Used for global emote and channel emote fetches which don't require
    a specific user's permission.
    """
    resp = session.post(
        "https://id.twitch.tv/oauth2/token",
        params={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "grant_type": "client_credentials"},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def refresh_user_token(session: requests.Session) -> str | None:
    """Try to exchange the stored refresh token for a new access token.

    Returns None (instead of raising) if the refresh token is missing or expired,
    so the caller can fall back to the full OAuth flow.
    """
    if not REFRESH_TOKEN:
        return None
    try:
        resp = session.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "grant_type": "refresh_token",
                "refresh_token": REFRESH_TOKEN,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]
    except requests.HTTPError:
        return None


def validate_token_scopes(session: requests.Session, token: str) -> set[str]:
    """Return the set of scopes granted to token, or an empty set on failure."""
    resp = session.get(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {token}"},
    )
    if resp.status_code != 200:
        return set()
    return set(resp.json().get("scopes", []))


def oauth_flow(session: requests.Session) -> str:
    """Run the Authorization Code flow and return a fresh user access token.

    Steps:
      1. Build an authorization URL with a random `state` parameter (CSRF guard).
      2. Open the browser at that URL.
      3. Spin up a minimal HTTP server on localhost:1337 that captures the
         `code` query parameter from Twitch's redirect.
      4. Exchange the code for tokens via POST /token.

    The local server polls with a 1-second timeout so the while-loop checks
    `code_holder` without blocking indefinitely.  The flow times out after 120 s.
    """
    # state is a random hex string used to verify that the redirect came from Twitch
    state = secrets.token_hex(16)
    scope_str = urllib.parse.quote(" ".join(SCOPES))
    auth_url = (
        f"https://id.twitch.tv/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&response_type=code"
        f"&scope={scope_str}"
        f"&state={state}"
        f"&force_verify=true"  # always show the authorization page, even if already approved
    )

    # Shared dict used by the HTTP handler to pass the code back to this scope
    code_holder: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            # Extract the `code` parameter from the redirect URL query string
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in params:
                code_holder["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authorized! You can close this tab.</h1>")

        def log_message(self, *_: object) -> None:
            # Suppress the default per-request log lines from BaseHTTPRequestHandler
            pass

    server = http.server.HTTPServer(("localhost", 1337), Handler)
    server.timeout = 1  # handle_request() returns after 1 s even if no request arrived

    print("\nOpening browser for Twitch authorization...")
    print(f"If the browser does not open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    deadline = time.time() + 120
    while "code" not in code_holder and time.time() < deadline:
        server.handle_request()
    server.server_close()

    code = code_holder.get("code")
    if not code:
        raise RuntimeError("OAuth timed out or was cancelled.")

    # Exchange the authorization code for an access token
    resp = session.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": REDIRECT_URI,
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def get_user_token(session: requests.Session) -> str:
    """Return a valid user token with the required scopes.

    Strategy:
      1. Attempt to refresh the stored TWITCH_REFRESH_TOKEN.
      2. If the refreshed token is valid and has all required scopes, return it.
      3. Otherwise run the full OAuth flow in the browser.
    """
    token = refresh_user_token(session)
    if token:
        scopes = validate_token_scopes(session, token)
        needed = set(SCOPES)
        if needed.issubset(scopes):
            return token
        missing = needed - scopes
        print(f"  Refreshed token is missing scopes: {missing}. Running OAuth flow...")
    else:
        print("  Could not refresh token. Running OAuth flow...")
    return oauth_flow(session)


# ── Twitch API helpers ────────────────────────────────────────────────────────

def hdrs(token: str) -> dict:
    """Build the standard Twitch API request headers for the given token."""
    return {"Client-Id": CLIENT_ID, "Authorization": f"Bearer {token}"}


def get_current_user(session: requests.Session, token: str) -> dict:
    """Return the authenticated user's Twitch profile dict."""
    resp = session.get(f"{BASE_URL}/users", headers=hdrs(token))
    resp.raise_for_status()
    return resp.json()["data"][0]


def paginate(session: requests.Session, url: str, token: str, params: dict) -> list[dict]:
    """Fetch all pages from a cursor-paginated Twitch API endpoint.

    Twitch paginates results using a cursor returned in `pagination.cursor`.
    This function loops until no cursor is returned or the page is empty.
    """
    results: list[dict] = []
    cursor = None
    while True:
        # Merge the `after` cursor into params only when one exists
        p = {**params, **({"after": cursor} if cursor else {})}
        resp = session.get(url, headers=hdrs(token), params=p)
        resp.raise_for_status()
        data = resp.json()
        batch = data.get("data", [])
        results.extend(batch)
        cursor = data.get("pagination", {}).get("cursor")
        if not cursor or not batch:
            break
    return results


def fetch_followed_ids(session: requests.Session, token: str, user_id: str) -> list[str]:
    """Return broadcaster IDs for all channels the user follows."""
    items = paginate(session, f"{BASE_URL}/channels/followed", token, {"user_id": user_id, "first": 100})
    return [i["broadcaster_id"] for i in items]


def fetch_follower_ids(session: requests.Session, token: str, broadcaster_id: str) -> list[str]:
    """Return user IDs for all followers of the given broadcaster."""
    items = paginate(session, f"{BASE_URL}/channels/followers", token, {"broadcaster_id": broadcaster_id, "first": 100})
    return [i["user_id"] for i in items]


def fetch_global_emotes(session: requests.Session, token: str) -> list[dict]:
    """Return all global Twitch emotes (available in every channel)."""
    resp = session.get(f"{BASE_URL}/chat/emotes/global", headers=hdrs(token))
    resp.raise_for_status()
    return resp.json()["data"]


def fetch_channel_emotes(session: requests.Session, token: str, broadcaster_id: str) -> list[dict]:
    """Return channel-specific emotes for the given broadcaster.

    Returns an empty list for channels that have no emotes or don't exist (400/404),
    rather than raising, so a single missing channel doesn't abort the whole fetch.
    """
    resp = session.get(f"{BASE_URL}/chat/emotes", headers=hdrs(token), params={"broadcaster_id": broadcaster_id})
    if resp.status_code in (400, 404):
        return []
    resp.raise_for_status()
    return resp.json()["data"]


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    """Fetch all emotes and write them to emotes/emotes.db.

    The output file maps emote name → {url_1x, url_2x, url_4x}.
    Duplicate emote names (same name in multiple channels) are deduplicated
    by keeping the first occurrence — typically the global version.
    """
    OUTPUT_FILE.parent.mkdir(exist_ok=True)

    with requests.Session() as session:
        print("Getting app token...")
        app_token = get_app_token(session)

        print("Getting user token...")
        user_token = get_user_token(session)

        print("Getting current user...")
        user = get_current_user(session, user_token)
        user_id, login = user["id"], user["login"]
        print(f"  Logged in as: {login} ({user_id})")

        print("Fetching global emotes...")
        global_emotes = fetch_global_emotes(session, app_token)
        print(f"  {len(global_emotes)} global emotes")

        print("Fetching followed channels...")
        followed_ids = fetch_followed_ids(session, user_token, user_id)
        print(f"  Following {len(followed_ids)} channels")

        print("Fetching follower channels...")
        follower_ids = fetch_follower_ids(session, user_token, user_id)
        print(f"  {len(follower_ids)} followers")

        # Union of followed + follower channels; channels in both sets are deduplicated
        all_channel_ids = list(set(followed_ids + follower_ids))
        print(f"\nFetching emotes for {len(all_channel_ids)} unique channels...")

        channel_emotes: list[dict] = []
        for i, cid in enumerate(all_channel_ids, 1):
            emotes = fetch_channel_emotes(session, app_token, cid)
            channel_emotes.extend(emotes)
            if i % 20 == 0:
                # Progress checkpoint + brief sleep to stay within Twitch rate limits
                print(f"  {i}/{len(all_channel_ids)} done ({len(channel_emotes)} emotes so far)...")
                time.sleep(0.3)

        print(f"  {len(channel_emotes)} channel emotes from {len(all_channel_ids)} channels")

        print(f"\nWriting to {OUTPUT_FILE}...")
        seen: set[str] = set()
        with pickledb.PickleDB(str(OUTPUT_FILE)) as db:
            for emote in global_emotes + channel_emotes:
                name = emote["name"]
                if name in seen:
                    continue  # keep the first occurrence (usually the global emote)
                seen.add(name)
                imgs = emote["images"]
                db.set(name, {"url_1x": imgs["url_1x"], "url_2x": imgs["url_2x"], "url_4x": imgs["url_4x"]})

        print(f"Saved to {OUTPUT_FILE} ({len(seen)} unique emotes)")


if __name__ == "__main__":
    main()
