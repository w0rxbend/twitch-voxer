#!/usr/bin/env python3
import http.server
import secrets
import time
import urllib.parse
import webbrowser
import pickledb
import requests
from pathlib import Path
from dotenv import load_dotenv
from voxer.config import CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN

load_dotenv()

BASE_URL = "https://api.twitch.tv/helix"
REDIRECT_URI = "http://localhost:1337/api/connect/twitch/callback"
SCOPES = ["user:read:follows", "moderator:read:followers"]
OUTPUT_FILE = Path("emotes/emotes.db")


# ── auth ──────────────────────────────────────────────────────────────────────

def get_app_token(session: requests.Session) -> str:
    resp = session.post(
        "https://id.twitch.tv/oauth2/token",
        params={"client_id": CLIENT_ID, "client_secret": CLIENT_SECRET, "grant_type": "client_credentials"},
    )
    resp.raise_for_status()
    return resp.json()["access_token"]


def refresh_user_token(session: requests.Session) -> str | None:
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
    resp = session.get(
        "https://id.twitch.tv/oauth2/validate",
        headers={"Authorization": f"OAuth {token}"},
    )
    if resp.status_code != 200:
        return set()
    return set(resp.json().get("scopes", []))


def oauth_flow(session: requests.Session) -> str:
    state = secrets.token_hex(16)
    scope_str = urllib.parse.quote(" ".join(SCOPES))
    auth_url = (
        f"https://id.twitch.tv/oauth2/authorize"
        f"?client_id={CLIENT_ID}"
        f"&redirect_uri={urllib.parse.quote(REDIRECT_URI)}"
        f"&response_type=code"
        f"&scope={scope_str}"
        f"&state={state}"
        f"&force_verify=true"
    )

    code_holder: dict[str, str] = {}

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
            if "code" in params:
                code_holder["code"] = params["code"][0]
            self.send_response(200)
            self.send_header("Content-type", "text/html")
            self.end_headers()
            self.wfile.write(b"<h1>Authorized! You can close this tab.</h1>")

        def log_message(self, *_: object) -> None:
            pass

    server = http.server.HTTPServer(("localhost", 1337), Handler)
    server.timeout = 1  # check for code every second

    print(f"\nOpening browser for Twitch authorization...")
    print(f"If the browser does not open, visit:\n  {auth_url}\n")
    webbrowser.open(auth_url)

    deadline = time.time() + 120
    while "code" not in code_holder and time.time() < deadline:
        server.handle_request()
    server.server_close()

    code = code_holder.get("code")
    if not code:
        raise RuntimeError("OAuth timed out or was cancelled.")

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


# ── api helpers ───────────────────────────────────────────────────────────────

def hdrs(token: str) -> dict:
    return {"Client-Id": CLIENT_ID, "Authorization": f"Bearer {token}"}


def get_current_user(session: requests.Session, token: str) -> dict:
    resp = session.get(f"{BASE_URL}/users", headers=hdrs(token))
    resp.raise_for_status()
    return resp.json()["data"][0]


def paginate(session: requests.Session, url: str, token: str, params: dict) -> list[dict]:
    results: list[dict] = []
    cursor = None
    while True:
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
    items = paginate(session, f"{BASE_URL}/channels/followed", token, {"user_id": user_id, "first": 100})
    return [i["broadcaster_id"] for i in items]


def fetch_follower_ids(session: requests.Session, token: str, broadcaster_id: str) -> list[str]:
    items = paginate(session, f"{BASE_URL}/channels/followers", token, {"broadcaster_id": broadcaster_id, "first": 100})
    return [i["user_id"] for i in items]


def fetch_global_emotes(session: requests.Session, token: str) -> list[dict]:
    resp = session.get(f"{BASE_URL}/chat/emotes/global", headers=hdrs(token))
    resp.raise_for_status()
    return resp.json()["data"]


def fetch_channel_emotes(session: requests.Session, token: str, broadcaster_id: str) -> list[dict]:
    resp = session.get(f"{BASE_URL}/chat/emotes", headers=hdrs(token), params={"broadcaster_id": broadcaster_id})
    if resp.status_code in (400, 404):
        return []
    resp.raise_for_status()
    return resp.json()["data"]


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
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

        all_channel_ids = list(set(followed_ids + follower_ids))
        print(f"\nFetching emotes for {len(all_channel_ids)} unique channels...")

        channel_emotes: list[dict] = []
        for i, cid in enumerate(all_channel_ids, 1):
            emotes = fetch_channel_emotes(session, app_token, cid)
            channel_emotes.extend(emotes)
            if i % 20 == 0:
                print(f"  {i}/{len(all_channel_ids)} done ({len(channel_emotes)} emotes so far)...")
                time.sleep(0.3)

        print(f"  {len(channel_emotes)} channel emotes from {len(all_channel_ids)} channels")

        print(f"\nWriting to {OUTPUT_FILE}...")
        seen: set[str] = set()
        with pickledb.PickleDB(str(OUTPUT_FILE)) as db:
            for emote in global_emotes + channel_emotes:
                name = emote["name"]
                if name in seen:
                    continue
                seen.add(name)
                imgs = emote["images"]
                db.set(name, {"url_1x": imgs["url_1x"], "url_2x": imgs["url_2x"], "url_4x": imgs["url_4x"]})

        print(f"Saved to {OUTPUT_FILE} ({len(seen)} unique emotes)")


if __name__ == "__main__":
    main()
