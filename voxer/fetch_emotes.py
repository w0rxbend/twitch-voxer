"""One-shot script to fetch and cache Twitch emotes into a local pickledb file.

Run this once (or periodically) to populate the emote cache at
VOXER_EMOTES_DB_PATH (default emotes/emotes.db), which the bot reads at
startup to resolve Twitch emote names to image URLs for the overlay.

What it collects:
  - Global Twitch emotes (available in every channel)
  - Channel emotes for all channels the authenticated user follows
  - Channel emotes for all channels that follow the authenticated user

The result is a pickledb file where each key is an emote name and the value
is {"url_1x": "...", "url_2x": "...", "url_4x": "..."}.

Authentication:
  The script needs both an app token (client credentials flow, for most API
  calls) and a user token with `user:read:follows` and `moderator:read:followers`
  scopes (to list followed/follower channels).  In order it tries:
    1. The bot's shared token file (VOXER_TOKEN_FILE, default data/tokens.json)
       written by the main app's OAuth flow — the stored access token is used
       while still valid; only a dead one is refreshed, with the rotated pair
       written back atomically so it stays usable by the bot.
    2. The TWITCH_REFRESH_TOKEN env var, if set.
    3. A local OAuth callback server plus browser flow as the last resort.

Usage:
    uv run voxer-fetch-emotes   (after `uv sync`)
    python -m voxer.fetch_emotes

This module is part of the `voxer` package (it reads credentials from
voxer.config), so it must be run with `-m` rather than as a bare file path —
`python voxer/fetch_emotes.py` cannot resolve the package-relative import.
"""

import datetime
import http.server
import json
import os
import secrets
import time
import urllib.parse
import webbrowser
import pickledb
import requests
from pathlib import Path

from . import config


# ── Configuration ─────────────────────────────────────────────────────────────

# Credentials come from voxer.config, which is the single place that reads .env
# and owns the variable names.  They are read leniently there (defaulting to "")
# so this module imports cleanly in tests; main() re-checks them via
# config.validate_credentials() to fail fast with a clear message.
CLIENT_ID: str = config.CLIENT_ID
CLIENT_SECRET: str = config.CLIENT_SECRET
# Optional: if set, the script tries to refresh this token before the full OAuth
# flow.  Empty string when unset, so every check on it must be a truthiness test.
REFRESH_TOKEN: str = config.REFRESH_TOKEN

BASE_URL = "https://api.twitch.tv/helix"
# The local redirect URI that Twitch sends the authorization code to.
# Must match one of the OAuth Redirect URLs registered in the Twitch Dev Console.
REDIRECT_URI = "http://localhost:1337/api/connect/twitch/callback"
# Minimum scopes needed to list followed and follower channels
SCOPES = ["user:read:follows", "moderator:read:followers"]
# Where the emote cache is written.  This must be the same file the bot reads
# (app.py builds its EmoteStore from config.EMOTES_DB_PATH), so it is derived
# from config rather than hardcoded here: if it were hardcoded, anyone setting
# VOXER_EMOTES_DB_PATH would have this script write one file and the bot read
# another, and EmoteStore treats a missing file as "start empty" with only a
# warning — so the only symptom would be emotes silently never appearing.
# config.EMOTES_DB_PATH is a str; this module treats it as a Path throughout.
OUTPUT_FILE = Path(config.EMOTES_DB_PATH)
# The main app persists its OAuth tokens here (twitchio JSON format:
# {user_id: {"user_id", "token", "refresh", "last_validated"}}).  Reusing it
# means this script needs no OAuth flow of its own once the bot has run once.
# config.TOKEN_FILE is a str; this module treats it as a Path throughout.
TOKEN_FILE = Path(config.TOKEN_FILE)


# ── Authentication helpers ────────────────────────────────────────────────────


def get_app_token(session: requests.Session) -> str:
    """Obtain a client-credentials app token (no user context).

    Used for global emote and channel emote fetches which don't require
    a specific user's permission.
    """
    resp = session.post(
        "https://id.twitch.tv/oauth2/token",
        params={
            "client_id": CLIENT_ID,
            "client_secret": CLIENT_SECRET,
            "grant_type": "client_credentials",
        },
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def _refresh_grant(session: requests.Session, refresh_token: str) -> dict | None:
    """Exchange a refresh token for a new token pair via POST /oauth2/token.

    Returns the token-endpoint response dict ({"access_token", "refresh_token",
    ...}) or None when the refresh token is expired or revoked.
    """
    try:
        resp = session.post(
            "https://id.twitch.tv/oauth2/token",
            params={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": CLIENT_ID,
                "client_secret": CLIENT_SECRET,
            },
        )
        resp.raise_for_status()
        return resp.json()
    except requests.HTTPError:
        return None


def _write_tokens_atomically(tokens: dict, fresh_refresh: str) -> None:
    """Replace TOKEN_FILE with the rotated pair without risking a truncated file.

    Writes to a temp file in the same directory, fsyncs, then os.replace()s it
    over the original — a crash mid-write can therefore never destroy the only
    copy of the credentials.  If even that fails, the fresh refresh token is
    printed so it can be recovered manually.
    """
    tmp = TOKEN_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w") as fp:
            json.dump(tokens, fp)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp, TOKEN_FILE)
    except OSError as exc:
        print(f"  Warning: could not write rotated tokens back to {TOKEN_FILE}: {exc}")
        print("  Save this refresh token manually or re-run the bot's OAuth flow:")
        print(f"    {fresh_refresh}")


def refresh_from_token_file(
    session: requests.Session, needed: set[str] | None = None
) -> str | None:
    """Obtain a valid access token from the bot's shared token file.

    The stored access token is reused as-is while it is still alive AND carries
    the scopes this script needs — refreshing would needlessly rotate the
    refresh token and, if the bot is running at the same time, strand the pair
    the bot holds in memory.  A dead or under-scoped access token triggers a
    refresh, in which case the rotated pair is written back atomically
    (otherwise the bot's next refresh would fail with a stale token).

    Args:
        session: HTTP session used for the validate/refresh calls.
        needed: Scopes the returned token must carry.  Defaults to SCOPES.

    Returns:
        The access token, or None when the file is missing, unreadable, or
        every stored credential is dead.
    """
    needed = set(SCOPES) if needed is None else needed
    try:
        tokens: dict[str, dict] = json.loads(TOKEN_FILE.read_text())
    except FileNotFoundError, ValueError, OSError:
        return None
    if not isinstance(tokens, dict):
        return None

    for user_id, entry in tokens.items():
        if not isinstance(entry, dict):
            continue
        # Prefer the stored access token while it is still usable — no rotation.
        # It must carry every needed scope: a token that merely validates would
        # otherwise be returned here and then rejected by the caller's own check.
        stored = entry.get("token")
        if stored and token_has_scopes(session, stored, needed):
            return stored
        refresh = entry.get("refresh")
        if not refresh:
            continue
        fresh = _refresh_grant(session, refresh)
        if not fresh:
            continue
        tokens[user_id] = {
            "user_id": user_id,
            "token": fresh["access_token"],
            "refresh": fresh["refresh_token"],
            "last_validated": datetime.datetime.now().isoformat(),
        }
        _write_tokens_atomically(tokens, fresh["refresh_token"])
        return fresh["access_token"]
    return None


def refresh_user_token(session: requests.Session) -> str | None:
    """Try to exchange the stored refresh token for a new access token.

    Returns None (instead of raising) if the refresh token is missing or expired,
    so the caller can fall back to the full OAuth flow.
    """
    if not REFRESH_TOKEN:
        return None
    fresh = _refresh_grant(session, REFRESH_TOKEN)
    return fresh["access_token"] if fresh else None


def validate_token_scopes(session: requests.Session, token: str) -> set[str]:
    """Return the set of scopes granted to token, or an empty set on failure."""
    resp = session.get(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {token}"},
    )
    if resp.status_code != 200:
        return set()
    return set(resp.json().get("scopes", []))


def token_has_scopes(session: requests.Session, token: str, needed: set[str]) -> bool:
    """Return True when token is valid AND carries every scope in needed.

    A token that validates but lacks a scope is useless to this script, so the
    two questions ("is it alive?" and "can it do what we need?") are answered
    together rather than letting a live-but-under-scoped token pass as good.
    """
    return needed.issubset(validate_token_scopes(session, token))


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
            # Extract the `code` parameter from the redirect URL query string.
            # The `state` must match the one we generated — this is the CSRF
            # (cross-site request forgery) guard: without the check, any page in
            # the browser could hit localhost and inject an attacker's code.
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if params.get("state", [None])[0] != state:
                self.send_response(400)
                self.end_headers()
                self.wfile.write(b"<h1>Invalid state parameter.</h1>")
                return
            if "code" in params:
                code_holder["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authorized! You can close this tab.</h1>")

        def log_message(self, format: str, *args: object) -> None:
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
      1. Refresh from the bot's shared token file (written by the main app's
         OAuth flow — its scope set includes everything this script needs).
      2. Attempt to refresh the stored TWITCH_REFRESH_TOKEN env var.
      3. Otherwise run the full OAuth flow in the browser.
    A refreshed token is used only when it carries all required scopes.
    """
    needed = set(SCOPES)
    # Lazy callables: a later source must not be refreshed (Twitch rotates the
    # refresh token on every use) when an earlier one already succeeded.
    sources = (
        (f"token file {TOKEN_FILE}", refresh_from_token_file),
        ("TWITCH_REFRESH_TOKEN", refresh_user_token),
    )
    for source, fetch in sources:
        token = fetch(session)
        if not token:
            continue
        scopes = validate_token_scopes(session, token)
        if needed.issubset(scopes):
            print(f"  Using token refreshed from {source}")
            return token
        print(f"  Token from {source} is missing scopes: {needed - scopes}")
    print("  No reusable token found. Running OAuth flow...")
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


def paginate(
    session: requests.Session, url: str, token: str, params: dict
) -> list[dict]:
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


def fetch_followed_ids(
    session: requests.Session, token: str, user_id: str
) -> list[str]:
    """Return broadcaster IDs for all channels the user follows."""
    items = paginate(
        session,
        f"{BASE_URL}/channels/followed",
        token,
        {"user_id": user_id, "first": 100},
    )
    return [i["broadcaster_id"] for i in items]


def fetch_follower_ids(
    session: requests.Session, token: str, broadcaster_id: str
) -> list[str]:
    """Return user IDs for all followers of the given broadcaster."""
    items = paginate(
        session,
        f"{BASE_URL}/channels/followers",
        token,
        {"broadcaster_id": broadcaster_id, "first": 100},
    )
    return [i["user_id"] for i in items]


def fetch_global_emotes(session: requests.Session, token: str) -> list[dict]:
    """Return all global Twitch emotes (available in every channel)."""
    resp = session.get(f"{BASE_URL}/chat/emotes/global", headers=hdrs(token))
    resp.raise_for_status()
    return resp.json()["data"]


def fetch_channel_emotes(
    session: requests.Session, token: str, broadcaster_id: str
) -> list[dict]:
    """Return channel-specific emotes for the given broadcaster.

    Returns an empty list for channels that have no emotes or don't exist (400/404),
    rather than raising, so a single missing channel doesn't abort the whole fetch.
    """
    resp = session.get(
        f"{BASE_URL}/chat/emotes",
        headers=hdrs(token),
        params={"broadcaster_id": broadcaster_id},
    )
    if resp.status_code in (400, 404):
        return []
    resp.raise_for_status()
    return resp.json()["data"]


# ── Entry point ───────────────────────────────────────────────────────────────


def main() -> None:
    """Fetch all emotes and write them to OUTPUT_FILE (VOXER_EMOTES_DB_PATH).

    The output file maps emote name → {url_1x, url_2x, url_4x}.
    Duplicate emote names (same name in multiple channels) are deduplicated
    by keeping the first occurrence — typically the global version.
    """
    # Only the two application credentials — deliberately not the bot's whole
    # configuration.  config.validate_config() would additionally require
    # TWITCH_BOT_USERNAME and a working OAuth redirect URL, and this script
    # uses neither: it talks to the Twitch API directly and, when it needs a
    # user token, runs its own local callback server on REDIRECT_URI above.
    config.validate_credentials()
    # parents=True because the path is configurable: a value such as
    # VOXER_EMOTES_DB_PATH=/data/voxer/emotes/emotes.db nests more than one
    # level deep, and without it the very first run on a fresh volume would
    # fail with FileNotFoundError after doing all the downloading.
    OUTPUT_FILE.parent.mkdir(parents=True, exist_ok=True)

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
                print(
                    f"  {i}/{len(all_channel_ids)} done ({len(channel_emotes)} emotes so far)..."
                )
                time.sleep(0.3)

        print(
            f"  {len(channel_emotes)} channel emotes from {len(all_channel_ids)} channels"
        )

        print(f"\nWriting to {OUTPUT_FILE}...")
        seen: set[str] = set()
        with pickledb.PickleDB(str(OUTPUT_FILE)) as db:
            for emote in global_emotes + channel_emotes:
                name = emote["name"]
                if name in seen:
                    continue  # keep the first occurrence (usually the global emote)
                seen.add(name)
                imgs = emote["images"]
                # pickledb's set() is a dual sync/async method; outside an event
                # loop it runs synchronously, so the "coroutine" is never awaited
                _ = db.set(
                    name,
                    {
                        "url_1x": imgs["url_1x"],
                        "url_2x": imgs["url_2x"],
                        "url_4x": imgs["url_4x"],
                    },
                )

        print(f"Saved to {OUTPUT_FILE} ({len(seen)} unique emotes)")


if __name__ == "__main__":
    main()
