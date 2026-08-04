"""Read-only repository analysis used by the deployment worker.

This module never imports, installs, or executes anything from a checked-out
repository.  It only reads bounded text files and static manifests.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

try:
    import tomllib
except ModuleNotFoundError:  # pragma: no cover - Python 3.10 fallback
    tomllib = None

from pydantic import BaseModel, ConfigDict, Field, ValidationError

try:
    from .config import get_settings
except ImportError:
    from config import get_settings

logger = logging.getLogger(__name__)

MAX_FILE_BYTES = 256 * 1024
MAX_TOTAL_BYTES = 2 * 1024 * 1024
MAX_DEPTH = 5
MAX_TREE_ENTRIES = 600
MAX_README_BYTES = 12 * 1024
CONFIDENCE_THRESHOLD = 0.78
IGNORED_DIRS = {".git", "node_modules", "vendor", "dist", "build", ".next", ".venv", "venv", "target"}


class Detection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime: str = "unknown"
    framework: str = "unknown"
    language: str = "unknown"
    package_manager: str = "unknown"
    build_cmd: str | None = None
    start_cmd: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    services: list[str] = Field(default_factory=list)
    env_vars_required: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class RepositoryAnalysisError(RuntimeError):
    pass


class AnalysisTimeout(RepositoryAnalysisError):
    pass


def _read_file(path: Path, remaining: int) -> str:
    if remaining <= 0 or not path.is_file():
        return ""
    try:
        if path.stat().st_size > min(MAX_FILE_BYTES, remaining):
            return ""
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, UnicodeError):
        return ""


def _repository_files(root: Path) -> list[Path]:
    files: list[Path] = []
    for current, dirs, names in os.walk(root):
        current_path = Path(current)
        depth = len(current_path.relative_to(root).parts)
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not d.startswith("."))
        if depth >= MAX_DEPTH:
            dirs[:] = []
        for name in sorted(names):
            path = current_path / name
            if len(files) >= MAX_TREE_ENTRIES:
                return files
            if path.is_symlink():
                continue
            files.append(path)
    return files


def _load_manifests(root: Path) -> tuple[dict[str, str], str, list[str]]:
    files = _repository_files(root)
    total = 0
    manifests: dict[str, str] = {}
    selected_names = {
        "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "pyproject.toml",
        "requirements.txt", "Pipfile", "poetry.lock", "composer.json", "Cargo.toml", "go.mod",
        "Gemfile", "pom.xml", "build.gradle", "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
        "Procfile", "README", "README.md", "README.rst", "README.txt",
    }
    for path in files:
        if path.name not in selected_names and not path.name.endswith(".csproj"):
            continue
        content = _read_file(path, min(MAX_FILE_BYTES, MAX_TOTAL_BYTES - total))
        if not content:
            continue
        total += len(content.encode("utf-8", errors="ignore"))
        relative = path.relative_to(root).as_posix()
        if path.name.lower().startswith("readme"):
            manifests.setdefault("README", content[:MAX_README_BYTES])
        else:
            manifests[relative] = content
        if total >= MAX_TOTAL_BYTES:
            break
    tree = "\n".join(p.relative_to(root).as_posix() for p in files)
    return manifests, tree[:40_000], files


def _deps(package: dict[str, Any]) -> set[str]:
    result: set[str] = set()
    for key in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
        values = package.get(key, {})
        if isinstance(values, dict):
            result.update(str(k).lower() for k in values)
    return result


def _python_dependencies(manifests: dict[str, str]) -> set[str]:
    deps: set[str] = set()
    text = "\n".join(v for k, v in manifests.items() if k.endswith(("requirements.txt", "Pipfile", "poetry.lock")))
    deps.update(re.findall(r"(?im)^\s*([a-zA-Z][\w-]+)", text))
    pyproject = manifests.get("pyproject.toml", "")
    if pyproject and tomllib:
        try:
            data = tomllib.loads(pyproject)
            project = data.get("project", {})
            deps.update(str(x).split("[", 1)[0].split("=", 1)[0].lower() for x in project.get("dependencies", []))
            for values in data.get("tool", {}).get("poetry", {}).get("dependencies", {}).keys():
                deps.add(str(values).lower())
        except (tomllib.TOMLDecodeError, AttributeError):
            pass
    return {x.lower().replace("_", "-") for x in deps}


def _port_from_text(text: str) -> int | None:
    match = re.search(r"(?im)(?:PORT|EXPOSE|--port\s+|port\s*[:=])\s*[\"']?(\d{2,5})", text)
    return int(match.group(1)) if match and 1 <= int(match.group(1)) <= 65535 else None


def _required_env(text: str) -> list[str]:
    names = set(re.findall(r"\bprocess\.env\.([A-Z][A-Z0-9_]*)", text))
    names.update(re.findall(r"\bos\.getenv\(\s*[\"']([A-Z][A-Z0-9_]*)", text))
    names.update(re.findall(r"\b(?:os\.environ|ENV)\[\s*[\"']([A-Z][A-Z0-9_]*)", text))
    names.update(re.findall(r"\$\{([A-Z][A-Z0-9_]*)\}", text))
    return sorted(names)


def _services(text: str) -> list[str]:
    found = []
    for service in ("postgres", "redis", "mysql", "mongodb"):
        if re.search(rf"\b(?:{service}|postgresql|mongo)\b", text, re.IGNORECASE):
            found.append(service)
    return found


def analyze_rules(root: Path) -> tuple[Detection, dict[str, Any]]:
    manifests, tree, files = _load_manifests(root)
    source_parts: list[str] = []
    source_bytes = 0
    source_suffixes = {".js", ".jsx", ".ts", ".tsx", ".py", ".php", ".rb", ".java", ".go", ".rs", ".cs", ".yml", ".yaml"}
    for path in files:
        if path.suffix.lower() not in source_suffixes or path.name in {"package.json", "pyproject.toml"}:
            continue
        excerpt = _read_file(path, min(MAX_FILE_BYTES, 512 * 1024 - source_bytes))
        if excerpt:
            source_parts.append(excerpt)
            source_bytes += len(excerpt.encode("utf-8", errors="ignore"))
        if source_bytes >= 512 * 1024:
            break
    all_text = "\n".join(manifests.values()) + "\n" + "\n".join(source_parts)
    package: dict[str, Any] = {}
    package_raw = manifests.get("package.json", "")
    if package_raw:
        try:
            package = json.loads(package_raw)
        except json.JSONDecodeError:
            logger.warning("Invalid package.json; continuing with other manifests")
    deps = _deps(package)
    pydeps = _python_dependencies(manifests)
    scripts = package.get("scripts", {}) if isinstance(package.get("scripts"), dict) else {}
    framework = "unknown"
    language = "unknown"
    runtime = "unknown"
    package_manager = "unknown"
    build_cmd: str | None = None
    start_cmd: str | None = None
    port: int | None = None

    if "next" in deps:
        framework, language, runtime, port = "Next.js", "TypeScript" if "typescript" in deps else "JavaScript", "Node.js", 3000
    elif "@angular/core" in deps:
        framework, language, runtime, port = "Angular", "TypeScript", "Node.js", 4200
    elif "vue" in deps:
        framework, language, runtime, port = "Vue", "TypeScript" if "typescript" in deps else "JavaScript", "Node.js", 5173
    elif "react" in deps or "react-dom" in deps:
        framework, language, runtime, port = "React", "TypeScript" if "typescript" in deps else "JavaScript", "Node.js", 3000
    elif "@nestjs/core" in deps:
        framework, language, runtime, port = "NestJS", "TypeScript", "Node.js", 3000
    elif "express" in deps:
        framework, language, runtime, port = "Express", "JavaScript", "Node.js", 3000
    elif "fastapi" in pydeps:
        framework, language, runtime, port = "FastAPI", "Python", "Python", 8000
    elif "flask" in pydeps:
        framework, language, runtime, port = "Flask", "Python", "Python", 5000
    elif "django" in pydeps or "manage.py" in [p.name for p in files]:
        framework, language, runtime, port = "Django", "Python", "Python", 8000
    elif "laravel/framework" in deps:
        framework, language, runtime, port = "Laravel", "PHP", "PHP", 8000
    elif "spring-boot" in all_text or "springframework.boot" in all_text:
        framework, language, runtime, port = "Spring Boot", "Java", "Java", 8080
    elif any(p.name.endswith(".csproj") for p in files) or "microsoft.net.sdk" in all_text.lower():
        framework, language, runtime, port = "ASP.NET", "C#", ".NET", 5000
    elif "Cargo.toml" in manifests:
        framework, language, runtime, port = "unknown", "Rust", "Rust", None
    elif "go.mod" in manifests:
        framework, language, runtime, port = "unknown", "Go", "Go", 8080
    elif "Gemfile" in manifests:
        framework, language, runtime, port = "unknown", "Ruby", "Ruby", 3000
    elif "pom.xml" in manifests or "build.gradle" in manifests:
        framework, language, runtime, port = "unknown", "Java", "Java", 8080
    elif "index.html" in [p.name for p in files]:
        framework, language, runtime, port = "Static HTML", "HTML", "static", 8080

    if "pnpm-lock.yaml" in manifests:
        package_manager = "pnpm"
    elif "yarn.lock" in manifests:
        package_manager = "yarn"
    elif "package-lock.json" in manifests:
        package_manager = "npm"
    elif "poetry.lock" in manifests:
        package_manager = "poetry"
    elif "Pipfile" in manifests:
        package_manager = "pipenv"
    elif "requirements.txt" in manifests:
        package_manager = "pip"
    elif "composer.json" in manifests:
        package_manager = "composer"
    elif "Cargo.toml" in manifests:
        package_manager = "cargo"
    elif "go.mod" in manifests:
        package_manager = "go"
    elif "Gemfile" in manifests:
        package_manager = "bundler"

    build_cmd = str(scripts.get("build")) if scripts.get("build") else None
    start_cmd = str(scripts.get("start")) if scripts.get("start") else None
    if framework == "Next.js" and not start_cmd:
        start_cmd = "npm run start"
    if framework == "FastAPI" and not start_cmd:
        start_cmd = "uvicorn app.main:app --host 0.0.0.0"
    if framework == "Django" and not start_cmd:
        start_cmd = "python manage.py runserver 0.0.0.0:8000"
    dockerfile = manifests.get("Dockerfile", "")
    port = _port_from_text(dockerfile) or _port_from_text(all_text) or port
    env_vars = _required_env(all_text)
    services = _services(all_text)
    manifest_count = sum(1 for name in manifests if name != "README")
    monorepo = len([p for p in files if p.name == "package.json"]) > 1 or len({p.parent for p in files if p.name in {"package.json", "pyproject.toml", "pom.xml"}}) > 1
    conflicts = bool(package and pydeps)
    confidence = 0.92 if framework != "unknown" else (0.62 if manifest_count else 0.3)
    if monorepo or conflicts:
        confidence = min(confidence, 0.65)
    detection = Detection(runtime=runtime, framework=framework, language=language, package_manager=package_manager, build_cmd=build_cmd, start_cmd=start_cmd, port=port, services=services, env_vars_required=env_vars, confidence=confidence)
    context = {"tree": tree, "manifests": manifests, "readme": manifests.get("README", ""), "monorepo": monorepo, "conflicts": conflicts, "needs_llm": confidence < CONFIDENCE_THRESHOLD or monorepo or conflicts or manifest_count == 0 or framework == "unknown"}
    return detection, context


def _llm_detection(context: dict[str, Any], fallback: Detection) -> Detection:
    settings = get_settings()
    if not settings.groq_api_key.strip():
        logger.warning("GROQ_API_KEY is not configured; using rule-engine result")
        return fallback
    try:
        from groq import Groq
        client = Groq(api_key=settings.groq_api_key.strip(), timeout=settings.analysis_timeout_seconds)
        prompt = "Return STRICT JSON ONLY with exactly these fields: runtime, framework, language, package_manager, build_cmd, start_cmd, port, services, env_vars_required, confidence. Do not execute or infer secrets.\nRepository tree:\n" + context["tree"] + "\nManifest files:\n" + json.dumps(context["manifests"], ensure_ascii=False) + "\nREADME:\n" + context["readme"]
        for attempt in range(2):
            try:
                response = client.chat.completions.create(model=settings.groq_model, messages=[{"role": "system", "content": "You are a repository manifest classifier."}, {"role": "user", "content": prompt}], temperature=0.1, max_tokens=800, response_format={"type": "json_object"})
                return Detection.model_validate(json.loads(response.choices[0].message.content or "{}"))
            except (json.JSONDecodeError, ValidationError, TypeError, AttributeError) as exc:
                logger.warning("Invalid Groq analysis JSON on attempt %s: %s", attempt + 1, exc)
        return fallback
    except Exception:
        logger.exception("Groq repository analysis failed; retaining rule-engine result")
        return fallback


def analyze_repository(root: Path) -> Detection:
    rules, context = analyze_rules(root)
    result = _llm_detection(context, rules) if context["needs_llm"] else rules
    return Detection.model_validate(result.model_dump())


def shallow_clone(repository_url: str, destination: Path, timeout_seconds: int) -> str:
    if not shutil.which("git"):
        raise RepositoryAnalysisError("Git is not installed on the worker.")
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    try:
        subprocess.run(["git", "clone", "--depth", "1", "--single-branch", "--no-tags", repository_url, str(destination)], check=True, capture_output=True, text=True, timeout=timeout_seconds, env=env)
        result = subprocess.run(["git", "-C", str(destination), "rev-parse", "HEAD"], check=True, capture_output=True, text=True, timeout=10, env=env)
    except subprocess.TimeoutExpired as exc:
        raise AnalysisTimeout("Repository analysis timed out while cloning.") from exc
    except (OSError, subprocess.CalledProcessError) as exc:
        raise RepositoryAnalysisError("The repository could not be cloned for analysis.") from exc
    sha = result.stdout.strip()
    if not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise RepositoryAnalysisError("Git returned an invalid commit SHA.")
    return sha


def analyze_cloned_repository(repository_url: str) -> tuple[str, Detection]:
    settings = get_settings()
    with tempfile.TemporaryDirectory(prefix="repo-analysis-") as temp:
        root = Path(temp) / "repository"
        sha = shallow_clone(repository_url, root, settings.analysis_timeout_seconds)
        return sha, analyze_repository(root)
