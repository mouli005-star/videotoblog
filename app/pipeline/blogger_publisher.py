import json
import os
import re
import sys
import time

import yaml
from dotenv import load_dotenv
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build


load_dotenv()


SCOPES = ["https://www.googleapis.com/auth/blogger"]


def load_config():
    with open("config.yaml", "r", encoding="utf-8") as file_handle:
        return yaml.safe_load(file_handle)


def load_video_list(channel_name, base_dir):
    path = os.path.join(base_dir, channel_name, "video_list.json")
    with open(path, "r", encoding="utf-8") as file_handle:
        return json.load(file_handle)


def save_video_list(videos, channel_name, base_dir):
    path = os.path.join(base_dir, channel_name, "video_list.json")
    with open(path, "w", encoding="utf-8") as file_handle:
        json.dump(videos, file_handle, indent=2, ensure_ascii=False)


def safe_title_for_filename(title):
    return "".join(character if character.isalnum() or character in " -_()" else "_" for character in title)[:60].strip()


def get_blogger_service():
    refresh_token = os.getenv("BLOGGER_REFRESH_TOKEN")
    client_id = os.getenv("BLOGGER_CLIENT_ID")
    client_secret = os.getenv("BLOGGER_CLIENT_SECRET")

    if not refresh_token or not client_id or not client_secret:
        print("  x Blogger OAuth values are missing from .env")
        print("    Run auth_blogger.py first")
        sys.exit(1)

    credentials = Credentials(
        token=None,
        refresh_token=refresh_token,
        client_id=client_id,
        client_secret=client_secret,
        token_uri="https://oauth2.googleapis.com/token",
        scopes=SCOPES,
    )
    credentials.refresh(Request())
    return build("blogger", "v3", credentials=credentials)


def load_blog_html(video, channel_name, base_dir):
    blog_dir = os.path.join(base_dir, channel_name, "blogs")
    if not os.path.exists(blog_dir):
        return None

    expected_file = video.get("blog_file")
    if expected_file:
        candidate = os.path.join(blog_dir, expected_file)
        if os.path.exists(candidate):
            with open(candidate, "r", encoding="utf-8") as file_handle:
                return file_handle.read()

    index = video.get("index", 0)
    safe_title = safe_title_for_filename(video["title"])

    expected_filename = f"{index:02d}_{safe_title}.html"
    expected_path = os.path.join(blog_dir, expected_filename)
    if os.path.exists(expected_path):
        with open(expected_path, "r", encoding="utf-8") as file_handle:
            return file_handle.read()

    expected_prefix = f"{index:02d}_{safe_title[:30]}"
    for filename in os.listdir(blog_dir):
        if filename.startswith(expected_prefix) and filename.endswith(".html"):
            filepath = os.path.join(blog_dir, filename)
            with open(filepath, "r", encoding="utf-8") as file_handle:
                return file_handle.read()

    for filename in os.listdir(blog_dir):
        if filename.startswith(f"{index:02d}_") and filename.endswith(".html"):
            filepath = os.path.join(blog_dir, filename)
            with open(filepath, "r", encoding="utf-8") as file_handle:
                return file_handle.read()

    return None


def parse_blog_html(html, fallback_title):
    title_match = re.search(r"<!--\s*title:\s*(.*?)\s*-->", html, re.DOTALL | re.IGNORECASE)
    tags_match = re.search(r"<!--\s*tags:\s*(.*?)\s*-->", html, re.DOTALL | re.IGNORECASE)
    h1_match = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.IGNORECASE | re.DOTALL)

    title = fallback_title
    if title_match:
        title = title_match.group(1).strip()
    elif h1_match:
        title = re.sub(r"<[^>]+>", "", h1_match.group(1)).strip()

    tags = []
    if tags_match:
        tags = [tag.strip() for tag in tags_match.group(1).split(",") if tag.strip()]

    content = re.sub(r"<!--.*?-->", "", html, flags=re.DOTALL).strip()
    return title, tags, content


def publish_post(service, blog_id, title, content, tags, is_draft=False):
    body = {
        "title": title,
        "content": content,
        "labels": tags,
    }

    request = service.posts().insert(blogId=blog_id, body=body, isDraft=is_draft)
    result = request.execute()
    return result.get("url", ""), result.get("id", "")


def process_video(video, channel_name, base_dir, config, service):
    title = video["title"][:55]
    print(f"\n  [{video.get('index', '?'):02d}] {title}")

    html = load_blog_html(video, channel_name, base_dir)
    if not html:
        print("      x Blog HTML not found - run blog_formatter.py first")
        return "failed", None

    post_title, tags, content = parse_blog_html(html, video["title"])

    is_draft = config["blog"]["post_as_draft"]
    mode = "draft" if is_draft else "live"
    print(f"      Posting as {mode}: {post_title[:50]}")

    blog_id = os.getenv("BLOGGER_BLOG_ID")
    if not blog_id:
        print("      x BLOGGER_BLOG_ID missing in .env")
        return "failed", None

    try:
        post_url, post_id = publish_post(service, blog_id, post_title, content, tags, is_draft)
        print(f"      ✓ Published -> {post_url}")
        return "done", {"url": post_url, "id": post_id}
    except Exception as error:
        print(f"      x Publish failed: {error}")
        return "failed", None


def main():
    parser = argparse.ArgumentParser(description="Publish blog posts to Blogger")
    parser.add_argument("channel_name", help="Channel folder name")
    parser.add_argument("--start", type=int, default=None)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--draft", action="store_true", help="Post as draft")
    parser.add_argument("--rerun-failed", action="store_true")
    args = parser.parse_args()

    config = load_config()
    base_dir = config["output"]["base_dir"]
    publish_delay_seconds = config.get("blog", {}).get("publish_delay_seconds", 2)

    if args.draft:
        config["blog"]["post_as_draft"] = True

    videos = load_video_list(args.channel_name, base_dir)
    for index, video in enumerate(videos, start=1):
        video["index"] = index

    targets = videos
    if args.start is not None:
        targets = [video for video in targets if video["index"] >= args.start]
    if args.end is not None:
        targets = [video for video in targets if video["index"] <= args.end]

    targets = [
        video
        for video in targets
        if video.get("transcript_status") == "done"
        and (
            not video.get("blog_url")
            or (args.rerun_failed and video.get("blog_status") == "failed")
        )
    ]

    if not targets:
        print("\n  No videos ready for publishing.")
        print("  Make sure transcript_fetcher.py and blog_formatter.py have run first.\n")
        sys.exit(0)

    print(f"\n  Publishing {len(targets)} blog posts...\n{'─' * 50}")

    service = get_blogger_service()
    done = 0
    failed = 0

    for video in targets:
        status, publish_info = process_video(video, args.channel_name, base_dir, config, service)
        video["blog_status"] = status
        if publish_info:
            video["blog_url"] = publish_info.get("url")
            video["blog_post_id"] = publish_info.get("id")

        if status == "done":
            done += 1
        else:
            failed += 1

        save_video_list(videos, args.channel_name, base_dir)
        time.sleep(publish_delay_seconds)

    print(f"\n{'─' * 50}")
    print(f"  ✓ Done: {done}  ✗ Failed: {failed}")
    if done > 0:
        print(f"  Published posts were sent to: {os.getenv('BLOGGER_BLOG_ID')}")
    print()


if __name__ == "__main__":
    import argparse

    main()