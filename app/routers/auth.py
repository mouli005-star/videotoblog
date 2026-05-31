import os

import httpx
from dotenv import load_dotenv
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from sqlalchemy.orm import Session

from app import models
from app.auth_utils import create_session_token
from app.crypto import encrypt
from app.database import get_db

load_dotenv()

router = APIRouter(prefix="/auth", tags=["auth"])

GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI")

GOOGLE_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_USER_URL = "https://www.googleapis.com/oauth2/v2/userinfo"

SCOPES = [
    "openid",
    "email",
    "profile",
    "https://www.googleapis.com/auth/blogger",
]


@router.get("/login")
def login():
    """Redirect user to Google OAuth consent screen."""
    scope_str = " ".join(SCOPES)
    url = (
        f"{GOOGLE_AUTH_URL}"
        f"?client_id={GOOGLE_CLIENT_ID}"
        f"&redirect_uri={GOOGLE_REDIRECT_URI}"
        f"&response_type=code"
        f"&scope={scope_str}"
        f"&access_type=offline"
        f"&prompt=consent"
    )
    return RedirectResponse(url)


@router.get("/callback")
async def callback(code: str = None, error: str = None, db: Session = Depends(get_db)):
    """Google redirects here after user approves."""

    if error or not code:
        raise HTTPException(status_code=400, detail=f"OAuth error: {error}")

    async with httpx.AsyncClient() as client:
        token_resp = await client.post(
            GOOGLE_TOKEN_URL,
            data={
                "code": code,
                "client_id": GOOGLE_CLIENT_ID,
                "client_secret": GOOGLE_CLIENT_SECRET,
                "redirect_uri": GOOGLE_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )

    token_data = token_resp.json()

    if "error" in token_data:
        raise HTTPException(status_code=400, detail=f"Token exchange failed: {token_data}")

    access_token = token_data.get("access_token")
    refresh_token = token_data.get("refresh_token")

    async with httpx.AsyncClient() as client:
        user_resp = await client.get(
            GOOGLE_USER_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )

    user_info = user_resp.json()
    google_id = user_info.get("id")
    email = user_info.get("email")
    name = user_info.get("name")
    picture = user_info.get("picture")

    blog_id = None
    blog_name = None
    blog_url = None

    async with httpx.AsyncClient() as client:
        blog_resp = await client.get(
            "https://www.googleapis.com/blogger/v3/users/self/blogs",
            headers={"Authorization": f"Bearer {access_token}"},
        )

    blog_data = blog_resp.json()
    blogs = blog_data.get("items", [])
    if blogs:
        blog_id = blogs[0].get("id")
        blog_name = blogs[0].get("name")
        blog_url = blogs[0].get("url")

    user = db.query(models.User).filter(models.User.google_id == google_id).first()

    if not user:
        user = models.User(
            email=email,
            google_id=google_id,
            name=name,
            picture=picture,
        )
        db.add(user)
        db.flush()

    blogger_token = db.query(models.BloggerToken).filter(models.BloggerToken.user_id == user.id).first()

    if blogger_token:
        if refresh_token:
            blogger_token.refresh_token = encrypt(refresh_token)
        blogger_token.blog_id = blog_id or blogger_token.blog_id
        blogger_token.blog_name = blog_name or blogger_token.blog_name
        blogger_token.blog_url = blog_url or blogger_token.blog_url
    else:
        if not refresh_token:
            raise HTTPException(
                status_code=400,
                detail="No refresh token received. Revoke app access at myaccount.google.com and try again.",
            )
        blogger_token = models.BloggerToken(
            user_id=user.id,
            blog_id=blog_id or "",
            blog_name=blog_name or "",
            blog_url=blog_url or "",
            refresh_token=encrypt(refresh_token),
            client_id=GOOGLE_CLIENT_ID,
            client_secret=GOOGLE_CLIENT_SECRET,
        )
        db.add(blogger_token)

    db.commit()
    db.refresh(user)

    session_token = create_session_token(user.id, user.email)
    response = RedirectResponse(url="/dashboard/", status_code=302)
    response.set_cookie(
        key="session",
        value=session_token,
        httponly=True,
        secure=os.getenv("ENV", "dev") == "production",
        max_age=60 * 60 * 24 * 30,
        samesite="lax",
    )
    return response


@router.get("/logout")
def logout():
    response = RedirectResponse(url="/", status_code=302)
    response.delete_cookie("session")
    return response


@router.get("/me")
async def me(request: Request, db: Session = Depends(get_db)):
    """Quick check — returns current user info."""
    from app.dependencies import get_current_user

    user = get_current_user(request, db)
    return {
        "id": user.id,
        "email": user.email,
        "name": user.name,
        "blog": user.blogger_token.blog_name if user.blogger_token else None,
    }
