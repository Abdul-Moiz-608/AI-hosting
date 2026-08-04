from contextlib import asynccontextmanager
import asyncio
import logging
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urlparse
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session
import uvicorn

try:
    from .config import get_settings
    from .database import Base, engine, get_db
    from .models import DeploymentJob, Server
    from .github import GitHubFetchError, GitHubRateLimitError, GitHubTimeoutError, RepositoryNotFound, RepositoryUrlError, fetch_metadata, parse_repository_url
    from .deployment_queue import QueueUnavailable, enqueue_job
    from .security import LocalVault, generate_ssh_keypair, new_bootstrap_token, token_digest
    from .tunnels import PinggyTunnel, TunnelError, TunnelManager
except ImportError:  # Supports the existing direct `python main.py` entrypoint.
    from config import get_settings
    from database import Base, engine, get_db
    from models import DeploymentJob, Server
    from github import GitHubFetchError, GitHubRateLimitError, GitHubTimeoutError, RepositoryNotFound, RepositoryUrlError, fetch_metadata, parse_repository_url
    from deployment_queue import QueueUnavailable, enqueue_job
    from security import LocalVault, generate_ssh_keypair, new_bootstrap_token, token_digest
    from tunnels import PinggyTunnel, TunnelError, TunnelManager

settings = get_settings()
logger = logging.getLogger(__name__)
APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
Base.metadata.create_all(bind=engine)
tunnel_manager = TunnelManager()
TUNNEL_DEPLOYMENT_ID = "api"


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Run tunnel start in a thread pool so Uvicorn can start listening immediately
    loop = asyncio.get_running_loop()
    loop.run_in_executor(None, _safe_start_tunnel)
    try:
        yield
    finally:
        tunnel_manager.stop_all()


def _safe_start_tunnel():
    try:
        url = start_pinggy_tunnel()
        print(f"\n{'='*60}")
        print(f"  Pinggy tunnel active: {url}")
        print(f"{'='*60}\n")
    except TunnelError as exc:
        print(f"\n[WARNING] Could not auto-start Pinggy tunnel: {exc}\n")


app = FastAPI(title=settings.app_name, version="0.1.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in settings.cors_origins.split(",")], allow_methods=["*"], allow_headers=["*"])
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


class ServerCreate(BaseModel):
    label: str = Field(min_length=2, max_length=80)
    host: str = Field(min_length=1, max_length=255)
    ssh_port: int = Field(default=22, ge=1, le=65535)


class FactReport(BaseModel):
    os: str = "unknown"
    kernel: str = "unknown"
    ram_mb: int = 0
    disk_gb: int = 0
    docker_version: str = "not installed"
    sshd: str = "unknown"


class RepositoryRequest(BaseModel):
    repository_url: str = Field(min_length=1, max_length=512)


class DeploymentRequest(BaseModel):
    repository_url: str = Field(min_length=1, max_length=512)
    repository_metadata: dict | None = None


def serialize(server: Server) -> dict:
    return {"id": server.id, "label": server.label, "host": server.host, "ssh_port": server.ssh_port, "status": server.status, "facts": server.facts, "created_at": server.created_at.isoformat()}


def public_api_base_url() -> str:
    """Return the externally reachable API URL used in VPS bootstrap commands."""
    tunnel_url = tunnel_manager.public_url_for(TUNNEL_DEPLOYMENT_ID)
    if tunnel_url:
        return tunnel_url
    base_url = settings.public_api_base_url.strip().rstrip("/")
    parsed = urlparse(base_url)
    blocked_hosts = {"localhost", "127.0.0.1", "::1", "0.0.0.0", "your_public_api_domain"}
    if parsed.scheme != "https" or not parsed.hostname or parsed.hostname.lower() in blocked_hosts:
        raise HTTPException(503, "PUBLIC_API_BASE_URL must be an externally reachable HTTPS URL, not localhost.")
    return base_url


def local_api_url() -> str:
    return f"http://127.0.0.1:{settings.api_port}"


def start_pinggy_tunnel() -> str:
    existing_url = tunnel_manager.public_url_for(TUNNEL_DEPLOYMENT_ID)
    if existing_url:
        return existing_url
    tunnel = PinggyTunnel(
        local_port=settings.api_port,
        health_path=settings.pinggy_health_path,
        timeout_seconds=settings.pinggy_timeout_seconds,
        ssh_host=settings.pinggy_ssh_host,
        ssh_port=settings.pinggy_ssh_port,
        token=settings.pinggy_token,
    )   
    return tunnel_manager.start_for(TUNNEL_DEPLOYMENT_ID, tunnel)


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(STATIC_DIR / "index.html")


@app.get("/api/health")
def health():
    return {"status": "ok", "environment": settings.environment}


@app.post("/api/repositories/metadata")
def repository_metadata(payload: RepositoryRequest):
    try:
        repository = parse_repository_url(payload.repository_url)
        return fetch_metadata(repository)
    except RepositoryUrlError as exc:
        logger.warning("Rejected invalid GitHub repository URL")
        raise HTTPException(422, str(exc)) from exc
    except RepositoryNotFound as exc:
        logger.info("GitHub repository not found")
        raise HTTPException(404, str(exc)) from exc
    except GitHubRateLimitError as exc:
        logger.warning("GitHub API rate limit reached")
        raise HTTPException(429, str(exc), headers={"Retry-After": "60"}) from exc
    except GitHubTimeoutError as exc:
        logger.warning("GitHub metadata request timed out")
        raise HTTPException(504, str(exc)) from exc
    except GitHubFetchError as exc:
        logger.warning("GitHub metadata fetch failed")
        raise HTTPException(502, str(exc)) from exc


@app.post("/api/deployment-jobs", status_code=202)
def create_deployment_job(payload: DeploymentRequest, db: Session = Depends(get_db)):
    try:
        repository = parse_repository_url(payload.repository_url)
    except RepositoryUrlError as exc:
        logger.warning("Rejected invalid GitHub repository URL for deployment")
        raise HTTPException(422, str(exc)) from exc

    # The browser normally sends metadata obtained by the intake endpoint. A
    # direct caller may omit it; in that case, fetch it before creating a job.
    metadata = payload.repository_metadata
    if not metadata:
        try:
            metadata = fetch_metadata(repository)
        except RepositoryNotFound as exc:
            logger.info("GitHub repository not found during deployment intake")
            raise HTTPException(404, str(exc)) from exc
        except GitHubRateLimitError as exc:
            logger.warning("GitHub API rate limit reached during deployment intake")
            raise HTTPException(429, str(exc), headers={"Retry-After": "60"}) from exc
        except GitHubTimeoutError as exc:
            logger.warning("GitHub metadata request timed out during deployment intake")
            raise HTTPException(504, str(exc)) from exc
        except GitHubFetchError as exc:
            logger.warning("GitHub metadata fetch failed during deployment intake")
            raise HTTPException(502, str(exc)) from exc

    try:
        duplicate = db.query(DeploymentJob).filter(
            DeploymentJob.repository_url == repository.normalized_url,
            DeploymentJob.state.in_(["QUEUED", "RUNNING"]),
        ).first()
        if duplicate:
            logger.info("Duplicate deployment job rejected for %s", repository.normalized_url)
            raise HTTPException(409, "A deployment job for this repository is already queued or running.")
        job = DeploymentJob(
            id=str(uuid4()), repository_url=repository.normalized_url,
            owner=repository.owner, repo=repository.repo, state="QUEUED",
            retry_budget=3, repository_metadata=metadata,
        )
        db.add(job)
        db.commit()
        db.refresh(job)
    except HTTPException:
        raise
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("Could not save deployment job")
        raise HTTPException(503, "The deployment service could not save this job. Please try again.") from exc

    try:
        enqueue_job(job.id)
    except QueueUnavailable as exc:
        logger.warning("Deployment queue unavailable for job %s", job.id)
        job.state = "FAILED"
        job.error = "Queue unavailable"
        try:
            db.commit()
        except SQLAlchemyError:
            db.rollback()
            logger.exception("Could not record failed enqueue for job %s", job.id)
        raise HTTPException(503, str(exc)) from exc
    job.enqueued_at = datetime.utcnow()
    try:
        db.commit()
    except SQLAlchemyError as exc:
        db.rollback()
        logger.exception("Could not record enqueue timestamp for job %s", job.id)
        # The job is already in Redis; the worker can still safely use its ID.
        raise HTTPException(503, "The deployment job was queued but could not be finalized.") from exc
    return {"jobId": job.id, "status": "QUEUED"}


@app.get("/api/tunnel")
def tunnel_status():
    return {"public_url": tunnel_manager.public_url_for(TUNNEL_DEPLOYMENT_ID), "local_url": local_api_url()}


@app.post("/api/tunnel")
def start_tunnel():
    try:
        public_url = start_pinggy_tunnel()
    except TunnelError as exc:
        raise HTTPException(502, f"Pinggy tunnel could not be started: {exc}") from exc
    return {"public_url": public_url, "local_url": local_api_url()}


@app.delete("/api/tunnel", status_code=204)
def stop_tunnel():
    tunnel_manager.stop_for(TUNNEL_DEPLOYMENT_ID)


@app.get("/api/servers")
def list_servers(db: Session = Depends(get_db)):
    return [serialize(server) for server in db.query(Server).order_by(Server.created_at.desc()).all()]


@app.post("/api/servers", status_code=201)
def register_server(payload: ServerCreate, db: Session = Depends(get_db)):
    base_url = public_api_base_url()
    public_key, private_key = generate_ssh_keypair()
    token, digest = new_bootstrap_token()
    server = Server(id=str(uuid4()), label=payload.label.strip(), host=payload.host.strip(), ssh_port=payload.ssh_port, public_key=public_key, encrypted_private_key=LocalVault().encrypt(private_key), token_digest=digest, token_expires_at=datetime.utcnow() + timedelta(minutes=30))
    db.add(server)
    db.commit()
    command = f"curl -fsSL {base_url}/bootstrap.sh | sudo env API_BASE={base_url} SERVER_ID={server.id} BOOTSTRAP_TOKEN={token} bash"
    return {**serialize(server), "public_key": public_key, "bootstrap_command": command, "token_expires_at": server.token_expires_at.isoformat(), "public_url": base_url, "local_url": local_api_url()}


def bootstrap_script_content() -> str:
    return """#!/usr/bin/env bash
set -euo pipefail
: \"${API_BASE:?API_BASE is required}\"; : \"${SERVER_ID:?SERVER_ID is required}\"; : \"${BOOTSTRAP_TOKEN:?BOOTSTRAP_TOKEN is required}\"
if ! command -v docker >/dev/null 2>&1 && command -v apt-get >/dev/null 2>&1; then apt-get update -y && apt-get install -y docker.io; fi
os=$( (. /etc/os-release 2>/dev/null && echo \"${PRETTY_NAME:-unknown}\") || uname -s )
kernel=$(uname -r); ram_mb=$(awk '/MemTotal/ {print int($2/1024)}' /proc/meminfo)
disk_gb=$(df -BG / | awk 'NR==2 {gsub(/G/, \"\", $2); print $2}')
docker_version=$(docker --version 2>/dev/null || echo not-installed)
sshd=$(systemctl is-active ssh 2>/dev/null || systemctl is-active sshd 2>/dev/null || echo unknown)
curl --fail --silent --show-error -X POST \"$API_BASE/api/servers/$SERVER_ID/handshake\" -H \"Authorization: Bearer $BOOTSTRAP_TOKEN\" -H 'Content-Type: application/json' --data \"{\\\"os\\\":\\\"$os\\\",\\\"kernel\\\":\\\"$kernel\\\",\\\"ram_mb\\\":$ram_mb,\\\"disk_gb\\\":$disk_gb,\\\"docker_version\\\":\\\"$docker_version\\\",\\\"sshd\\\":\\\"$sshd\\\"}\"
echo 'Onboarding handshake completed.'
"""


@app.get("/bootstrap.sh", response_class=PlainTextResponse)
def bootstrap_script():
    return PlainTextResponse(bootstrap_script_content(), media_type="text/plain")


@app.post("/api/servers/{server_id}/handshake")
def handshake(server_id: str, facts: FactReport, authorization: str | None = Header(default=None), db: Session = Depends(get_db)):
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "Bootstrap token required")
    server = db.get(Server, server_id)
    if not server or server.status != "AWAITING_BOOTSTRAP":
        raise HTTPException(404, "Server is unavailable for bootstrap")
    if server.token_expires_at < datetime.utcnow() or token_digest(authorization[7:]) != server.token_digest:
        raise HTTPException(401, "Invalid or expired bootstrap token")
    server.facts = facts.model_dump()
    server.status = "READY"
    server.bootstrapped_at = datetime.utcnow()
    db.commit()
    return {"status": server.status, "server_id": server.id}

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=settings.api_port)
