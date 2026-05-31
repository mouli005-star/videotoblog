import json
import os
import sys
import time

import yaml
from dotenv import load_dotenv
from groq import Groq


load_dotenv()


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


def load_transcript(video, channel_name, base_dir):
    transcript_dir = os.path.join(base_dir, channel_name, "transcripts")

    transcript_file = video.get("transcript_file")
    if transcript_file:
        candidate = os.path.join(transcript_dir, transcript_file)
        if os.path.exists(candidate):
            with open(candidate, "r", encoding="utf-8") as file_handle:
                return file_handle.read()

    index = video.get("index", 0)
    safe_title = safe_title_for_filename(video["title"])
    expected_prefix = f"{index:02d}_{safe_title[:30]}"

    for filename in os.listdir(transcript_dir):
        if filename.startswith(f"{index:02d}_") and filename.endswith(".txt"):
            filepath = os.path.join(transcript_dir, filename)
            with open(filepath, "r", encoding="utf-8") as file_handle:
                return file_handle.read()

    for filename in os.listdir(transcript_dir):
        if filename.startswith(expected_prefix) and filename.endswith(".txt"):
            filepath = os.path.join(transcript_dir, filename)
            with open(filepath, "r", encoding="utf-8") as file_handle:
                return file_handle.read()

    return None


def transcript_body_only(transcript_text):
    parts = transcript_text.split("\n\n", 1)
    if len(parts) == 2:
        return parts[1].strip()
    return transcript_text.strip()


BLOG_SYSTEM_PROMPT = """
You are an expert blog writer. You will be given a YouTube video transcript.
Your job is to convert it into a high-quality, SEO-friendly blog post in HTML format.

Rules:
- Write a compelling <h1> title that can differ slightly from the video title for SEO
- Write a short meta description (1-2 sentences) in a <!-- meta: --> comment at the top
- Write suggested tags in a <!-- tags: tag1, tag2, tag3 --> comment at the top
- Use <h2> subheadings to break up sections with at least 3 sections
- Write in clear, engaging prose, not bullet points
- Remove filler words, timestamps, and repetition from the transcript
- Add a short intro paragraph and a conclusion paragraph
- Keep the tone informative but conversational
- Return only the HTML content, with no markdown, no code fences, and no explanation
- Do not include <html>, <head>, or <body> tags
""".strip()


def format_blog_with_groq(transcript_text, video_title, config, retries=3):
    client = Groq(api_key=os.getenv("GROQ_API_KEY"))
    model = config["blog"]["groq_model"]

    max_chars = 12000
    if len(transcript_text) > max_chars:
        transcript_text = transcript_text[:max_chars] + "\n\n[transcript truncated]"

    user_prompt = f"""
Video title: {video_title}

Transcript:
{transcript_text}

Convert this into a blog post following the rules.
""".strip()

    for attempt in range(1, retries + 1):
        try:
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": BLOG_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                max_tokens=2048,
                temperature=0.7,
            )
            return response.choices[0].message.content.strip()
        except Exception as error:
            print(f"      Attempt {attempt} failed: {error}")
            if attempt < retries:
                wait_seconds = 2 ** attempt
                print(f"      Retrying in {wait_seconds}s...")
                time.sleep(wait_seconds)

    return None


def extract_meta(html):
    import re

    meta = ""
    tags = []

    meta_match = re.search(r"<!--\s*meta:\s*(.*?)\s*-->", html, re.DOTALL)
    tags_match = re.search(r"<!--\s*tags:\s*(.*?)\s*-->", html)

    if meta_match:
        meta = meta_match.group(1).strip()
    if tags_match:
        tags = [tag.strip() for tag in tags_match.group(1).split(",") if tag.strip()]

    return meta, tags


def save_blog(html, meta, tags, video, channel_name, base_dir=None):
    if base_dir is None:
        blog_dir = os.fspath(channel_name)
    else:
        blog_dir = os.path.join(base_dir, channel_name, "blogs")
    os.makedirs(blog_dir, exist_ok=True)

    index = video.get("index", 0)
    safe_title = safe_title_for_filename(video["title"])
    filename = f"{index:02d}_{safe_title}.html"
    filepath = os.path.join(blog_dir, filename)

    full_content = f"<!-- title: {video['title']} -->\n"
    full_content += f"<!-- video_id: {video['video_id']} -->\n"
    full_content += f"<!-- url: {video['url']} -->\n"
    if meta:
        full_content += f"<!-- meta: {meta} -->\n"
    if tags:
        full_content += f"<!-- tags: {', '.join(tags)} -->\n"
    full_content += "\n" + html

    with open(filepath, "w", encoding="utf-8") as file_handle:
        file_handle.write(full_content)

    return filepath, tags


def process_video(video, channel_name, base_dir, config):
    title = video["title"][:55]
    print(f"\n  [{video.get('index', '?'):02d}] {title}")

    transcript = load_transcript(video, channel_name, base_dir)
    if not transcript:
        print("      x Transcript file not found - run transcript_fetcher.py first")
        return "failed", []

    transcript = transcript_body_only(transcript)

    print("      Formatting with Groq LLM...")
    html = format_blog_with_groq(transcript, video["title"], config)

    if not html:
        print("      x Blog formatting failed after retries")
        return "failed", []

    meta, tags = extract_meta(html)
    filepath, tags = save_blog(html, meta, tags, video, channel_name, base_dir)

    print(f"      ✓ Saved -> {os.path.basename(filepath)}")
    if tags:
        print(f"      Tags: {', '.join(tags[:4])}")

    return "done", tags


def main():
    parser = argparse.ArgumentParser(description="Format transcripts into blog posts")
    parser.add_argument("channel_name", help="Channel folder name")
    parser.add_argument("--start", type=int, default=None)
    parser.add_argument("--end", type=int, default=None)
    parser.add_argument("--rerun-failed", action="store_true")
    args = parser.parse_args()

    config = load_config()
    base_dir = config["output"]["base_dir"]

    videos = load_video_list(args.channel_name, base_dir)

    for index, video in enumerate(videos, start=1):
        video["index"] = index

    targets = videos
    if args.start is not None:
        targets = [video for video in targets if video["index"] >= args.start]
    if args.end is not None:
        targets = [video for video in targets if video["index"] <= args.end]

    if args.rerun_failed:
        targets = [video for video in targets if video.get("blog_status") in ("pending", "failed")]
    else:
        targets = [video for video in targets if video.get("blog_status") == "pending"]

    targets = [video for video in targets if video.get("transcript_status") == "done"]

    if not targets:
        print("\n  No videos ready for blog formatting.")
        print("  Make sure transcript_fetcher.py has run first.\n")
        sys.exit(0)

    print(f"\n  Formatting {len(targets)} blog posts...\n{'─' * 50}")

    done = 0
    failed = 0

    for video in targets:
        status, tags = process_video(video, args.channel_name, base_dir, config)
        video["blog_status"] = status
        if tags:
            video["blog_tags"] = tags

        if status == "done":
            done += 1
        else:
            failed += 1

        save_video_list(videos, args.channel_name, base_dir)
        time.sleep(2)

    print(f"\n{'─' * 50}")
    print(f"  ✓ Done: {done}  ✗ Failed: {failed}")
    print(f"  Blog posts saved in: ./channels/{args.channel_name}/blogs/")
    if done > 0:
        print(f"  Next step: python blogger_publisher.py {args.channel_name}")
    print()


if __name__ == "__main__":
    import argparse

    main()