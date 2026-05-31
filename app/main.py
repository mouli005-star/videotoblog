from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from app.database import init_db
from app.database import get_db
from app.dependencies import get_optional_user


@asynccontextmanager
async def lifespan(app: FastAPI):
    # runs on startup
    init_db()
    yield
    # runs on shutdown


app = FastAPI(
    title="YT to Blog",
    description="Auto-convert YouTube channels to Blogger posts",
    lifespan=lifespan,
)

app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")

from app.routers import auth, dashboard, jobs

app.include_router(auth.router)
app.include_router(dashboard.router)
app.include_router(jobs.router)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/", response_class=HTMLResponse)
def index(request: Request, db=Depends(get_db)):
    user = get_optional_user(request, db)
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "user": user,
        },
    )
