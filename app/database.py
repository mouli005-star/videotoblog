import os
import time

from dotenv import load_dotenv
from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, sessionmaker

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL")

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def init_db():
    from app import models

    for attempt in range(30):
        try:
            with engine.connect():
                break
        except OperationalError:
            if attempt == 29:
                raise
            time.sleep(1)

    Base.metadata.create_all(bind=engine)
    print("✓ Database tables created")


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
