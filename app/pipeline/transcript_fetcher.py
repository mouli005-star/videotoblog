import json
import os
import subprocess
import sys
import time

import yaml
from dotenv import load_dotenv
from youtube_transcript_api import NoTranscriptFound, TranscriptsDisabled, YouTubeTranscriptApi


load_dotenv()


def load_config():
    with open("config.yaml", "r", encoding="utf-8") as file_handle:
        return yaml.safe_load(file_handle)


def load_video_list(channel_name, base_dir="./channels"):
    path = os.path.join(base_dir, channel_name, "video_list.json")
    if not os.path.exists(path):
        print(f"  x video_list.json not found for '{channel_name}'")
        print("    Run: python channel_fetcher.py first")
        sys.exit(1)

    with open(path, "r", encoding="utf-8") as file_handle:
        return json.load(file_handle)


def save_video_list(videos, channel_name, base_dir="./channels"):
    path = os.path.join(base_dir, channel_name, "video_list.json")
    with open(path, "w", encoding="utf-8") as file_handle:
        json.dump(videos, file_handle, indent=2, ensure_ascii=False)


def get_channel_paths(channel_name, base_dir):
    channel_dir = os.path.join(base_dir, channel_name)
    return {
        "channel_dir": channel_dir,
        "transcripts": os.path.join(channel_dir, "transcripts"),
        "audios": os.path.join(channel_dir, "audios"),
        "blogs": os.path.join(channel_dir, "blogs"),
    }


def get_preferred_languages(config):
    youtube_settings = config.get("youtube", {})
    if "preferred_languages" in youtube_settings and youtube_settings["preferred_languages"]:
        return youtube_settings["preferred_languages"]

    language = config.get("transcription", {}).get("language", "en")
    return [language]


def ensure_channel_dirs(channel_paths):
    os.makedirs(channel_paths["channel_dir"], exist_ok=True)
    os.makedirs(channel_paths["transcripts"], exist_ok=True)
    os.makedirs(channel_paths["audios"], exist_ok=True)
    os.makedirs(channel_paths["blogs"], exist_ok=True)


def get_transcript_via_api(video_id, languages):
    try:
        segments = YouTubeTranscriptApi.get_transcript(video_id, languages=languages)
        return segments, "youtube-api"
    except NoTranscriptFound:
        try:
            transcript_list = YouTubeTranscriptApi.list_transcripts(video_id)
            transcript = transcript_list.find_generated_transcript(languages)
            segments = transcript.fetch()
            return segments, "youtube-auto"
        except Exception:
            return None, None
    except TranscriptsDisabled:
        return None, None
    except Exception:
        return None, None


def download_audio(video_id, audio_dir, cookies_path=None):
    url = f"https://www.youtube.com/watch?v={video_id}"
    output_template = os.path.join(audio_dir, f"{video_id}.%(ext)s")

    command = [
        sys.executable,
        "-m",
        "yt_dlp",
        "-x",
        "--audio-format",
        "mp3",
        "--audio-quality",
        "5",
        "--no-playlist",
        "--sleep-interval",
        "2",
        "--max-sleep-interval",
        "5",
        "-o",
        output_template,
    ]

    if cookies_path and os.path.exists(cookies_path):
        command.extend(["--cookies", cookies_path])

    command.append(url)

    print("      Downloading audio...")
    result = subprocess.run(command, capture_output=True, text=True)

    if result.returncode != 0:
        print(f"      x Download failed: {result.stderr[:200]}")
        return None

    for filename in os.listdir(audio_dir):
        if filename.startswith(video_id) and filename.endswith(".mp3"):
            return os.path.join(audio_dir, filename)

    return None


def transcribe_with_groq(audio_path, config):
    from groq import Groq

    client = Groq(api_key=os.getenv("GROQ_API_KEY"))

    print("      Sending to Groq Whisper...")
    try:
        with open(audio_path, "rb") as file_handle:
            result = client.audio.transcriptions.create(
                model=config["transcription"]["groq_model"],
                file=file_handle,
                language=config["transcription"]["language"],
            )
        return result.text, "groq-whisper"
    except Exception as error:
        print(f"      x Groq Whisper error: {error}")
        return None, None


def transcribe_with_gpu_server(audio_path, config):
    import requests

    url = config["transcription"]["gpu_server_url"]
    print(f"      Sending to GPU server: {url}")

    try:
        with open(audio_path, "rb") as file_handle:
            response = requests.post(url, files={"audio": file_handle}, timeout=300)

        if response.status_code == 200:
            return response.json().get("text"), "gpu-server"

        print(f"      x GPU server error: {response.status_code}")
        return None, None
    except Exception as error:
        print(f"      x GPU server error: {error}")
        return None, None


def segments_to_text(segments, with_timestamps=True):
    lines = []
    for segment in segments:
        if with_timestamps:
            start = segment.get("start", 0)
            minutes = int(start // 60)
            seconds = int(start % 60)
            lines.append(f"[{minutes:02d}:{seconds:02d}] {segment['text'].strip()}")
        else:
            lines.append(segment["text"].strip())
    return "\n".join(lines)


def safe_title_for_filename(title):
    return "".join(character if character.isalnum() or character in " -_()" else "_" for character in title)[:60].strip()


def save_transcript(text, source, video, transcript_dir):
    safe_title = safe_title_for_filename(video["title"])
    index = video.get("index", 0)
    filename = f"{index:02d}_{safe_title}.txt"
    filepath = os.path.join(transcript_dir, filename)

    header = f"# {video['title']}\n"
    header += f"# source: {source}\n"
    header += f"# video_id: {video['video_id']}\n"
    header += f"# url: {video['url']}\n"
    if video.get("upload_date"):
        header += f"# uploaded: {video['upload_date']}\n"
    header += "\n"

    with open(filepath, "w", encoding="utf-8") as file_handle:
        file_handle.write(header + text)

    return filepath


def update_video_record(video, transcript_status, transcript_source=None, transcript_file=None):
    video["transcript_status"] = transcript_status
    if transcript_source is not None:
        video["transcript_source"] = transcript_source
    if transcript_file is not None:
        video["transcript_file"] = transcript_file


def process_video(video, channel_paths, config, cookies_path=None):
    video_id = video["video_id"]
    title = video["title"][:55]

    print(f"\n  [{video.get('index', '?'):02d}] {title}")
    print("      Trying YouTube captions...")

    segments, source = get_transcript_via_api(
        video_id,
        languages=get_preferred_languages(config),
    )

    if segments:
        text = segments_to_text(segments, with_timestamps=True)
        print(f"      ✓ Captions found ({source})")
    else:
        print("      No captions - falling back to Whisper")
        audio_path = download_audio(video_id, channel_paths["audios"], cookies_path)
        if not audio_path:
            print("      x Audio download failed - skipping")
            update_video_record(video, "failed")
            return "failed"

        if config["transcription"]["dev_mode"]:
            text, source = transcribe_with_groq(audio_path, config)
        else:
            text, source = transcribe_with_gpu_server(audio_path, config)

        if not config["transcription"]["keep_audio"]:
            try:
                os.remove(audio_path)
                print("      Audio deleted")
            except OSError:
                pass

        if not text:
            update_video_record(video, "failed")
            return "failed"

    transcript_path = save_transcript(text, source, video, channel_paths["transcripts"])
    update_video_record(video, "done", source, os.path.basename(transcript_path))
    print(f"      ✓ Saved -> {os.path.basename(transcript_path)}")
    return "done"


def main():
    parser = argparse.ArgumentParser(description="Fetch transcripts for all videos in a channel")
    parser.add_argument("channel_name", help="Channel folder name from channel_fetcher.py output")
    parser.add_argument("--start", type=int, default=None, help="Start from this index")
    parser.add_argument("--end", type=int, default=None, help="Stop at this index")
    parser.add_argument("--rerun-failed", action="store_true", help="Retry previously failed videos")
    parser.add_argument("--cookies", default="cookies.txt", help="Path to cookies.txt for yt-dlp")
    args = parser.parse_args()

    config = load_config()
    base_dir = config["output"]["base_dir"]
    channel_paths = get_channel_paths(args.channel_name, base_dir)
    ensure_channel_dirs(channel_paths)

    videos = load_video_list(args.channel_name, base_dir)

    for index, video in enumerate(videos, start=1):
        video["index"] = index

    targets = videos
    if args.start is not None:
        targets = [video for video in targets if video["index"] >= args.start]
    if args.end is not None:
        targets = [video for video in targets if video["index"] <= args.end]
    if not args.rerun_failed:
        targets = [video for video in targets if video.get("transcript_status") != "done"]

    total = len(targets)
    print(f"\nProcessing {total} videos in {args.channel_name}")

    done_count = 0
    failed_count = 0

    for video in targets:
        result = process_video(video, channel_paths, config, args.cookies)
        if result == "done":
            done_count += 1
        else:
            failed_count += 1

        save_video_list(videos, args.channel_name, base_dir)
        time.sleep(2)

    print(f"\nSummary: {done_count} done, {failed_count} failed")
    print(f"  Next step: python blog_formatter.py {args.channel_name}")


if __name__ == "__main__":
    import argparse

    main()
