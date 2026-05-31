from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app import models
from app.database import get_db
from app.dependencies import get_current_user

router = APIRouter(prefix="/dashboard", tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")


@router.get("/", response_class=HTMLResponse)
def dashboard(
    request: Request,
    db: Session = Depends(get_db),
    user=Depends(get_current_user),
):
    if not user.blogger_token or not user.blogger_token.blog_id:
        return templates.TemplateResponse(
            "no_blog.html",
            {
                "request": request,
                "user": user,
            },
        )

    jobs = (
        db.query(models.Job)
        .filter(models.Job.user_id == user.id)
        .order_by(models.Job.created_at.desc())
        .all()
    )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "blogger": user.blogger_token,
            "jobs": jobs,
        },
    )
