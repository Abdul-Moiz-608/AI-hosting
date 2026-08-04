from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import Request, urlopen

try:
    from .config import get_settings
except ImportError:
    from config import get_settings

logger = logging.getLogger(__name__)
_REPOSITORY = re.compile(r"^(?P<owner>[A-Za-z0-9][A-Za-z0-9_.-]{0,38})/(?P<repo>[A-Za-z0-9_.-]{1,100})$")


class RepositoryUrlError(ValueError):
    pass


class RepositoryNotFound(LookupError):
    pass


class GitHubRateLimitError(RuntimeError):
    pass


class GitHubFetchError(RuntimeError):
    pass


class GitHubTimeoutError(GitHubFetchError):
    pass


@dataclass(frozen=True)
class RepositoryRef:
    owner: str
    repo: str
    normalized_url: str


def _parts_from_url(value: str) -> tuple[str, str] | None:
    parsed = urlsplit(value)
    if parsed.scheme == "https" and parsed.hostname == "github.com" and not parsed.username:
        if parsed.port is not None or parsed.query or parsed.fragment:
            return None
        path = parsed.path.strip("/")
    elif parsed.scheme == "ssh" and parsed.hostname == "github.com" and parsed.username == "git":
        if parsed.port not in (None, 22) or parsed.query or parsed.fragment:
            return None
        path = parsed.path.strip("/")
    else:
        return None
    if path.endswith(".git"):
        path = path[:-4]
    match = _REPOSITORY.fullmatch(path)
    return (match.group("owner"), match.group("repo")) if match else None


def parse_repository_url(value: str) -> RepositoryRef:
    raw = value.strip()
    # urlsplit parses git@github.com:owner/repo as a path, so handle the only
    # supported SCP-style SSH form explicitly and keep all other hosts rejected.
    if raw.lower().startswith("git@github.com:"):
        parts = ("git", raw.split(":", 1)[1])
        path = parts[1].strip("/")
        if path.endswith(".git"):
            path = path[:-4]
        match = _REPOSITORY.fullmatch(path)
        parsed_parts = (match.group("owner"), match.group("repo")) if match else None
    else:
        parsed_parts = _parts_from_url(raw)
    if not parsed_parts:
        raise RepositoryUrlError("Enter a GitHub HTTPS or SSH repository URL, such as https://github.com/owner/repo.")
    owner, repo = parsed_parts
    return RepositoryRef(owner, repo, f"https://github.com/{owner}/{repo}")


def fetch_metadata(repository: RepositoryRef) -> dict:
    settings = get_settings()
    api_url = f"https://api.github.com/repos/{quote(repository.owner)}/{quote(repository.repo)}"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "AI-Hosting-Platform"}
    if settings.github_token.strip():
        headers["Authorization"] = f"Bearer {settings.github_token.strip()}"
    try:
        with urlopen(Request(api_url, headers=headers), timeout=5) as response:
            data = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code == 404:
            raise RepositoryNotFound("Repository was not found or is private.") from exc
        if exc.code == 403 and (exc.headers.get("X-RateLimit-Remaining") == "0" or "rate limit" in str(exc).lower()):
            raise GitHubRateLimitError("GitHub API rate limit reached. Try again later or configure GITHUB_TOKEN.") from exc
        logger.exception("GitHub metadata request failed with HTTP %s", exc.code)
        raise GitHubFetchError("GitHub could not return repository metadata.") from exc
    except TimeoutError as exc:
        logger.warning("GitHub metadata request timed out")
        raise GitHubTimeoutError("GitHub took too long to respond. Please try again.") from exc
    except (URLError, OSError, json.JSONDecodeError) as exc:
        logger.exception("GitHub metadata request failed")
        raise GitHubFetchError("GitHub could not be reached. Please try again.") from exc
    return {
        "owner": repository.owner,
        "repo": repository.repo,
        "language": data.get("language") or "Unknown",
        "stars": int(data.get("stargazers_count") or 0),
        "last_commit": data.get("pushed_at") or data.get("updated_at"),
        "visibility": data.get("visibility") or ("private" if data.get("private") else "public"),
        "archived": bool(data.get("archived")),
        "empty_repo": not bool(data.get("size")),
        "url": repository.normalized_url,
    }
