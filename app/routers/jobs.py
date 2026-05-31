import asyncio
import json
import re

from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.dependencies import get_current_user

router = APIRouter(prefix="/jobs", tags=["jobs"])
templates = Jinja2Templates(directory="app/templates")


@router.post("/submit")
def submit_job(
    request: Request,
    channel_url: str = Form(...),
    start_idx: int = Form(1),
    end_idx: int = Form(None),
    post_as_draft: bool = Form(False),
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    channel_url = channel_url.strip()
    if not re.match(r'^https?://(www\.)?youtube\.com/(@[\w.-]+|channel/UC[\w-]{22})', channel_url):
        raise HTTPException(status_code=400, detail="Invalid YouTube channel URL")

    # Max 50 videos per job
    if end_idx and start_idx and (end_idx - start_idx + 1) > 50:
        raise HTTPException(status_code=400, detail="Maximum 50 videos per job")

    # Max 2 concurrent jobs per user
    running = db.query(models.Job).filter(
        models.Job.user_id == user.id,
        models.Job.status.in_(["queued", "running"])
    ).count()
    if running >= 2:
        raise HTTPException(status_code=429, detail="You already have jobs running. Wait for them to finish.")

    channel_name = channel_url.rstrip("/").split("/")[-1].replace("@", "")

    job = models.Job(
        user_id=user.id,
        channel_url=channel_url,
        channel_name=channel_name,
        start_idx=start_idx,
        end_idx=end_idx,
        status="queued",
    )
    db.add(job)
    db.commit()
    db.refresh(job)

    try:
        from workers.pipeline_worker import run_pipeline

        run_pipeline.delay(job.id, post_as_draft)
    except Exception as exc:
        print(f"Worker queue error: {exc}")

    return RedirectResponse(url=f"/jobs/{job.id}/progress", status_code=302)


@router.get("/{job_id}/progress", response_class=HTMLResponse)
def job_progress(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    job = (
        db.query(models.Job)
        .filter(models.Job.id == job_id, models.Job.user_id == user.id)
        .first()
    )

    if not job:
        return RedirectResponse("/dashboard/")

    logs = (
        db.query(models.ProgressLog)
        .filter(models.ProgressLog.job_id == job_id)
        .order_by(models.ProgressLog.created_at)
        .all()
    )

    return templates.TemplateResponse(
        "progress.html",
        {
            "request": request,
            "user": user,
            "job": job,
            "logs": logs,
        },
    )


@router.get("/{job_id}/stream")
async def job_stream(
    job_id: str,
    request: Request,
    db: Session = Depends(get_db),
):
    async def event_generator():
        last_id = ""
        while True:
            if await request.is_disconnected():
                break

            logs = (
                db.query(models.ProgressLog)
                .filter(models.ProgressLog.job_id == job_id, models.ProgressLog.id > last_id)
                .order_by(models.ProgressLog.created_at)
                .all()
            )

            for log in logs:
                last_id = log.id
                data = json.dumps(
                    {
                        "type": "log",
                        "message": log.message,
                        "level": log.level,
                        "time": log.created_at.strftime("%H:%M:%S"),
                    }
                )
                yield f"data: {data}\n\n"

            job = db.query(models.Job).filter(models.Job.id == job_id).first()
            if job:
                stats = json.dumps(
                    {
                        "type": "stats",
                        "done": job.done_count,
                        "failed": job.failed_count,
                        "total": job.total_videos,
                    }
                )
                yield f"data: {stats}\n\n"

                if job.status in ("done", "failed"):
                    status_data = json.dumps(
                        {
                            "type": "status",
                            "status": job.status,
                        }
                    )
                    yield f"data: {status_data}\n\n"
                    break

            await asyncio.sleep(2)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )
