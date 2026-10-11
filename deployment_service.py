"""Network and Git operations for the Streamlit deployment wizard."""
from __future__ import annotations

import base64
import os
import subprocess
import time
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import quote

import requests

GITHUB_API = "https://api.github.com"
VERCEL_API = "https://api.vercel.com"
GROQ_API = "https://api.groq.com/openai/v1/chat/completions"


@dataclass(frozen=True)
class Credentials:
    github_username: str
    github_token: str
    vercel_username: str
    vercel_token: str
    vercel_team_id: str = ""
    groq_api_key: str = ""
    groq_model: str = "openai/gpt-oss-20b"


class DeploymentError(RuntimeError):
    """An error that can be shown safely in the UI."""


class RepositoryNameTaken(DeploymentError):
    """GitHub rejected the requested repository name."""


class VercelBuildFailure(DeploymentError):
    """A Vercel build failed, with enough detail for safe remediation."""

    def __init__(self, deployment: dict):
        self.deployment = deployment
        self.error_code = deployment.get("errorCode", "")
        self.error_message = deployment.get("errorMessage", "")
        super().__init__(self.error_message or f"Vercel deployment ended with status: {deployment.get('readyState')}.")


def find_vercel_incompatibility(folder_text: str) -> tuple[str, str] | None:
    """Identify project types that Vercel cannot run directly, without executing code.

    The check is deliberately conservative: it only rejects known persistent-server
    and container-only deployment models. Other projects are allowed through so
    Vercel can validate their real build configuration.
    """
    folder = Path(folder_text).expanduser().resolve()
    if (folder / "Dockerfile").is_file() or (folder / "docker-compose.yml").is_file() or (folder / "compose.yml").is_file():
        return (
            "Docker/container project",
            "Vercel does not deploy Dockerfiles or Docker Compose applications directly. Use a container host such as Render, Railway, Fly.io, or a cloud container service.",
        )

    candidates = [folder / "app.py", folder / "requirements.txt", folder / "pyproject.toml", folder / "Pipfile"]
    for file in candidates:
        if not file.is_file():
            continue
        contents = file.read_text(encoding="utf-8", errors="ignore").lower()
        if "streamlit" in contents:
            return (
                "Streamlit app",
                "Streamlit requires a persistent server, while Vercel expects serverless request handlers. Use Streamlit Community Cloud, Render, or Railway.",
            )
        if "gradio" in contents:
            return (
                "Gradio app",
                "Gradio normally runs as a persistent Python web server and cannot be deployed directly to Vercel. Use Hugging Face Spaces, Render, or Railway.",
            )
    return None


def github_credentials(credentials: Credentials) -> tuple[str, str]:
    username = credentials.github_username.strip()
    token = credentials.github_token.strip()
    if not username or not token:
        raise DeploymentError("Set GITHUB_USERNAME and GITHUB_PASSWORD in .env. GITHUB_PASSWORD must be a Personal Access Token.")
    return username, token


def vercel_credentials(credentials: Credentials) -> tuple[str, str]:
    username = credentials.vercel_username.strip()
    token = credentials.vercel_token.strip()
    if not token:
        raise DeploymentError("Set VERCEL_PASSWORD to a Vercel token in .env.")
    return username, token


def create_repository(name: str, credentials: Credentials, private: bool) -> dict:
    username, token = github_credentials(credentials)
    response = requests.post(
        f"{GITHUB_API}/user/repos",
        auth=(username, token),
        headers={"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"},
        json={"name": name, "private": private, "auto_init": False},
        timeout=30,
    )
    if response.status_code == 422:
        raise RepositoryNameTaken("GitHub refused that repository name. Choose another name and try again.")
    if not response.ok:
        raise DeploymentError(f"GitHub could not create the repository ({response.status_code}): {response.text[:240]}")
    return response.json()


def _run_git(folder: Path, *args: str, env: dict | None = None) -> str:
    process = subprocess.run(
        ["git", "-c", f"safe.directory={folder.as_posix()}", *args],
        cwd=folder,
        env=env,
        text=True,
        capture_output=True,
    )
    if process.returncode:
        message = (process.stderr or process.stdout).strip()
        raise DeploymentError(f"Git {' '.join(args[:2])} failed: {message[:500]}")
    return process.stdout.strip()


def upload_project(folder_text: str, repository: dict, credentials: Credentials) -> None:
    folder = Path(folder_text).expanduser().resolve()
    if not folder.is_dir():
        raise DeploymentError("The project folder does not exist or is not a directory.")
    if not any(path.name != ".git" for path in folder.iterdir()):
        raise DeploymentError("The project folder is empty. Add your project files before uploading.")

    username, token = github_credentials(credentials)
    if not (folder / ".git").exists():
        _run_git(folder, "init", "-b", "main")
    branch = _run_git(folder, "branch", "--show-current")
    if branch and branch != "main":
        raise DeploymentError(f"This project is on '{branch}'. Switch it to main before deploying.")
    if not branch:
        _run_git(folder, "checkout", "-b", "main")

    _ensure_deployment_gitignore(folder)
    _run_git(folder, "add", "-A")
    if _run_git(folder, "status", "--porcelain"):
        try:
            _run_git(folder, "commit", "-m", "Deploy production release")
        except DeploymentError as error:
            if "Author identity unknown" in str(error):
                raise DeploymentError("Configure Git user.name and user.email on this machine, then retry.") from error
            raise

    clean_remote = repository["clone_url"]
    try:
        _run_git(folder, "remote", "set-url", "origin", clean_remote)
    except DeploymentError:
        _run_git(folder, "remote", "add", "origin", clean_remote)

    # Authentication is used only for this push; the saved Git remote stays token-free.
    encoded = base64.b64encode(f"{username}:{token}".encode()).decode()
    git_env = os.environ.copy()
    git_env["GIT_CONFIG_COUNT"] = "1"
    git_env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
    git_env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {encoded}"
    _run_git(folder, "push", "-u", "origin", "main", env=git_env)


def _ensure_deployment_gitignore(folder: Path) -> bool:
    """Ignore generated dependency/build files before their first Git upload."""
    file = folder / ".gitignore"
    existing = file.read_text(encoding="utf-8", errors="ignore") if file.exists() else ""
    additions = [entry for entry in ("node_modules/", "dist/", ".env") if entry not in existing.splitlines()]
    if not additions:
        return False
    separator = "" if not existing or existing.endswith("\n") else "\n"
    file.write_text(f"{existing}{separator}{'\n'.join(additions)}\n", encoding="utf-8")
    return True


def _git_push_existing_main(folder: Path, credentials: Credentials) -> None:
    """Push a repair using the already-configured, token-free origin."""
    username, token = github_credentials(credentials)
    encoded = base64.b64encode(f"{username}:{token}".encode()).decode()
    git_env = os.environ.copy()
    git_env["GIT_CONFIG_COUNT"] = "1"
    git_env["GIT_CONFIG_KEY_0"] = "http.https://github.com/.extraheader"
    git_env["GIT_CONFIG_VALUE_0"] = f"AUTHORIZATION: basic {encoded}"
    _run_git(folder, "push", "origin", "main", env=git_env)


def _vercel_params(credentials: Credentials) -> dict:
    team_id = credentials.vercel_team_id.strip()
    return {"teamId": team_id} if team_id else {}


def create_vercel_project(repository: dict, credentials: Credentials) -> dict:
    """Create a Vercel project linked to GitHub before the first push to main."""
    _, token = vercel_credentials(credentials)
    owner, repo_name = repository["full_name"].split("/", 1)
    response = requests.post(
        f"{VERCEL_API}/v10/projects",
        headers={"Authorization": f"Bearer {token}"},
        params=_vercel_params(credentials),
        json={
            "name": repo_name.lower(),
            "gitRepository": {"type": "github", "repo": repository["full_name"]},
        },
        timeout=30,
    )
    if not response.ok:
        detail = response.text[:360]
        raise DeploymentError(
            f"Vercel could not create the linked project ({response.status_code}): {detail}. "
            "Connect GitHub to Vercel in Account Settings → Git Integrations, then retry."
        )
    return response.json()


def wait_for_vercel_deployment(project: dict, credentials: Credentials, max_seconds: int = 180, after_uid: str | None = None) -> str:
    """Find the production deployment that Vercel starts from the main-branch push."""
    _, token = vercel_credentials(credentials)
    project_id = project["id"]
    headers = {"Authorization": f"Bearer {token}"}
    deadline = time.monotonic() + max_seconds
    while time.monotonic() < deadline:
        params = {**_vercel_params(credentials), "projectId": project_id, "target": "production", "limit": 1}
        response = requests.get(f"{VERCEL_API}/v6/deployments", headers=headers, params=params, timeout=30)
        if not response.ok:
            raise DeploymentError(f"Could not read Vercel deployment status ({response.status_code}).")
        deployments = response.json().get("deployments", [])
        if not deployments:
            time.sleep(3)
            continue
        deployment = deployments[0]
        if after_uid and (deployment.get("uid") or deployment.get("id")) == after_uid:
            time.sleep(3)
            continue
        state = deployment.get("readyState")
        if state == "READY":
            url = deployment.get("url")
            return f"https://{url}" if url and not url.startswith("http") else url
        if state in {"ERROR", "CANCELED"}:
            if deployment.get("errorCode") == "PYTHON_ENTRYPOINT_NOT_FOUND":
                raise DeploymentError(
                    "Vercel cannot deploy this Python app in its current form. It needs a serverless "
                    "entry point exporting app, application, or handler. Persistent-server frameworks "
                    "such as Streamlit and Gradio cannot be deployed directly to Vercel."
                )
            raise VercelBuildFailure(deployment)
        time.sleep(3)
    raise DeploymentError("Vercel is still building. Check its dashboard for the final deployment URL.")


def _deployment_log_text(deployment: dict, credentials: Credentials) -> str:
    """Fetch build-event text only after Vercel reports a failed deployment."""
    _, token = vercel_credentials(credentials)
    uid = deployment.get("uid") or deployment.get("id")
    if not uid:
        return ""
    response = requests.get(
        f"{VERCEL_API}/v2/deployments/{uid}/events",
        headers={"Authorization": f"Bearer {token}"},
        params=_vercel_params(credentials),
        timeout=30,
    )
    if not response.ok:
        return ""
    return "\n".join(event.get("payload", {}).get("text", "") for event in response.json())


def _redact_sensitive_text(text: str, credentials: Credentials) -> str:
    """Remove local credential values and common token shapes before an LLM call."""
    for value in (credentials.github_token, credentials.vercel_token, credentials.groq_api_key):
        if value:
            text = text.replace(value, "[REDACTED]")
    text = re.sub(r"\bgh[ps]_[A-Za-z0-9_]+\b", "[REDACTED_GITHUB_TOKEN]", text)
    text = re.sub(r"\bgsk_[A-Za-z0-9_-]+\b", "[REDACTED_GROQ_KEY]", text)
    text = re.sub(r"(?i)(api[_-]?key|token|password|secret)\s*[:=]\s*[^\s'\"]+", r"\1=[REDACTED]", text)
    return text


def _project_context(folder_text: str, credentials: Credentials) -> str:
    """Collect only deployment-relevant manifests, never the full source tree."""
    folder = Path(folder_text).expanduser().resolve()
    parts = []
    for name in ("package.json", "package-lock.json", "requirements.txt", "pyproject.toml", "vercel.json", ".gitignore"):
        file = folder / name
        if file.is_file():
            contents = file.read_text(encoding="utf-8", errors="ignore")[:6000]
            parts.append(f"--- {name} ---\n{contents}")
    return _redact_sensitive_text("\n\n".join(parts), credentials)[:18000]


def analyze_with_groq(folder_text: str, failure: VercelBuildFailure, credentials: Credentials) -> str | None:
    """Ask Groq for a diagnosis of an unfamiliar failure; it never makes edits."""
    api_key = credentials.groq_api_key.strip() or os.getenv("GROQ_API_KEY", "").strip()
    if not api_key:
        return None
    logs = _redact_sensitive_text(_deployment_log_text(failure.deployment, credentials), credentials)[-12000:]
    prompt = f"""You are a deployment troubleshooter. Diagnose a Vercel production build failure.
Only use the supplied, redacted build log and manifest files. Never request secrets.
Give a concise answer with: likely cause, exact safe fix steps, and whether this project is incompatible with Vercel.
Do not claim a fix is verified unless the log proves it.

Vercel error code: {failure.error_code}
Vercel message: {failure.error_message}

Build log:
{logs}

Project manifests:
{_project_context(folder_text, credentials)}
"""
    response = requests.post(
        GROQ_API,
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json={
            "model": credentials.groq_model or os.getenv("GROQ_MODEL", "openai/gpt-oss-20b"),
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0.1,
            "max_tokens": 700,
        },
        timeout=45,
    )
    if not response.ok:
        return f"AI diagnosis was unavailable ({response.status_code})."
    try:
        return response.json()["choices"][0]["message"]["content"].strip()
    except (KeyError, IndexError, TypeError, ValueError):
        return "AI diagnosis returned an unexpected response."


def repair_known_vercel_failure(folder_text: str, failure: VercelBuildFailure, credentials: Credentials) -> str | None:
    """Repair only a narrowly-known safe error, then push main to trigger Vercel.

    Returns a user-visible repair summary, or None when human project changes are
    needed. It never edits dependencies, source code, secrets, or build commands.
    """
    folder = Path(folder_text).expanduser().resolve()
    logs = _deployment_log_text(failure.deployment, credentials).lower()
    is_node_modules_permission_error = (
        failure.error_code == "EACCESS"
        and "node_modules/.bin" in logs
        and "permission denied" in logs
    )
    if not is_node_modules_permission_error:
        return None

    _ensure_deployment_gitignore(folder)
    tracked_files = _run_git(folder, "ls-files", "node_modules")
    if tracked_files:
        _run_git(folder, "rm", "-r", "--cached", "node_modules")
    _run_git(folder, "add", ".gitignore")
    if not _run_git(folder, "status", "--porcelain"):
        return None
    _run_git(folder, "commit", "-m", "fix: exclude generated Node dependencies from deployment")
    _git_push_existing_main(folder, credentials)
    return "Removed tracked node_modules, added deployment ignores, and pushed the repair to main."