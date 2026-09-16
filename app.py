import os
import logging
import smtplib
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncGenerator
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from fastapi import Depends, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi_users import BaseUserManager, FastAPIUsers, UUIDIDMixin, schemas
from fastapi_users.authentication import AuthenticationBackend, BearerTransport, JWTStrategy
from fastapi_users.db import SQLAlchemyBaseUserTableUUID, SQLAlchemyUserDatabase
from dotenv import load_dotenv
from pydantic import BaseModel, Field
from sqlalchemy import DateTime, Float, ForeignKey, Integer, String, Text, UniqueConstraint
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


APP_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_TIERS = '{"onTime":"10:00","t1":"11:00","t2":"13:00","t3":"16:00"}'
load_dotenv()
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./attendance_fastapi.db")
engine_options = {}
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql+asyncpg://", 1)
elif DATABASE_URL.startswith("postgresql://"):
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+asyncpg://", 1)
if DATABASE_URL.startswith("postgresql+asyncpg://"):
    parsed_database_url = urlsplit(DATABASE_URL)
    database_query = dict(parse_qsl(parsed_database_url.query, keep_blank_values=True))
    ssl_mode = database_query.pop("sslmode", "")
    DATABASE_URL = urlunsplit(
        parsed_database_url._replace(query=urlencode(database_query))
    )
    if ssl_mode.lower() == "require":
        engine_options["connect_args"] = {"ssl": "require"}
SECRET = os.getenv("SECRET", "change-this-in-render-before-deploying")
SMTP_HOST = os.getenv("SMTP_HOST", "")
SMTP_PORT = int(os.getenv("SMTP_PORT", "587"))
SMTP_USERNAME = os.getenv("SMTP_USERNAME", "")
SMTP_PASSWORD = os.getenv("SMTP_PASSWORD", "")
EMAIL_FROM = os.getenv("EMAIL_FROM", SMTP_USERNAME)
FRONTEND_URL = os.getenv("FRONTEND_URL", "http://localhost:8000").rstrip("/")
logger = logging.getLogger(__name__)

engine = create_async_engine(DATABASE_URL, **engine_options)
async_session_maker = async_sessionmaker(engine, expire_on_commit=False)


class Base(DeclarativeBase):
    pass


class User(SQLAlchemyBaseUserTableUUID, Base):
    __tablename__ = "user"
    display_name: Mapped[str] = mapped_column(String(80), default="")
    monthly_salary: Mapped[float] = mapped_column(Float, default=0)
    working_days: Mapped[int] = mapped_column(Integer, default=26)
    tiers: Mapped[str] = mapped_column(Text, default=DEFAULT_TIERS)


class Record(Base):
    __tablename__ = "record"
    __table_args__ = (UniqueConstraint("user_id", "date_key", name="unique_user_record_date"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    date_key: Mapped[str] = mapped_column(String(10))
    timestamp: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    pct: Mapped[int] = mapped_column(Integer, default=0)
    label: Mapped[str] = mapped_column(String(30))


class Reminder(Base):
    __tablename__ = "reminder"
    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("user.id", ondelete="CASCADE"), index=True)
    title: Mapped[str] = mapped_column(String(150))
    note: Mapped[str] = mapped_column(Text, default="")
    date_str: Mapped[str] = mapped_column(String(20), default="")


class UserRead(schemas.BaseUser[uuid.UUID]):
    display_name: str


class UserCreate(schemas.BaseUserCreate):
    display_name: str = Field(min_length=1, max_length=80)


class UserUpdate(schemas.BaseUserUpdate):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)


class UserManager(UUIDIDMixin, BaseUserManager[User, uuid.UUID]):
    reset_password_token_secret = SECRET
    verification_token_secret = SECRET

    async def on_after_forgot_password(self, user: User, token: str, request=None) -> None:
        """Send a one-time reset link only to the email stored on the account."""
        if not SMTP_HOST or not EMAIL_FROM:
            logger.error("Password reset requested but SMTP is not configured")
            return

        reset_url = f"{FRONTEND_URL}/?reset_token={token}"
        message = (
            "We received a request to reset your Attendance password.\n\n"
            f"Set a new password here: {reset_url}\n\n"
            "If you did not request this, you can safely ignore this email."
        )
        from email.message import EmailMessage

        email = EmailMessage()
        email["Subject"] = "Reset your Attendance password"
        email["From"] = EMAIL_FROM
        email["To"] = user.email
        email.set_content(message)

        try:
            if os.getenv("SMTP_USE_SSL", "false").lower() == "true":
                with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                    if SMTP_USERNAME:
                        server.login(SMTP_USERNAME, SMTP_PASSWORD)
                    server.send_message(email)
            else:
                with smtplib.SMTP(SMTP_HOST, SMTP_PORT, timeout=15) as server:
                    server.ehlo()
                    if os.getenv("SMTP_USE_TLS", "true").lower() == "true":
                        server.starttls()
                        server.ehlo()
                    if SMTP_USERNAME:
                        server.login(SMTP_USERNAME, SMTP_PASSWORD)
                    server.send_message(email)
        except (OSError, smtplib.SMTPException):
            logger.exception("Could not send password reset email")


async def get_async_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session_maker() as session:
        yield session


async def get_user_db(session: AsyncSession = Depends(get_async_session)) -> AsyncGenerator[SQLAlchemyUserDatabase, None]:
    yield SQLAlchemyUserDatabase(session, User)


async def get_user_manager(user_db: SQLAlchemyUserDatabase = Depends(get_user_db)) -> AsyncGenerator[UserManager, None]:
    yield UserManager(user_db)


bearer_transport = BearerTransport(tokenUrl="auth/jwt/login")


def get_jwt_strategy() -> JWTStrategy:
    return JWTStrategy(secret=SECRET, lifetime_seconds=int(timedelta(days=7).total_seconds()))


auth_backend = AuthenticationBackend(name="jwt", transport=bearer_transport, get_strategy=get_jwt_strategy)
fastapi_users = FastAPIUsers[User, uuid.UUID](get_user_manager, [auth_backend])
current_active_user = fastapi_users.current_user(active=True)


class SettingsInput(BaseModel):
    monthly_salary: float = 0
    working_days: int = 26
    tiers: dict[str, str]


class PunchInput(BaseModel):
    date_key: str
    pct: int = Field(ge=0, le=100)
    label: str = Field(max_length=30)
    timestamp: datetime | None = None


class RecordUpdate(PunchInput):
    timestamp: datetime


class DateInput(BaseModel):
    date_key: str


class ReminderInput(BaseModel):
    title: str = Field(min_length=1, max_length=150)
    note: str = ""
    date: str = ""


class ReminderDelete(BaseModel):
    id: int


@asynccontextmanager
async def lifespan(_: FastAPI):
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


app = FastAPI(title="Attendance API", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[item for item in os.getenv("CORS_ORIGINS", "").split(",") if item],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.include_router(fastapi_users.get_auth_router(auth_backend), prefix="/auth/jwt", tags=["auth"])
app.include_router(fastapi_users.get_register_router(UserRead, UserCreate), prefix="/auth", tags=["auth"])
app.include_router(fastapi_users.get_reset_password_router(), prefix="/auth", tags=["auth"])
app.mount("/static", StaticFiles(directory=APP_DIR), name="static")


@app.get("/healthz")
async def health_check():
    return {"status": "ok"}


def profile(user: User) -> dict:
    return {
        "id": str(user.id), "username": user.display_name, "email": user.email,
        "monthly_salary": user.monthly_salary, "working_days": user.working_days,
        "tiers": __import__("json").loads(user.tiers or DEFAULT_TIERS),
    }


@app.get("/api/me")
async def get_me(user: User = Depends(current_active_user)):
    return profile(user)


@app.post("/api/user/settings")
async def update_settings(data: SettingsInput, session: AsyncSession = Depends(get_async_session), user: User = Depends(current_active_user)):
    user.monthly_salary = data.monthly_salary
    user.working_days = data.working_days
    user.tiers = __import__("json").dumps(data.tiers)
    session.add(user)
    await session.commit()
    return {"status": "ok"}


@app.post("/api/punch")
async def punch(data: PunchInput, session: AsyncSession = Depends(get_async_session), user: User = Depends(current_active_user)):
    from sqlalchemy import select
    result = await session.execute(select(Record).where(Record.user_id == user.id, Record.date_key == data.date_key))
    record = result.scalar_one_or_none()
    # Use the server's clock so a client cannot backdate attendance by changing its device time.
    timestamp = datetime.now(timezone.utc)
    if record is None:
        session.add(Record(user_id=user.id, date_key=data.date_key, pct=data.pct, label=data.label, timestamp=timestamp))
    else:
        record.pct, record.label, record.timestamp = data.pct, data.label, timestamp
    await session.commit()
    return {"status": "ok"}


@app.get("/api/records")
async def get_records(session: AsyncSession = Depends(get_async_session), user: User = Depends(current_active_user)):
    from sqlalchemy import select
    result = await session.execute(select(Record).where(Record.user_id == user.id).order_by(Record.date_key))
    return [{"id": record.id, "date": record.date_key, "pct": record.pct, "label": record.label, "timestamp": record.timestamp.isoformat()} for record in result.scalars()]


@app.post("/api/records/update")
async def update_record(data: RecordUpdate, session: AsyncSession = Depends(get_async_session), user: User = Depends(current_active_user)):
    from sqlalchemy import select
    result = await session.execute(select(Record).where(Record.user_id == user.id, Record.date_key == data.date_key))
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(404, "Record not found")
    record.pct, record.label, record.timestamp = data.pct, data.label, data.timestamp
    await session.commit()
    return {"status": "ok"}


@app.post("/api/records/delete")
async def delete_record(data: DateInput, session: AsyncSession = Depends(get_async_session), user: User = Depends(current_active_user)):
    from sqlalchemy import select
    result = await session.execute(select(Record).where(Record.user_id == user.id, Record.date_key == data.date_key))
    record = result.scalar_one_or_none()
    if record is None:
        raise HTTPException(404, "Record not found")
    await session.delete(record)
    await session.commit()
    return {"status": "ok"}


@app.get("/api/reminders")
async def get_reminders(session: AsyncSession = Depends(get_async_session), user: User = Depends(current_active_user)):
    from sqlalchemy import select
    result = await session.execute(select(Reminder).where(Reminder.user_id == user.id).order_by(Reminder.id))
    return [{"id": item.id, "title": item.title, "note": item.note, "date": item.date_str} for item in result.scalars()]


@app.post("/api/reminders/add")
async def add_reminder(data: ReminderInput, session: AsyncSession = Depends(get_async_session), user: User = Depends(current_active_user)):
    item = Reminder(user_id=user.id, title=data.title, note=data.note, date_str=data.date)
    session.add(item)
    await session.commit()
    return {"status": "ok", "id": item.id}


@app.post("/api/reminders/delete")
async def delete_reminder(data: ReminderDelete, session: AsyncSession = Depends(get_async_session), user: User = Depends(current_active_user)):
    item = await session.get(Reminder, data.id)
    if item is None or item.user_id != user.id:
        raise HTTPException(404, "Reminder not found")
    await session.delete(item)
    await session.commit()
    return {"status": "ok"}


@app.get("/")
async def serve_html():
    return FileResponse(os.path.join(APP_DIR, "attendance.html"))


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app:app", host="0.0.0.0", port=int(os.getenv("PORT", "8000")))
