# yt-to-blog-web

A FastAPI application that converts YouTube channel transcripts into blog posts and publishes them to Blogger.

## Features

- Fetch videos from a YouTube channel
- Transcribe or use captions
- Format transcripts into blog HTML
- Publish to Blogger using OAuth2 (refresh tokens encrypted at rest)
- Celery + Redis worker pipeline
- Per-channel job folders under `channels/`

## Prerequisites

- Docker & Docker Compose (recommended)
- Python 3.11+ (for local development)
- A Google OAuth client with Blogger API enabled

## Quick start (Docker)

1. Copy the example env and edit secrets:

```powershell
cp .env.example .env
# generate a secure SECRET_KEY and ENCRYPTION_KEY and paste into .env
```

2. Bring up the stack:

```powershell
docker-compose up --build
```

3. Visit `http://localhost` (or `http://localhost:8000`) and login via **Auth → Google**. After granting access, the app stores an encrypted refresh token.

4. Submit a job from the dashboard to convert a channel's videos into blog posts.

## Quick start (local development)

1. Create and activate a venv, then install deps:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements-local.txt
```

2. Create `.env` (see `.env.example`) and generate keys:

```powershell
# SECRET_KEY (64 hex chars)
python -c "import secrets; print(secrets.token_hex(32))"

# ENCRYPTION_KEY (Fernet)
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

Paste the outputs into your `.env` as `SECRET_KEY` and `ENCRYPTION_KEY`.

3. Run Postgres/Redis (Docker recommended) or point `DATABASE_URL`/`REDIS_URL` to your instances.

4. Start the app and worker (example shown in this project):

```powershell
# start app
uvicorn app.main:app --reload

# start worker (example env injection shown in repo)
$env:PYTHONPATH='C:\path\to\repo'
$env:REDIS_URL='redis://localhost:6379/0'
$env:DATABASE_URL='postgresql://postgres:postgres@localhost:5432/ytblog'
$env:ENCRYPTION_KEY='<your ENCRYPTION_KEY>'
celery -A workers.pipeline_worker worker --loglevel=info --concurrency=2 -P solo
```

## Security notes

- `ENCRYPTION_KEY` is required by the worker to decrypt refresh tokens.
- `.gitignore` excludes `.env`, `cookies.txt`, `channels/` and other sensitive files — do not commit secrets.
- Set `ENV=production` in `.env` on your production server so cookies are set `secure=True`.

## Admin / maintenance

- To purge stored Blogger tokens (so users must re-authenticate):

```powershell
docker-compose exec db psql -U postgres -d ytblog -c "DELETE FROM blogger_tokens;"
```

- Check Redis Celery queue length:

```powershell
docker-compose exec redis redis-cli LLEN celery
```

- If Celery workers report large clock drift, restart Docker / host VM to resync clocks.

## Where to look in the code

- API routes: `app/routers/`
- Job pipeline: `workers/pipeline_worker.py`
- Encryption helper: `app/crypto.py`
- Blog formatting / pipeline: `app/pipeline/`

## Support

If you want, I can:
- Add deployment instructions for DigitalOcean App Platform or a Docker droplet
- Add CI that checks for missing secrets before PRs


