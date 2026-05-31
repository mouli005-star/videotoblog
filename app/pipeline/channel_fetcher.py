import json
import os
import subprocess
import sys
from urllib.parse import urlparse

import yaml
from dotenv import load_dotenv


load_dotenv()


def load_config():
    with open("config.yaml", "r", encoding="utf-8") as file_handle:
        return yaml.safe_load(file_handle)


def run_ytdlp(args):
    cookies_path = "/app/cookies.txt"
    extra = ["--cookies", cookies_path] if os.path.exists(cookies_path) else []
    return subprocess.run(
        [sys.executable, "-m", "yt_dlp", *extra, *args],
        capture_output=True,
        text=True,
        timeout=120,
    )


def normalize_channel_name(channel_url):
    parsed = urlparse(channel_url)
    parts = [part for part in parsed.path.split("/") if part]

    if not parts:
        return "channel"

    last_part = parts[-1]
    if last_part in {"videos", "featured", "streams", "shorts", "playlists", "community"} and len(parts) >= 2:
        last_part = parts[-2]

    return last_part.lstrip("@").replace("/", "_") or "channel"


def parse_duration(duration_raw):
    if duration_raw in (None, "", "None"):
        return None

    try:
        return float(duration_raw)
    except (TypeError, ValueError):
        return None


def is_short_video(url, duration_sec, short_max_seconds):
    if "/shorts/" in url:
        return True

    return duration_sec is not None and duration_sec <= short_max_seconds


def fetch_channel_videos(channel_url, start=1, end=None, skip_shorts=True, short_max_seconds=60):
    """
    Fetch all video metadata from a YouTube channel using yt-dlp.
    Returns a list of video dicts. Skips Shorts by duration.
    """
    print(f"\nFetching video list from: {channel_url}")
    print(f"  Range: {start} to {'all' if end is None else end}")

    command = [
        "--flat-playlist",
        "--print",
        "%(id)s|%(title)s|%(duration)s|%(upload_date)s|%(url)s",
        "--playlist-start",
        str(start),
    ]

    if end is not None:
        command.extend(["--playlist-end", str(end)])

    command.append(channel_url)

    try:
        result = run_ytdlp(command)
    except subprocess.TimeoutExpired:
        print("  ✗ Timed out fetching channel. Check your internet.")
        sys.exit(1)

    if result.returncode != 0:
        print(f"  ✗ yt-dlp error:\n{result.stderr[:500]}")
        sys.exit(1)

    lines = [line for line in result.stdout.splitlines() if line.strip()]
    videos = []
    skipped_shorts = 0

    for line in lines:
        parts = line.split("|", 4)
        if len(parts) < 2:
            continue

        video_id = parts[0].strip()
        title = parts[1].strip()
        duration_raw = parts[2].strip() if len(parts) > 2 else ""
        upload_date = parts[3].strip() if len(parts) > 3 else ""
        url = parts[4].strip() if len(parts) > 4 else f"https://www.youtube.com/watch?v={video_id}"

        duration_sec = parse_duration(duration_raw)

        if skip_shorts and is_short_video(url, duration_sec, short_max_seconds):
            skipped_shorts += 1
            continue

        videos.append(
            {
                "video_id": video_id,
                "title": title,
                "duration": duration_raw,
                "duration_sec": duration_sec,
                "upload_date": upload_date,
                "url": url,
                "transcript_status": "pending",
                "blog_status": "pending",
                "blog_url": None,
            }
        )

    print(f"  ✓ Fetched {len(videos)} videos")
    if skipped_shorts:
        print(f"  ✓ Skipped {skipped_shorts} shorts")

    return videos


def save_video_list(videos, channel_url, base_dir):
    """
    Save video metadata to channel folder and create working subfolders.
    Returns the channel folder path and channel name.
    """
    channel_name = normalize_channel_name(channel_url)
    channel_dir = os.path.join(base_dir, channel_name)
    os.makedirs(channel_dir, exist_ok=True)
    os.makedirs(os.path.join(channel_dir, "transcripts"), exist_ok=True)
    os.makedirs(os.path.join(channel_dir, "audios"), exist_ok=True)
    os.makedirs(os.path.join(channel_dir, "blogs"), exist_ok=True)

    output_path = os.path.join(channel_dir, "video_list.json")

    if os.path.exists(output_path):
        with open(output_path, "r", encoding="utf-8") as file_handle:
            existing_videos = json.load(file_handle)
        existing_by_id = {video["video_id"]: video for video in existing_videos if "video_id" in video}

        for video in videos:
            previous = existing_by_id.get(video["video_id"])
            if not previous:
                continue

            video["transcript_status"] = previous.get("transcript_status", "pending")
            video["blog_status"] = previous.get("blog_status", "pending")
            video["blog_url"] = previous.get("blog_url", None)

    with open(output_path, "w", encoding="utf-8") as file_handle:
        json.dump(videos, file_handle, indent=2, ensure_ascii=False)

    print(f"  ✓ Saved video_list.json → {output_path}")
    return channel_dir, channel_name


def print_summary(videos):
    print(f"\n{'─' * 50}")
    print(f"  Total videos : {len(videos)}")
    if videos:
        print(f"  Most recent  : {videos[0]['title'][:60]}")
        print(f"  Oldest       : {videos[-1]['title'][:60]}")
        durations = [video['duration_sec'] for video in videos if video['duration_sec']]
        if durations:
            average = sum(durations) / len(durations)
            print(f"  Avg duration : {int(average // 60)}m {int(average % 60)}s")
    print(f"{'─' * 50}\n")


def main():
    parser = argparse.ArgumentParser(description="Fetch all videos from a YouTube channel")
    parser.add_argument("channel_url", help="YouTube channel URL e.g. https://www.youtube.com/@mkbhd/videos")
    parser.add_argument("--start", type=int, default=1, help="First video index (1 = most recent)")
    parser.add_argument("--end", type=int, default=None, help="Last video index (omit for all)")
    parser.add_argument("--no-skip-shorts", action="store_true", help="Include Shorts in output")
    args = parser.parse_args()

    config = load_config()
    skip_shorts = config["youtube"]["skip_shorts"] and not args.no_skip_shorts
    short_max_seconds = config["youtube"]["short_max_seconds"]
    base_dir = config["output"]["base_dir"]

    videos = fetch_channel_videos(
        channel_url=args.channel_url,
        start=args.start,
        end=args.end,
        skip_shorts=skip_shorts,
        short_max_seconds=short_max_seconds,
    )

    if not videos:
        print("  No videos found. Check the channel URL.")
        sys.exit(1)

    channel_dir, channel_name = save_video_list(videos, args.channel_url, base_dir)
    print_summary(videos)
    print(f"  Next step: run transcript_fetcher.py {channel_name}")


if __name__ == "__main__":
    import argparse

    main()