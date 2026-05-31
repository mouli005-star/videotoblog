import uuid

from sqlalchemy import Boolean, Column, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.database import Base


def gen_uuid():
    return str(uuid.uuid4())


class User(Base):
    __tablename__ = "users"

    id = Column(String, primary_key=True, default=gen_uuid)
    email = Column(String, unique=True, nullable=False, index=True)
    google_id = Column(String, unique=True, nullable=False)
    name = Column(String)
    picture = Column(String)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, server_default=func.now())

    blogger_token = relationship("BloggerToken", back_populates="user", uselist=False)
    jobs = relationship("Job", back_populates="user")


class BloggerToken(Base):
    __tablename__ = "blogger_tokens"

    id = Column(String, primary_key=True, default=gen_uuid)
    user_id = Column(String, ForeignKey("users.id"), unique=True)
    blog_id = Column(String, nullable=False)
    blog_name = Column(String)
    blog_url = Column(String)
    refresh_token = Column(Text, nullable=False)
    client_id = Column(String, nullable=False)
    client_secret = Column(String, nullable=False)
    connected_at = Column(DateTime, server_default=func.now())

    user = relationship("User", back_populates="blogger_token")


class Job(Base):
    __tablename__ = "jobs"

    id = Column(String, primary_key=True, default=gen_uuid)
    user_id = Column(String, ForeignKey("users.id"))
    channel_url = Column(String, nullable=False)
    channel_name = Column(String)
    start_idx = Column(Integer, default=1)
    end_idx = Column(Integer, nullable=True)
    status = Column(String, default="queued")
    # queued | running | done | failed
    total_videos = Column(Integer, default=0)
    done_count = Column(Integer, default=0)
    failed_count = Column(Integer, default=0)
    created_at = Column(DateTime, server_default=func.now())
    completed_at = Column(DateTime, nullable=True)

    user = relationship("User", back_populates="jobs")
    videos = relationship("VideoResult", back_populates="job")
    logs = relationship("ProgressLog", back_populates="job")


class VideoResult(Base):
    __tablename__ = "video_results"

    id = Column(String, primary_key=True, default=gen_uuid)
    job_id = Column(String, ForeignKey("jobs.id"))
    video_id = Column(String)
    title = Column(String)
    duration_sec = Column(Integer, nullable=True)
    transcript_status = Column(String, default="pending")
    blog_status = Column(String, default="pending")
    blog_url = Column(String, nullable=True)
    source = Column(String, nullable=True)

    job = relationship("Job", back_populates="videos")


class ProgressLog(Base):
    __tablename__ = "progress_logs"

    id = Column(String, primary_key=True, default=gen_uuid)
    job_id = Column(String, ForeignKey("jobs.id"), index=True)
    message = Column(Text)
    level = Column(String, default="info")
    # info | success | error | warning
    created_at = Column(DateTime, server_default=func.now())

    job = relationship("Job", back_populates="logs")
