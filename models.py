from datetime import datetime
from sqlalchemy import DateTime, Integer, JSON, String, Text, Index
from sqlalchemy.orm import Mapped, mapped_column
try:
    from .database import Base
except ImportError:
    from database import Base


class Server(Base):
    __tablename__ = "servers"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    label: Mapped[str] = mapped_column(String(80))
    host: Mapped[str] = mapped_column(String(255))
    ssh_port: Mapped[int] = mapped_column(Integer, default=22)
    status: Mapped[str] = mapped_column(String(32), default="AWAITING_BOOTSTRAP")
    public_key: Mapped[str] = mapped_column(Text)
    encrypted_private_key: Mapped[str] = mapped_column(Text)
    token_digest: Mapped[str] = mapped_column(String(64), unique=True)
    token_expires_at: Mapped[datetime] = mapped_column(DateTime)
    facts: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    bootstrapped_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


class DeploymentJob(Base):
    __tablename__ = "deployment_jobs"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    repository_url: Mapped[str] = mapped_column(String(255), index=True)
    owner: Mapped[str] = mapped_column(String(39))
    repo: Mapped[str] = mapped_column(String(100))
    state: Mapped[str] = mapped_column(String(32), default="QUEUED", index=True)
    retry_budget: Mapped[int] = mapped_column(Integer, default=3)
    repository_metadata: Mapped[dict] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    enqueued_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)


Index("ix_deployment_jobs_repo_active", DeploymentJob.repository_url, DeploymentJob.state)


class RepositoryAnalysis(Base):
    __tablename__ = "repository_analyses"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    job_id: Mapped[str] = mapped_column(String(36), index=True)
    repository_url: Mapped[str] = mapped_column(String(255), index=True)
    commit_sha: Mapped[str] = mapped_column(String(40), index=True)
    detection: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)


Index("uq_repository_analysis_commit", RepositoryAnalysis.repository_url, RepositoryAnalysis.commit_sha, unique=True)
