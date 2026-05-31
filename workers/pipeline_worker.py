import os
import sys
import time
import json
from datetime import datetime
from pathlib import Path
from celery import Celery
from dotenv import load_dotenv

load_dotenv()

# Add the project root to path for both Docker and local Windows runs.
PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DOCKER_APP_PATH = "/app"
if os.path.isdir(DOCKER_APP_PATH) and DOCKER_APP_PATH not in sys.path:
    sys.path.insert(0, DOCKER_APP_PATH)

from app.database import SessionLocal
from app import models
from app.crypto import decrypt

WORKSPACE_ROOT = PROJECT_ROOT if os.name == "nt" else (Path(DOCKER_APP_PATH) if os.path.isdir(DOCKER_APP_PATH) else PROJECT_ROOT)
CHANNELS_ROOT = WORKSPACE_ROOT / "channels"
CONFIG_PATH = WORKSPACE_ROOT / "config.yaml"

REDIS_URL = os.getenv("REDIS_URL", "redis://redis:6379/0")

celery_app = Celery("pipeline_worker", broker=REDIS_URL, backend=REDIS_URL)
celery_app.conf.update(
    broker_connection_retry_on_startup=True,
    task_serializer="json",
    result_serializer="json",
)


# ── ping task (health check) ──────────────────────
@celery_app.task
def ping():
    return "pong"


# ── main pipeline task ────────────────────────────
@celery_app.task(bind=True)
def run_pipeline(self, job_id: str, post_as_draft: bool = False):
    from sqlalchemy.orm import Session

    db: Session = SessionLocal()

    def log(message: str, level: str = "info"):
        """Write a log line to DB — SSE stream picks it up."""
        entry = models.ProgressLog(
            job_id=job_id,
            message=message,
            level=level,
        )
        db.add(entry)
        db.commit()
        print(f"[{level.upper()}] {message}")

    def update_job(**kwargs):
        db.query(models.Job).filter(
            models.Job.id == job_id
        ).update(kwargs)
        db.commit()

    try:
        # Load job + user tokens
        job = db.query(models.Job).filter(
            models.Job.id == job_id
        ).first()

        if not job:
            return

        user = db.query(models.User).filter(
            models.User.id == job.user_id
        ).first()

        blogger_token = db.query(models.BloggerToken).filter(
            models.BloggerToken.user_id == job.user_id
        ).first()

        if not blogger_token or not blogger_token.blog_id:
            log("✗ No Blogger account connected for this user", "error")
            update_job(status="failed")
            return

        update_job(status="running")
        log(f"▶ Starting pipeline for {job.channel_url}", "info")
        log(f"  Range: {job.start_idx} to {job.end_idx or 'all'}", "info")

        # ── Phase 2: Fetch video list ─────────────
        log("📋 Fetching video list...", "info")

        from app.pipeline.channel_fetcher import fetch_channel_videos

        channel_url = job.channel_url.rstrip("/")
        if not channel_url.endswith("/videos"):
            channel_url = channel_url + "/videos"

        videos = fetch_channel_videos(
            channel_url=channel_url,
            start=job.start_idx,
            end=job.end_idx,
            skip_shorts=True,
            short_max_seconds=120,  # skip videos under 2 mins
        )

        if not videos:
            log("✗ No videos found. Check the channel URL.", "error")
            update_job(status="failed")
            return

        channel_base_url = channel_url[:-len("/videos")] if channel_url.endswith("/videos") else channel_url

        update_job(
            total_videos=len(videos),
            channel_name=channel_base_url.rstrip("/").split("/")[-1].replace("@", "")
        )
        log(f"✓ Found {len(videos)} videos (Shorts + under 2min skipped)", "success")

        # Save video results to DB
        for i, v in enumerate(videos):
            v["index"] = i + 1
            result = models.VideoResult(
                job_id=job_id,
                video_id=v["video_id"],
                title=v["title"],
                duration_sec=int(v["duration_sec"]) if v.get("duration_sec") else None,
                transcript_status="pending",
                blog_status="pending",
            )
            db.add(result)
        db.commit()

        # ── Phase 3: Transcribe ───────────────────
        log("🎙️ Starting transcription...", "info")

        from app.pipeline.transcript_fetcher import (
            get_transcript_via_api,
            download_audio,
            transcribe_with_groq,
            transcribe_with_gpu_server,
            segments_to_text,
            save_transcript,
        )

        # Load config
        import yaml
        config_path = "config.yaml"
        if not os.path.exists(config_path):
            config_path = "/app/config.yaml"

        if os.path.exists(config_path):
            with open(config_path) as f:
                config = yaml.safe_load(f)
        else:
            config = {}

        # Always set these with fallbacks — never leave undefined
        if "transcription" not in config:
            config["transcription"] = {
                "dev_mode": True,
                "groq_model": "whisper-large-v3",
                "gpu_server_url": os.getenv("GPU_SERVER_URL", ""),
                "language": "en",
                "keep_audio": False,
            }
        if "youtube" not in config:
            config["youtube"] = {}

        preferred_languages = config["youtube"].get("preferred_languages", ["en"])

        # Per-channel output folder with a nested job folder.
        channel_name = _normalize_channel_name(job.channel_url)
        channel_dir = CHANNELS_ROOT / channel_name
        job_dir = channel_dir / job_id
        transcript_dir = job_dir / "transcripts"
        audio_dir = job_dir / "audios"
        blog_dir = job_dir / "blogs"

        os.makedirs(channel_dir, exist_ok=True)
        os.makedirs(transcript_dir, exist_ok=True)
        os.makedirs(audio_dir, exist_ok=True)
        os.makedirs(blog_dir, exist_ok=True)

        for video in videos:
            title_short = video["title"][:50]
            log(f"\n[{video['index']:02d}] {title_short}", "info")

            # Try captions first
            log("  Trying YouTube captions...", "info")
            segments, source = get_transcript_via_api(
                video["video_id"],
                languages=preferred_languages
            )

            if segments:
                text = segments_to_text(segments, with_timestamps=True)
                log(f"  ✓ Captions found ({source})", "success")
            else:
                log("  No captions — downloading audio for Whisper...", "info")
                audio_path = download_audio(
                    video["video_id"], audio_dir
                )
                if not audio_path:
                    log(f"  ✗ Audio download failed", "error")
                    _update_video(db, job_id, video["video_id"],
                                  transcript_status="failed")
                    _inc_failed(db, job_id)
                    continue

                if config["transcription"]["dev_mode"]:
                    text, source = transcribe_with_groq(audio_path, config)
                else:
                    text, source = transcribe_with_gpu_server(audio_path, config)

                if not config["transcription"]["keep_audio"]:
                    try:
                        os.remove(audio_path)
                    except Exception:
                        pass

                if not text:
                    log(f"  ✗ Transcription failed", "error")
                    _update_video(db, job_id, video["video_id"],
                                  transcript_status="failed")
                    _inc_failed(db, job_id)
                    continue

            # Save transcript
            filepath = save_transcript(text, source, video, str(transcript_dir))
            log(f"  ✓ Transcript saved", "success")
            _update_video(db, job_id, video["video_id"],
                          transcript_status="done",
                          source=source)
            time.sleep(1)

        # ── Phase 4: Format blogs ─────────────────
        log("\n✍️ Formatting blog posts...", "info")

        from app.pipeline.blog_formatter import format_blog_with_groq, extract_meta, save_blog

        for video in videos:
            db_video = db.query(models.VideoResult).filter(
                models.VideoResult.job_id == job_id,
                models.VideoResult.video_id == video["video_id"],
            ).first()

            if not db_video or db_video.transcript_status != "done":
                continue

            title_short = video["title"][:50]
            log(f"\n[{video['index']:02d}] Formatting: {title_short}", "info")

            # Load transcript
            transcript_text = _load_transcript(
                str(transcript_dir), video["index"]
            )
            if not transcript_text:
                log(f"  ✗ Transcript file not found", "error")
                continue

            html = format_blog_with_groq(transcript_text, video["title"], config)
            if not html:
                log(f"  ✗ Blog formatting failed", "error")
                _update_video(db, job_id, video["video_id"],
                              blog_status="failed")
                _inc_failed(db, job_id)
                continue

            meta, tags = extract_meta(html)
            filepath, tags = save_blog(
                html, meta, tags, video, blog_dir
            )
            log(f"  ✓ Blog formatted — tags: {', '.join(tags[:3])}", "success")
            _update_video(db, job_id, video["video_id"],
                          blog_status="formatted")
            time.sleep(2)

        # ── Phase 5: Publish to Blogger ───────────
        log("\n🚀 Publishing to Blogger...", "info")

        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build

        # Build Blogger service using THIS USER's stored tokens
        creds = Credentials(
            token=None,
            refresh_token=decrypt(blogger_token.refresh_token),
            client_id=blogger_token.client_id,
            client_secret=blogger_token.client_secret,
            token_uri="https://oauth2.googleapis.com/token",
            scopes=["https://www.googleapis.com/auth/blogger"],
        )
        creds.refresh(Request())
        service = build("blogger", "v3", credentials=creds)

        import re

        done_count = 0
        failed_count = 0

        for video in videos:
            db_video = db.query(models.VideoResult).filter(
                models.VideoResult.job_id == job_id,
                models.VideoResult.video_id == video["video_id"],
            ).first()

            if not db_video or db_video.blog_status != "formatted":
                continue

            title_short = video["title"][:50]
            log(f"\n[{video['index']:02d}] Publishing: {title_short}", "info")

            # Load blog HTML
            html_content = _load_blog(str(blog_dir), video["index"])
            if not html_content:
                log(f"  ✗ Blog file not found", "error")
                failed_count += 1
                _inc_failed(db, job_id)
                continue

            # Extract title and content
            try:
                h1_match = re.search(r']*>(.*?)', html_content, re.IGNORECASE | re.DOTALL)
                post_title = re.sub(r']+>', '', h1_match.group(1)).strip() if h1_match else video["title"]
            except Exception:
                post_title = video["title"]

            try:
                tags_match = re.search(r'', html_content, re.DOTALL)
                tags = [t.strip() for t in tags_match.group(1).split(",")] if tags_match else []
            except Exception:
                tags = []

            content = re.sub(
                r'', '', html_content, flags=re.DOTALL
            ).strip()

            try:
                result = service.posts().insert(
                    blogId=blogger_token.blog_id,
                    body={
                        "title": post_title,
                        "content": content,
                        "labels": tags,
                    },
                    isDraft=post_as_draft,
                ).execute()

                post_url = result.get("url", "")
                mode = "draft" if post_as_draft else "live"
                log(f"  ✓ Posted as {mode}: {post_url}", "success")

                _update_video(
                    db, job_id, video["video_id"],
                    blog_status="done",
                    blog_url=post_url,
                )
                done_count += 1
                update_job(done_count=done_count)

            except Exception as e:
                log(f"  ✗ Publish failed: {str(e)[:120]}", "error")
                _update_video(db, job_id, video["video_id"],
                              blog_status="failed")
                failed_count += 1
                _inc_failed(db, job_id)

            time.sleep(2)

        # ── Done ─────────────────────────────────
        final_status = "done" if failed_count == 0 else "done"
        update_job(
            status=final_status,
            completed_at=datetime.utcnow(),
            done_count=done_count,
            failed_count=failed_count,
        )
        log(
            f"\n✅ Pipeline complete — "
            f"✓ {done_count} published  ✗ {failed_count} failed",
            "success"
        )

    except Exception as e:
        log(f"✗ Pipeline crashed: {str(e)}", "error")
        update_job(status="failed", completed_at=datetime.utcnow())
        raise

    finally:
        db.close()


# ── Helper functions ──────────────────────────────

def _update_video(db, job_id, video_id, **kwargs):
    db.query(models.VideoResult).filter(
        models.VideoResult.job_id == job_id,
        models.VideoResult.video_id == video_id,
    ).update(kwargs)
    db.commit()

def _inc_failed(db, job_id):
    job = db.query(models.Job).filter(
        models.Job.id == job_id
    ).first()
    if job:
        job.failed_count = (job.failed_count or 0) + 1
        db.commit()

def _load_transcript(transcript_dir, index):
    for f in os.listdir(transcript_dir):
        if f.startswith(f"{index:02d}_") and f.endswith(".txt"):
            with open(os.path.join(transcript_dir, f), "r", encoding="utf-8") as fh:
                return fh.read()
    return None

def _load_blog(blog_dir, index):
    for f in os.listdir(blog_dir):
        if f.startswith(f"{index:02d}_") and f.endswith(".html"):
            with open(os.path.join(blog_dir, f), "r", encoding="utf-8") as fh:
                return fh.read()
    return None


def _normalize_channel_name(channel_url):
    channel_url = channel_url.rstrip("/")
    parts = [part for part in channel_url.split("/") if part]
    if not parts:
        return "channel"

    last_part = parts[-1]
    if last_part == "videos" and len(parts) >= 2:
        last_part = parts[-2]

    return last_part.lstrip("@").replace("/", "_") or "channel"
