"""
Social media posting engine for Conviction Capital.

Platforms:
  - X (Twitter)  — text posts, market briefs, script hooks
  - YouTube      — video uploads (Shorts format)
  - TikTok       — video uploads (Content Posting API)

All functions return {"ok": bool, "platform": str, "url": str, "error": str}
Keys are read from config.json or environment variables.
"""
from __future__ import annotations
import os, json, time, requests
from pathlib import Path
from typing import Optional

def _cfg() -> dict:
    try:
        return json.load(open(Path(__file__).parent.parent / "config.json"))
    except Exception:
        return {}

def _key(env: str, cfg_key: str) -> str:
    cfg = _cfg()
    return os.environ.get(env, "").strip() or cfg.get(cfg_key, "")


# ── X / TWITTER ──────────────────────────────────────────────────────────────

def post_to_x(text: str) -> dict:
    """
    Post to X (Twitter) via API v2.
    Needs: twitter_api_key, twitter_api_secret,
           twitter_access_token, twitter_access_secret in config.json
    """
    api_key      = _key("TWITTER_API_KEY",      "twitter_api_key")
    api_secret   = _key("TWITTER_API_SECRET",   "twitter_api_secret")
    access_token = _key("TWITTER_ACCESS_TOKEN", "twitter_access_token")
    access_secret= _key("TWITTER_ACCESS_SECRET","twitter_access_secret")

    if not all([api_key, api_secret, access_token, access_secret]):
        return {"ok": False, "platform": "twitter", "error": "Keys not configured — add twitter_api_key/secret/access_token/access_secret to config.json"}

    try:
        import tweepy  # type: ignore
    except ImportError:
        return {"ok": False, "platform": "twitter", "error": "tweepy not installed — run: pip install tweepy"}

    try:
        client = tweepy.Client(
            consumer_key=api_key,
            consumer_secret=api_secret,
            access_token=access_token,
            access_token_secret=access_secret,
        )
        # Split into thread if over 280 chars
        chunks = _split_thread(text, 280)
        prev_id = None
        tweet_id = None
        for chunk in chunks:
            resp = client.create_tweet(
                text=chunk,
                in_reply_to_tweet_id=prev_id,
            )
            tweet_id = resp.data["id"]
            prev_id  = tweet_id

        url = f"https://x.com/i/status/{tweet_id}"
        return {"ok": True, "platform": "twitter", "id": str(tweet_id), "url": url, "parts": len(chunks)}
    except Exception as e:
        return {"ok": False, "platform": "twitter", "error": str(e)}


def _split_thread(text: str, limit: int = 280) -> list[str]:
    """Split long text into tweet-sized chunks, preserving word boundaries."""
    if len(text) <= limit:
        return [text]
    chunks, current = [], ""
    for word in text.split():
        if len(current) + len(word) + 1 > limit - 6:
            chunks.append(current.strip())
            current = word + " "
        else:
            current += word + " "
    if current.strip():
        chunks.append(current.strip())
    # Number them: (1/3), (2/3) …
    n = len(chunks)
    return [f"{c} ({i+1}/{n})" if n > 1 else c for i, c in enumerate(chunks)]


# ── YOUTUBE ──────────────────────────────────────────────────────────────────

def upload_to_youtube(video_path: str, title: str, description: str,
                      tags: Optional[list] = None, made_for_kids: bool = False) -> dict:
    """
    Upload a video to YouTube (Shorts-compatible).
    Needs: youtube_client_id, youtube_client_secret, youtube_refresh_token in config.json
    Get OAuth credentials: console.cloud.google.com → APIs → YouTube Data API v3
    Get refresh token: run web/youtube_auth.py once locally
    """
    client_id     = _key("YOUTUBE_CLIENT_ID",     "youtube_client_id")
    client_secret = _key("YOUTUBE_CLIENT_SECRET",  "youtube_client_secret")
    refresh_token = _key("YOUTUBE_REFRESH_TOKEN",  "youtube_refresh_token")

    if not all([client_id, client_secret, refresh_token]):
        return {"ok": False, "platform": "youtube",
                "error": "OAuth not configured — add youtube_client_id/secret/refresh_token to config.json"}

    if not Path(video_path).exists():
        return {"ok": False, "platform": "youtube", "error": f"Video not found: {video_path}"}

    # Refresh access token
    try:
        tr = requests.post("https://oauth2.googleapis.com/token", data={
            "client_id": client_id, "client_secret": client_secret,
            "refresh_token": refresh_token, "grant_type": "refresh_token",
        }, timeout=15)
        tr.raise_for_status()
        access_token = tr.json().get("access_token")
        if not access_token:
            return {"ok": False, "platform": "youtube", "error": "Token refresh failed"}
    except Exception as e:
        return {"ok": False, "platform": "youtube", "error": f"Token refresh: {e}"}

    # Upload via resumable upload
    try:
        file_size = Path(video_path).stat().st_size
        meta = {
            "snippet": {
                "title": title[:100],
                "description": description[:4900] + "\n\nconvictioncapital.com",
                "tags": (tags or []) + ["convictioncapital", "markets", "finance", "investing"],
                "categoryId": "25",  # News & Politics
            },
            "status": {
                "privacyStatus": "public",
                "madeForKids": made_for_kids,
            },
        }
        # Initiate resumable upload
        init = requests.post(
            "https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json",
                "X-Upload-Content-Type": "video/mp4",
                "X-Upload-Content-Length": str(file_size),
            },
            json=meta, timeout=30,
        )
        init.raise_for_status()
        upload_url = init.headers["Location"]

        # Upload file
        with open(video_path, "rb") as f:
            video_bytes = f.read()
        up = requests.put(
            upload_url,
            headers={"Content-Type": "video/mp4", "Content-Length": str(file_size)},
            data=video_bytes, timeout=300,
        )
        up.raise_for_status()
        video_id = up.json().get("id") or up.json().get("videoId", "")
        return {"ok": True, "platform": "youtube", "id": video_id,
                "url": f"https://youtube.com/watch?v={video_id}"}
    except Exception as e:
        return {"ok": False, "platform": "youtube", "error": str(e)}


# ── TIKTOK ───────────────────────────────────────────────────────────────────

def upload_to_tiktok(video_path: str, caption: str) -> dict:
    """
    Upload video to TikTok via Content Posting API v2.
    Needs: tiktok_access_token in config.json
    Get token: developers.tiktok.com → your app → Content Posting API
    """
    access_token = _key("TIKTOK_ACCESS_TOKEN", "tiktok_access_token")

    if not access_token:
        return {"ok": False, "platform": "tiktok",
                "error": "tiktok_access_token not configured in config.json"}

    if not Path(video_path).exists():
        return {"ok": False, "platform": "tiktok", "error": f"Video not found: {video_path}"}

    try:
        file_size = Path(video_path).stat().st_size

        # Step 1: Init upload
        init = requests.post(
            "https://open.tiktokapis.com/v2/post/publish/video/init/",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Type": "application/json; charset=UTF-8",
            },
            json={
                "post_info": {
                    "title": caption[:150],
                    "privacy_level": "PUBLIC_TO_EVERYONE",
                    "disable_duet": False,
                    "disable_comment": False,
                    "disable_stitch": False,
                    "video_cover_timestamp_ms": 1000,
                },
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": file_size,
                    "chunk_size": file_size,
                    "total_chunk_count": 1,
                },
            },
            timeout=20,
        )
        init.raise_for_status()
        data = init.json().get("data", {})
        upload_url = data.get("upload_url")
        publish_id = data.get("publish_id")
        if not upload_url:
            return {"ok": False, "platform": "tiktok", "error": f"No upload URL: {init.text[:200]}"}

        # Step 2: Upload file
        with open(video_path, "rb") as f:
            video_bytes = f.read()
        up = requests.put(
            upload_url,
            headers={
                "Content-Type": "video/mp4",
                "Content-Range": f"bytes 0-{file_size-1}/{file_size}",
            },
            data=video_bytes, timeout=300,
        )
        up.raise_for_status()

        return {"ok": True, "platform": "tiktok", "publish_id": publish_id,
                "url": "https://tiktok.com/@your_account"}
    except Exception as e:
        return {"ok": False, "platform": "tiktok", "error": str(e)}


# ── POST TO ALL ───────────────────────────────────────────────────────────────

def post_brief_to_x(brief: dict) -> dict:
    """Format a Jarvis brief and post it to X."""
    headline = brief.get("headline", "")
    body     = brief.get("body", "")
    label    = brief.get("label", "Market Update")
    text = f"{headline}\n\n{body}\n\nFull breakdown → convictioncapital.com".strip()
    return post_to_x(text)

def post_script_to_x(script: dict) -> dict:
    """Format a content script and post the hook + CTA to X."""
    hook    = script.get("hook", "")
    body    = script.get("body", "")
    caption = script.get("caption", "")
    text = f"{hook}\n\n{body}\n\nconvictioncapital.com".strip()
    return post_to_x(text)
