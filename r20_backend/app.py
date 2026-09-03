"""LEGACY control plane (Stage 3 deprecated).

Prefer ``uvicorn keel.api.app:app`` as the primary API entry.
This module remains for admin routes and the legacy read-only dashboard UI mount.
It does NOT spawn a second scheduler.
"""
from __future__ import annotations
import copy
import hashlib
import hmac
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
SCRIPTS_DIR = ROOT / "scripts"
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPTS_DIR))

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from contextlib import asynccontextmanager
from contextvars import ContextVar
from pydantic import BaseModel, Field
from r20_backend.config import refresh_settings, settings
from r20_backend.okx_client import OKXClient
from r20_backend.okx_trade_service import account_snapshot as okx_account_snapshot, fast_close_confirmed
from r20_backend.okx_setup import diagnose_okx_runtime, install_okx_cli, check_node_npm, start_oauth_device_login, oauth_status
from r20_backend.account_baseline import load_account_baseline, update_initial_capital
from r20_backend.backup_secrets import credential_status as backup_credential_status, save_credentials as save_backup_credentials
from r20_backend.prompt_views import EVOLUTION_USER_TEMPLATE, TRADING_USER_TEMPLATE, rendered_snapshots
from r20_backend.settings_store import mask, remove_env, update_env
from r20_backend.notifications import _env as notification_env, diagnose_channel, test_channel
from r20_backend.audit import recent as recent_audit, record as audit_record
from r20_backend.admin_auth import AdminAuthStore
from r20_backend.backup_store import (
    create_job as create_backup_job, delete_job as delete_backup_job, export_job as export_backup_job,
    get_job as get_backup_job, import_job as import_backup_job, list_jobs as list_backup_jobs,
    load_backup_methods, save_backup_methods, update_job as update_backup_job, validate_backup_job,
)
from r20_backend.schedule_store import load_schedule, save_schedule
from r20_gateway.agents import agent_statuses
from r20_gateway.publisher import DB_PATH as GATEWAY_DB_PATH
from r20_gateway.plugins import plugin_statuses
from r20_gateway import __version__ as GATEWAY_VERSION
from r20_gateway.scheduler import scheduler_snapshot
from r20_gateway.secrets import delete_secrets, save_secrets, status as secret_store_status
from r20_gateway.store import GatewayStore
from r20_gateway.supervisor import start_supervisor as start_gateway_supervisor, stop_supervisor as stop_gateway_supervisor
from scripts.instrument_pool import from_okx_instrument, load_instruments, save_instruments
from r20_backend.llm_manager import (
    load_llm_config,
    get_active_llm_runtime,
    activate_provider_model,
    upsert_provider,
    delete_provider,
    upsert_model,
    delete_model,
    test_llm_connection,
    init_llm_providers,
    _atomic_write_json,
    LLM_PROVIDERS_FILE,
)
from scripts.prompt_library import (
    PRESETS, TEMPLATE_KEYS, active_profile, activate_profile, all_profiles, apply_module_layout,
    create_profile, delete_profile, export_profile, get_profile, import_profile,
    load_library, pipeline_view, profile_history, resolve_profile, rollback_profile, save_library, update_profile, validate_profile,
)

PROMPT_OVERRIDE_FILE = DATA_DIR / "system_prompt_override.txt"
BACKUP_LOG_FILE = ROOT / "logs" / "r20_backup_manual.log"
STARTED_AT = time.time()
REQUEST_SESSION: ContextVar[str] = ContextVar("r20_admin_session", default="")

@asynccontextmanager
async def lifespan(_: FastAPI):
    """Control-plane only. Does NOT spawn a second scheduler/gateway worker.

    Stage 2: job scheduling is owned exclusively by `python -m keel.worker`.
    Auto-spawning r20_gateway.worker here previously double-fired the trader.
    Start notification delivery separately via deploy/r20-gateway.service if needed.
    """
    refresh_settings()
    admin_auth.initialize_from_legacy(settings.admin_token or settings.setup_token)
    # DISABLED Stage 2: start_gateway_supervisor()  # would start GatewayScheduler jobs
    try:
        from dashboard.app import start_dashboard_background_worker
        start_dashboard_background_worker()
    except Exception:
        pass
    yield
    try:
        from dashboard.app import stop_dashboard_background_worker
        stop_dashboard_background_worker()
    except Exception:
        pass
    # DISABLED Stage 2: stop_gateway_supervisor()


app = FastAPI(title="Keel Trader LEGACY Control Plane (prefer keel.api.app)", version="6.2.1", lifespan=lifespan)


@app.middleware("http")
async def admin_session_context(request: Request, call_next):
    token = REQUEST_SESSION.set(request.headers.get("X-R20-Session", ""))
    try:
        return await call_next(request)
    finally:
        REQUEST_SESSION.reset(token)


okx = OKXClient()
admin_auth = AdminAuthStore()
ADMIN_HTML = ROOT / "r20_backend" / "admin.html"


class AdminLoginRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=1, max_length=128)


class AdminCreateRequest(BaseModel):
    username: str = Field(min_length=3, max_length=32)
    password: str = Field(min_length=12, max_length=128)
    role: str = Field(default="admin", pattern=r"^(superadmin|admin)$")


class AdminPasswordRequest(BaseModel):
    current_password: str = Field(default="", max_length=128)
    new_password: str = Field(min_length=12, max_length=128)


class AdminEnabledRequest(BaseModel):
    enabled: bool


class AdminUnlockRequest(BaseModel):
    confirmation: str = Field(min_length=12, max_length=100)


class OkxCliInstallRequest(BaseModel):
    confirmation: str = Field(min_length=8, max_length=80)


class OkxOAuthStartRequest(BaseModel):
    site: str = Field(pattern=r"^(global|eea|us|tr)$")


class AdminConfigUpdate(BaseModel):
    okx_environment: str | None = Field(default=None, pattern=r"^(demo|live)$")
    okx_live_api_key: str | None = None
    okx_live_secret_key: str | None = None
    okx_live_passphrase: str | None = None
    okx_demo_api_key: str | None = None
    okx_demo_secret_key: str | None = None
    okx_demo_passphrase: str | None = None
    okx_api_key: str | None = None
    okx_secret_key: str | None = None
    okx_passphrase: str | None = None
    okx_simulated: bool | None = None
    llm_base_url: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_reasoning_effort: str | None = Field(default=None, pattern=r"^(low|medium|high|minimal|none|auto)$")
    notification_webhook: str | None = None
    manual_close_enabled: bool | None = None


class LLMActivateRequest(BaseModel):
    model_id: str
    provider_id: str | None = None
    reasoning_effort: str | None = Field(default=None, pattern=r"^(low|medium|high|minimal|none|auto)$")


class LLMTestRequest(BaseModel):
    model: str
    provider_id: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_format: str = "openai_chat"
    reasoning_effort: str = Field(default="auto", pattern=r"^(low|medium|high|minimal|none|auto)$")
    reasoning_type: str = "auto"


class LLMProviderUpsertRequest(BaseModel):
    id: str | None = None
    name: str
    base_url: str
    api_key: str | None = None
    description: str | None = ""
    models: list[dict[str, Any]] | None = None


class LLMModelUpsertRequest(BaseModel):
    id: str
    name: str | None = None
    provider_name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    api_format: str = "openai_chat"
    reasoning_type: str = "auto"
    default_effort: str = "high"
    reasoning_effort: str | None = None
    description: str | None = ""


class CouncilConfigUpdateRequest(BaseModel):
    enabled: bool
    timeout_seconds: float = Field(default=60.0, ge=10.0, le=300.0)
    roles: dict[str, Any]


class CouncilResetRoleRequest(BaseModel):
    role_id: str = Field(pattern=r"^(alpha|risk|quant|arbitrator)$")


class CouncilTestRequest(BaseModel):
    mock_market_prompt: str | None = None


class InitialCapitalUpdate(BaseModel):
    initial_capital: float = Field(gt=0, le=1_000_000_000)
    confirmation: str = Field(min_length=1, max_length=80)


class GatewayReplayRequest(BaseModel):
    confirmation: str


class InstrumentAddRequest(BaseModel):
    inst_id: str = Field(pattern=r"^[A-Z0-9]{2,15}-USDT-SWAP$")


class InstrumentDeleteRequest(BaseModel):
    confirmation: str


class ManualCloseRequest(BaseModel):
    close_token: str = Field(min_length=20, max_length=200)
    admin_password: str = Field(min_length=1, max_length=128)
    confirmation: str = Field(min_length=8, max_length=200)


class UpdateRequest(BaseModel):
    confirmation: str


class PromptOverrideRequest(BaseModel):
    content: str = Field(max_length=12000)


class PromptLibraryUpdate(BaseModel):
    active_style: str = Field(pattern=r"^(stable|aggressive|custom)$")
    trading_system: str = Field(default="", max_length=12000)
    trading_user: str = Field(default="", max_length=12000)
    evolution_system: str = Field(default="", max_length=12000)
    evolution_user: str = Field(default="", max_length=12000)


class PromptProfileCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=240)
    source_id: str = Field(default="stable", max_length=80)


class PromptProfileUpdateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=60)
    description: str = Field(default="", max_length=240)
    enabled: bool = True
    editor_mode: str = Field(default="modules", pattern=r"^(simple|advanced|modules)$")
    simple_policy: dict[str, Any] = Field(default_factory=dict)
    pipelines: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    trading_system: str = Field(default="", max_length=12000)
    trading_user: str = Field(default="", max_length=12000)
    evolution_system: str = Field(default="", max_length=12000)
    evolution_user: str = Field(default="", max_length=12000)
    note: str = Field(default="后台更新", max_length=240)


class PromptImportRequest(BaseModel):
    payload: dict[str, Any]
    name_override: str = Field(default="", max_length=60)


class PromptRollbackRequest(BaseModel):
    revision_id: str = Field(min_length=1, max_length=80)


class BackupJobCreateRequest(BaseModel):
    name: str = Field(min_length=1, max_length=80)
    source_id: str = Field(default="nightly-default", max_length=80)


class BackupJobUpdateRequest(BaseModel):
    job: dict[str, Any]


class SimpleBackupUpdateRequest(BaseModel):
    enabled: bool = True
    schedule_time: str = Field(default="02:00", pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")
    destination: str = Field(pattern=r"^(local|s3|oss|webdav|baidu_oauth)$")
    retention: int = Field(default=3, ge=1, le=365)
    endpoint: str = Field(default="", max_length=300)
    bucket: str = Field(default="", max_length=120)
    credentials: dict[str, str] = Field(default_factory=dict)


class BackupCredentialUpdateRequest(BaseModel):
    credential_ref: str = Field(min_length=1, max_length=100)
    credentials: dict[str, str]


class BackupJobRunRequest(BaseModel):
    confirmation: str


class BackupJobImportRequest(BaseModel):
    payload: dict[str, Any]
    name_override: str = Field(default="", max_length=80)


class BackupVerifyRequest(BaseModel):
    archive_path: str = Field(min_length=1, max_length=300)
    expected_sha256: str = Field(default="", max_length=64)
    key_env: str = Field(default="", max_length=64)


class ChannelToggleRequest(BaseModel):
    enabled: bool


class BackupMethodsUpdate(BaseModel):
    baidu_enabled: bool
    local_enabled: bool
    local_retention: int = Field(ge=1, le=30)
    sqlite_enabled: bool
    sqlite_retention: int = Field(ge=1, le=90)


class NotificationConfigUpdate(BaseModel):
    webhook_enabled: bool = False
    webhook_url: str = ""
    wechat_enabled: bool = False
    wechat_webhook: str = ""
    telegram_enabled: bool = False
    telegram_bot_token: str | None = None
    telegram_chat_id: str = ""
    telegram_api_base: str | None = None
    qq_enabled: bool = False
    qq_app_id: str = ""
    qq_client_secret: str | None = None
    qq_openid: str = ""


class QQOpenIDCaptureStartRequest(BaseModel):
    app_id: str | None = None
    client_secret: str | None = None
    timeout: int = 60


class NotificationTestRequest(BaseModel):
    channel: str = Field(pattern=r"^(webhook|wechat|telegram|qq)$")
    confirmation: str = ""


class NotificationScheduleUpdate(BaseModel):
    briefing_times: list[str] = Field(min_length=1, max_length=6)


class BackupRequest(BaseModel):
    confirmation: str


def require_admin_token(token: str) -> None:
    expected = settings.admin_token or settings.setup_token
    if not expected:
        raise HTTPException(status_code=503, detail="后台尚未设置 R20_SETUP_TOKEN 或 R20_ADMIN_TOKEN")
    if not hmac.compare_digest(token, expected):
        raise HTTPException(status_code=403, detail="管理员令牌无效")


def current_admin(x_r20_session: str | None = None, x_r20_admin_token: str | None = None) -> dict[str, Any]:
    user = admin_auth.validate_session(x_r20_session or "")
    if user:
        return user
    if x_r20_admin_token and not admin_auth.has_users():
        require_admin_token(x_r20_admin_token)
        return {"id": 0, "username": "legacy-token", "role": "legacy", "enabled": 1}
    raise HTTPException(status_code=401, detail="管理员会话已失效，请重新登录")


def require_admin_header(x_r20_admin_token: str | None = None, x_r20_session: str | None = None) -> dict[str, Any]:
    return current_admin(x_r20_session or REQUEST_SESSION.get(), x_r20_admin_token)


def require_superadmin(x_r20_session: str | None = None) -> dict[str, Any]:
    user = admin_auth.validate_session(x_r20_session or REQUEST_SESSION.get())
    if not user:
        raise HTTPException(status_code=401, detail="管理员会话已失效，请重新登录")
    if user["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="仅超级管理员可以执行此操作")
    return user


def read_json(filename: str, default: Any) -> Any:
    path = DATA_DIR / filename
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return default


def script_state(script_name: str) -> dict[str, Any]:
    path = SCRIPTS_DIR / script_name
    return {"name": script_name, "exists": path.exists(), "path": str(path)}


def file_health(filename: str, expected_interval: int) -> dict[str, Any]:
    path = DATA_DIR / filename
    if not path.exists():
        return {"name": filename, "exists": False, "age_seconds": None, "fresh": False}
    age = max(0, int(time.time() - path.stat().st_mtime))
    return {"name": filename, "exists": True, "age_seconds": age, "fresh": age <= expected_interval * 2, "bytes": path.stat().st_size}


def log_tail(filename: str, lines: int = 30) -> str:
    path = ROOT / "logs" / filename
    if not path.exists():
        return "暂无日志"
    return "\n".join(path.read_text(encoding="utf-8", errors="replace").splitlines()[-max(1, min(lines, 200)):])


def decision_summary() -> list[dict[str, Any]]:
    raw = read_json("ai_brain_decisions.json", {})
    result = []
    for inst_id, item in raw.items():
        decision = item.get("decision", {}) if isinstance(item, dict) else {}
        result.append({
            "instId": inst_id,
            "action": decision.get("action", "WAIT"),
            "confidence": decision.get("confidence", 0),
            "summary": decision.get("summary_reason", ""),
            "updated_at": item.get("time_str", "") if isinstance(item, dict) else "",
        })
    return result


def runtime_overview() -> dict[str, Any]:
    health_files = [
        file_health("ai_brain_decisions.json", 15 * 60),
        file_health("factor_library_snapshot.json", 60),
        file_health("news_sentiment.json", 10 * 60),
        file_health("trading_ledger.json", 15 * 60),
    ]
    positions_payload = read_json("position_trackers.json", {})
    return {
        "service": {"version": "6.2.1", "pid": os.getpid(), "uptime_seconds": int(time.time() - STARTED_AT)},
        "credentials": {"okx": bool(settings.okx_api_key and settings.okx_secret_key and settings.okx_passphrase), "llm": bool(settings.llm_api_key)},
        "data_health": health_files,
        "decisions": decision_summary(),
        "trackers": len(positions_payload) if isinstance(positions_payload, dict) else 0,
        "logs": {
            "trader": log_tail("ai_factor_trader.log", 18),
            "backend": log_tail("r20_backend.log", 18),
            "scheduler": log_tail("r20_scheduler.log", 18),
        },
        "audit": recent_audit(20),
    }


def git(command: list[str]) -> str:
    try:
        result = subprocess.run(["git", *command], cwd=ROOT, text=True, capture_output=True, timeout=30)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"git command timed out after {exc.timeout}s") from exc
    if result.returncode:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "git command failed")
    return result.stdout.strip()


def update_status() -> dict[str, Any]:
    try:
        local = git(["rev-parse", "--short", "HEAD"])
        branch = git(["branch", "--show-current"])
        dirty = bool(git(["status", "--porcelain"]))
        remote = ""
        behind = ahead = 0
        try:
            git(["fetch", "--quiet", "origin", branch])
            remote = git(["rev-parse", "--short", f"origin/{branch}"])
            ahead, behind = [int(item) for item in git(["rev-list", "--left-right", "--count", f"HEAD...origin/{branch}"]).split()]
        except RuntimeError:
            pass
        return {"branch": branch, "local": local, "remote": remote, "behind": behind, "ahead": ahead, "dirty": dirty}
    except RuntimeError as exc:
        return {"error": str(exc)}



@app.get("/admin", include_in_schema=False)
def admin_page() -> FileResponse:
    # Check for Vue SPA build first; fall back to legacy admin.html
    vue_index = ROOT / "frontend" / "dist" / "index.html"
    if vue_index.is_file():
        return FileResponse(str(vue_index), headers={"Cache-Control": "no-cache, no-store, must-revalidate"})
    return FileResponse(ADMIN_HTML, headers={"Cache-Control": "no-cache, no-store, must-revalidate"})


@app.get("/api/v1/admin/auth/status")
def admin_auth_status() -> dict[str, Any]:
    return {"initialized": admin_auth.has_users(), "mode": "account-password", "session_hours": 12}


@app.post("/api/v1/admin/login", include_in_schema=False)
@app.post("/api/v1/admin/auth/login")
def admin_login(payload: AdminLoginRequest) -> dict[str, Any]:
    try:
        result = admin_auth.login(payload.username, payload.password)
    except PermissionError as exc:
        audit_record("admin.login", "failed", {"username": payload.username})
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    audit_record("admin.login", "success", {"username": result["user"]["username"]})
    return result


@app.post("/api/v1/admin/logout", include_in_schema=False)
@app.post("/api/v1/admin/auth/logout")
def admin_logout(x_r20_session: str | None = Header(default=None)) -> dict[str, Any]:
    user = admin_auth.validate_session(x_r20_session or "")
    admin_auth.logout(x_r20_session or "")
    if user:
        audit_record("admin.logout", "success", {"username": user["username"]})
    return {"logged_out": True}


@app.get("/api/v1/admin/auth/me")
def admin_me(x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    user = current_admin(x_r20_session, None)
    if user.get("role") == "legacy":
        raise HTTPException(status_code=401, detail="请使用管理员账号密码登录")
    return {"user": user}


@app.get("/api/v1/admin/users")
def admin_users(x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    return {"users": admin_auth.list_users(), "current_user_id": actor["id"]}


@app.post("/api/v1/admin/users")
def create_admin_user(payload: AdminCreateRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try:
        user = admin_auth.create_user(payload.username, payload.password, payload.role)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("admin.user.create", "success", {"actor": actor["username"], "username": user["username"], "role": user["role"]})
    return {"created": user}


@app.put("/api/v1/admin/users/{user_id}/enabled")
def update_admin_enabled(user_id: int, payload: AdminEnabledRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try:
        admin_auth.set_enabled(user_id, payload.enabled, actor["id"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit_record("admin.user.enabled", "success", {"actor": actor["username"], "user_id": user_id, "enabled": payload.enabled})
    return {"user": admin_auth.get_user(user_id)}


@app.post("/api/v1/admin/users/{user_id}/unlock")
def unlock_admin_user(user_id: int, payload: AdminUnlockRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    expected = f"UNLOCK ADMIN {user_id}"
    if payload.confirmation.strip().upper() != expected:
        raise HTTPException(status_code=400, detail=f"确认短语必须精确为：{expected}")
    try:
        admin_auth.unlock_user(user_id)
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    audit_record("admin.user.unlock", "success", {"actor": actor["username"], "user_id": user_id})
    return {"user": admin_auth.get_user(user_id)}


@app.put("/api/v1/admin/users/{user_id}/password")
def update_admin_password(user_id: int, payload: AdminPasswordRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = current_admin(x_r20_session, None)
    if actor.get("role") == "legacy":
        raise HTTPException(status_code=401, detail="请使用管理员账号密码登录")
    if actor["id"] != user_id and actor["role"] != "superadmin":
        raise HTTPException(status_code=403, detail="只能修改自己的密码")
    if actor["id"] == user_id and not admin_auth.verify_password(actor["id"], payload.current_password):
        raise HTTPException(status_code=403, detail="当前密码不正确")
    try:
        admin_auth.change_password(user_id, payload.new_password)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("admin.password.update", "success", {"actor": actor["username"], "user_id": user_id})
    return {"changed": True, "reauthenticate": True}


@app.get("/api/v1/admin/overview")
def admin_overview(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    return runtime_overview()


@app.get("/api/v1/admin/audit")
def admin_audit(x_r20_admin_token: str | None = Header(default=None), limit: int = 50) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    return {"records": recent_audit(limit)}


ADMIN_LOG_SOURCES = {"trader": "ai_factor_trader.log", "backend": "r20_backend.log", "scheduler": "r20_scheduler.log"}


@app.get("/api/v1/admin/runtime")
def admin_runtime(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    payload = runtime_overview()
    payload["full_decisions"] = read_json("ai_brain_decisions.json", {})
    payload["llm_runtime"] = get_active_llm_runtime()
    return payload


@app.get("/api/v1/admin/logs")
def admin_logs(source: str = "trader", lines: int = 100, x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    filename = ADMIN_LOG_SOURCES.get(source)
    if not filename:
        raise HTTPException(status_code=400, detail=f"日志来源仅支持：{', '.join(ADMIN_LOG_SOURCES)}")
    return {"source": source, "file": filename, "content": log_tail(filename, lines)}


@app.get("/api/v1/admin/gateway")
def gateway_status(x_r20_admin_token: str | None = Header(default=None), limit: int = 50) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    store = GatewayStore(GATEWAY_DB_PATH)
    pid_file = DATA_DIR / "r20_gateway.pid"
    pid = int(pid_file.read_text().strip()) if pid_file.exists() and pid_file.read_text().strip().isdigit() else 0
    running = False
    if pid:
        try:
            os.kill(pid, 0)
            running = True
        except OSError:
            pass
    return {"version": GATEWAY_VERSION, "running": running, "pid": pid or None, "stats": store.stats(), "event_health": store.event_health(), "deliveries": store.recent(limit), "scheduler": scheduler_snapshot(store)}


@app.post("/api/v1/admin/gateway/deliveries/{delivery_id}/replay")
def replay_gateway_delivery(delivery_id: int, payload: GatewayReplayRequest, x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    if payload.confirmation.strip().upper() != f"REPLAY {delivery_id}":
        raise HTTPException(status_code=400, detail=f"确认短语必须精确为：REPLAY {delivery_id}")
    store = GatewayStore(GATEWAY_DB_PATH)
    if not store.replay_dead(delivery_id):
        raise HTTPException(status_code=409, detail="仅允许重放当前处于 dead 状态的投递")
    audit_record("gateway.delivery.replay", "accepted", {"delivery_id": delivery_id})
    return {"accepted": True, "delivery_id": delivery_id, "status": "pending"}


@app.get("/api/v1/admin/agents")
def admin_agents(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    store = GatewayStore(GATEWAY_DB_PATH)
    return {
        "agents": agent_statuses(store.job_runs(100)),
        "model_stats": store.model_stats(),
        "model_calls": store.model_calls(50),
        "prompt_policy": "交易主脑和自进化均由 Python 直接构造并传输提示词；Gateway 只记录无内容遥测。",
        "secret_store": secret_store_status(),
    }


@app.get("/api/v1/admin/plugins")
def admin_plugins(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    return {"plugins": plugin_statuses(), "installation_policy": "builtin-only", "reason": "实盘控制面不允许远程上传或执行任意插件代码"}


@app.get("/api/v1/admin/config")
def admin_config(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    return {
        "authentication_mode": "account-password",
        "configuration": {
            "OKX 当前环境": "模拟盘 DEMO" if settings.okx_simulated else "实盘 LIVE",
            "OKX 实盘凭证": "已完整配置" if settings.okx_live_configured else "未完整配置",
            "OKX 模拟盘凭证": "已完整配置" if settings.okx_demo_configured else "使用 OAuth/旧凭证或未配置",
            "LLM API Key": "已设置" if settings.llm_api_key else "未设置",
            "管理员系统": "账号密码 + 服务端会话" if admin_auth.has_users() else "尚未初始化",
            "通知 Webhook": "已设置" if settings.notification_webhook else "未设置",
            "手动平仓": "已启用" if settings.manual_close_enabled else "已禁用",
            "主页初始本金": f"{load_account_baseline()['initial_capital']:,.2f} USDT",
        },
        "editable": {
            "okx_environment": settings.okx_environment,
            "okx_live_configured": settings.okx_live_configured,
            "okx_demo_configured": settings.okx_demo_configured,
            "okx_simulated": settings.okx_simulated,
            "llm_base_url": settings.llm_base_url,
            "llm_model": settings.llm_model,
            "llm_reasoning_effort": settings.llm_reasoning_effort,
            "notification_webhook": settings.notification_webhook,
            "manual_close_enabled": settings.manual_close_enabled,
            "initial_capital": load_account_baseline()["initial_capital"],
            "initial_capital_reset_time": load_account_baseline()["reset_time"],
        },
    }


@app.put("/api/v1/admin/account-baseline")
def admin_update_account_baseline(payload: InitialCapitalUpdate, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    if payload.confirmation.strip().upper() != "UPDATE CAPITAL":
        raise HTTPException(status_code=400, detail="确认短语必须精确为：UPDATE CAPITAL")
    try:
        result = update_initial_capital(payload.initial_capital)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("account.baseline.update", "success", {
        "actor": actor["username"],
        "previous_initial_capital": result["previous_initial_capital"],
        "initial_capital": result["initial_capital"],
        "reset_time_preserved": result["reset_time"],
    })
    return {
        "updated": True,
        **result,
        "effect": "主页累计盈亏、累计 ROI 与权益基准线将按新本金重算；历史起算时间保持不变。",
    }


_OKX_RUNTIME_CACHE: dict[str, Any] = {"at": 0.0, "mode": "", "payload": None}


@app.get("/api/v1/admin/okx/runtime")
def admin_okx_runtime(x_r20_session: str | None = Header(default=None, alias="X-R20-Session"), refresh: int = 0) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_session=x_r20_session)
    configured = settings.okx_demo_configured if settings.okx_environment == "demo" else settings.okx_live_configured
    now = time.time()
    # Each CLI probe spawns 4-6 subprocesses (~1.5s). Serve a 15s cache so page
    # navigation feels instant; the UI's "重新诊断" passes refresh=1 to bypass.
    if not refresh and _OKX_RUNTIME_CACHE["payload"] and _OKX_RUNTIME_CACHE["mode"] == settings.okx_environment and now - _OKX_RUNTIME_CACHE["at"] < 15:
        return dict(_OKX_RUNTIME_CACHE["payload"])
    payload = diagnose_okx_runtime(settings.okx_environment, configured)
    _OKX_RUNTIME_CACHE.update({"at": now, "mode": settings.okx_environment, "payload": copy.deepcopy(payload)})
    return payload


@app.post("/api/v1/admin/okx/oauth/start")
def admin_okx_oauth_start(payload: OkxOAuthStartRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try:
        result = start_oauth_device_login(payload.site)
    except (ValueError, RuntimeError) as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("okx.oauth.start", "success", {"actor": actor["username"], "site": payload.site, "status": result.get("status")})
    return result


@app.get("/api/v1/admin/okx/oauth/status")
def admin_okx_oauth_status(x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    require_admin_header(x_r20_session=x_r20_session)
    return oauth_status()


@app.get("/api/v1/admin/okx/cli-check")
def admin_okx_cli_check(x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    """Check Node.js/npm/OKX CLI prerequisites without side effects."""
    require_admin_header(x_r20_session=x_r20_session)
    return check_node_npm()


@app.post("/api/v1/admin/okx/install-cli")
def admin_okx_install_cli(payload: OkxCliInstallRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    """One-click install or upgrade OKX CLI via npm. Requires superadmin and explicit confirmation."""
    actor = require_superadmin(x_r20_session)
    if payload.confirmation.strip().upper() != "INSTALL OKX CLI":
        raise HTTPException(status_code=400, detail="确认短语必须精确为：INSTALL OKX CLI")
    result = install_okx_cli()
    audit_record("okx.cli.install", "success" if result.get("ok") else "failed", {"actor": actor["username"], "detail": result.get("detail", "")[:300]})
    if not result.get("ok"):
        raise HTTPException(status_code=502, detail=result.get("detail") or "OKX CLI 安装失败")
    return result


@app.put("/api/v1/admin/config")
def update_admin_config(payload: AdminConfigUpdate, x_r20_admin_token: str | None = Header(default=None), x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    refresh_settings()
    data = payload.model_dump(exclude_none=True)
    sensitive = any(key.startswith("okx_") or key == "manual_close_enabled" for key in data)
    if sensitive: require_superadmin(x_r20_session)
    else: require_admin_header(x_r20_admin_token)
    if "llm_base_url" in data and data["llm_base_url"] and not data["llm_base_url"].startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="LLM Base URL 必须以 http:// 或 https:// 开头")
    if "notification_webhook" in data and data["notification_webhook"] and not data["notification_webhook"].startswith(("https://", "http://")):
        raise HTTPException(status_code=400, detail="Webhook 必须以 http:// 或 https:// 开头")
    selected_mode = data.get("okx_environment") or ("demo" if data.get("okx_simulated") else "live" if "okx_simulated" in data else None)
    if selected_mode and selected_mode != settings.okx_environment:
        import fcntl
        lock_path = DATA_DIR / ".ai_factor_trader.lock"; lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+", encoding="utf-8") as lock_handle:
            try: fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise HTTPException(status_code=409, detail="交易周期正在执行，OKX 环境已冻结；请等待本周期结束后再切换")
            finally:
                try: fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
                except OSError: pass
    secret_values = {
        "OKX_LIVE_API_KEY": data.get("okx_live_api_key"), "OKX_LIVE_SECRET_KEY": data.get("okx_live_secret_key"), "OKX_LIVE_PASSPHRASE": data.get("okx_live_passphrase"),
        "OKX_DEMO_API_KEY": data.get("okx_demo_api_key"), "OKX_DEMO_SECRET_KEY": data.get("okx_demo_secret_key"), "OKX_DEMO_PASSPHRASE": data.get("okx_demo_passphrase"),
        "OKX_API_KEY": data.get("okx_api_key"), "OKX_SECRET_KEY": data.get("okx_secret_key"), "OKX_PASSPHRASE": data.get("okx_passphrase"),
        "LLM_API_KEY": data.get("llm_api_key"),
    }
    save_secrets({key: value for key, value in secret_values.items() if value})
    env_values = {
        "R20_OKX_ENV": selected_mode,
        "OKX_IS_SIMULATED": "1" if selected_mode == "demo" else "0" if selected_mode else None,
        "LLM_BASE_URL": data.get("llm_base_url"),
        "LLM_MODEL": data.get("llm_model"),
        "LLM_REASONING_EFFORT": data.get("llm_reasoning_effort"),
        "R20_NOTIFICATION_WEBHOOK": data.get("notification_webhook"),
        "R20_MANUAL_CLOSE_ENABLED": "1" if data.get("manual_close_enabled") else "0" if "manual_close_enabled" in data else None,
    }
    update_env(env_values)
    refresh_settings()
    if any(k.startswith("llm_") for k in data):
        try:
            cfg = init_llm_providers()
            active_p = next((p for p in cfg.get("providers", []) if p["id"] == cfg.get("active_provider_id")), None)
            if active_p:
                if "llm_base_url" in data and data["llm_base_url"]:
                    active_p["base_url"] = data["llm_base_url"].rstrip("/")
                if "llm_api_key" in data and data["llm_api_key"]:
                    active_p["api_key"] = data["llm_api_key"]
                if "llm_model" in data and data["llm_model"]:
                    cfg["active_model_id"] = data["llm_model"]
                if "llm_reasoning_effort" in data and data["llm_reasoning_effort"]:
                    cfg["active_reasoning_effort"] = data["llm_reasoning_effort"]
                _atomic_write_json(LLM_PROVIDERS_FILE, cfg)
        except Exception:
            pass
    audit_record("config.update", "success", {"fields": sorted(data.keys())})
    return {
        "updated": True,
        "restart_note": "Long-running strategy processes read updated .env on their next execution cycle.",
        "manual_close_enabled": settings.manual_close_enabled,
    }


@app.get("/api/v1/admin/llm/providers")
@app.get("/api/v1/admin/llm/models")
def admin_get_llm_models(x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    require_admin_header(x_r20_session=x_r20_session)
    return load_llm_config(mask_keys=True)


@app.post("/api/v1/admin/llm/activate")
def admin_activate_llm_model(payload: LLMActivateRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try:
        result = activate_provider_model(payload.provider_id or "custom", payload.model_id, payload.reasoning_effort)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("llm.model.activate", "success", {
        "actor": actor["username"],
        "model_id": payload.model_id,
        "reasoning_effort": result.get("active_reasoning_effort"),
    })
    return result


@app.post("/api/v1/admin/llm/test")
def admin_test_llm(payload: LLMTestRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    require_admin_header(x_r20_session=x_r20_session)
    base_url = payload.base_url
    api_key = payload.api_key
    api_format = payload.api_format or "openai_chat"

    raw_config = load_llm_config(mask_keys=False)
    m_entry = next((m for m in raw_config.get("models", []) if m["id"] == payload.model), None)
    if m_entry:
        if not base_url:
            base_url = m_entry.get("base_url")
        if not api_key:
            api_key = m_entry.get("api_key")
        if not payload.api_format or payload.api_format == "openai_chat":
            api_format = m_entry.get("api_format", "openai_chat")

    if not base_url:
        active_runtime = get_active_llm_runtime()
        base_url = active_runtime.get("base_url")
        if not api_key:
            api_key = active_runtime.get("api_key")
        if not payload.api_format:
            api_format = active_runtime.get("api_format", "openai_chat")

    result = test_llm_connection(
        base_url=base_url or "",
        api_key=api_key or "",
        model=payload.model,
        api_format=api_format,
        reasoning_effort=payload.reasoning_effort,
        reasoning_type=payload.reasoning_type,
        timeout=25.0,
    )
    audit_record("llm.connection.test", "success" if result.get("ok") else "failed", {
        "model": payload.model,
        "api_format": api_format,
        "latency_ms": result.get("latency_ms"),
        "status_code": result.get("status_code"),
        "reasoning_detected": result.get("reasoning_detected"),
        "endpoint": result.get("endpoint"),
    })
    return result


@app.post("/api/v1/admin/llm/models")
@app.post("/api/v1/admin/llm/providers/{provider_id}/models")
def admin_upsert_llm_model(payload: LLMModelUpsertRequest, provider_id: str = "custom", x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try:
        res = upsert_model(provider_id, payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("llm.model.upsert", "success", {"actor": actor["username"], "model_id": payload.id, "api_format": res.get("api_format")})
    return res


@app.delete("/api/v1/admin/llm/models/{model_id}")
@app.delete("/api/v1/admin/llm/providers/{provider_id}/models/{model_id}")
def admin_delete_llm_model(model_id: str, provider_id: str = "custom", x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try:
        deleted = delete_model(provider_id, model_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="未找到该模型")
    audit_record("llm.model.delete", "success", {"actor": actor["username"], "model_id": model_id})
    return {"deleted": True, "model_id": model_id}


@app.post("/api/v1/admin/llm/providers")
def admin_upsert_llm_provider(payload: LLMProviderUpsertRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try:
        res = upsert_provider(payload.model_dump(exclude_none=True))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("llm.provider.upsert", "success", {"actor": actor["username"], "provider_id": res.get("id")})
    return res


@app.delete("/api/v1/admin/llm/providers/{provider_id}")
def admin_delete_llm_provider(provider_id: str, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try:
        deleted = delete_provider(provider_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    if not deleted:
        raise HTTPException(status_code=404, detail="未找到该模型供应商")
    audit_record("llm.provider.delete", "success", {"actor": actor["username"], "provider_id": provider_id})
    return {"deleted": True, "provider_id": provider_id}


# ============================================================================
# MULTI-AGENT COUNCIL (多模型协作决策系统) ENDPOINTS
# ============================================================================

@app.get("/api/v1/admin/council/config")
def admin_get_council_config(x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    require_admin_header(x_r20_session=x_r20_session)
    from r20_backend.council_manager import load_council_config
    return load_council_config()


@app.put("/api/v1/admin/council/config")
def admin_update_council_config(payload: CouncilConfigUpdateRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    from r20_backend.council_manager import save_council_config
    saved = save_council_config({
        "enabled": payload.enabled,
        "timeout_seconds": payload.timeout_seconds,
        "roles": payload.roles,
    })
    audit_record("council.config.update", "success", {
        "actor": actor["username"],
        "enabled": payload.enabled,
        "timeout_seconds": payload.timeout_seconds,
    })
    return {"status": "ok", "config": saved}


@app.post("/api/v1/admin/council/reset-role")
def admin_reset_council_role(payload: CouncilResetRoleRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    from r20_backend.council_manager import reset_role_template
    saved = reset_role_template(payload.role_id)
    audit_record("council.role.reset", "success", {"actor": actor["username"], "role_id": payload.role_id})
    return {"status": "ok", "role_id": payload.role_id, "config": saved}


@app.post("/api/v1/admin/council/test")
def admin_test_council_debate(payload: CouncilTestRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    require_admin_header(x_r20_session=x_r20_session)
    from r20_backend.council_manager import execute_council_debate, load_council_config
    c_cfg = load_council_config()
    test_market = payload.mock_market_prompt or (
        "【测试行情快照】\n"
        "BTC: $77,750, 1H v=+0.08, a=+0.42, ADX=18.5, CMF=+0.12, 聪明钱多头 74%\n"
        "ETH: $2,408, 1H v=-0.12, a=-0.38, ADX=26.2, CMF=-0.08, 聪明钱空头 65%\n"
        "SOL: $100.8, 1H v=+0.02, a=+0.15, ADX=20.1, CMF=+0.05, 聪明钱中性\n"
    )
    test_sys = "你是一个遵循极严风控的量化交易系统。"
    try:
        brain_output, transcript = execute_council_debate(
            market_prompt=test_market,
            original_system_prompt=test_sys,
            timeout=float(c_cfg.get("timeout_seconds", 60.0)),
        )
        return {
            "status": "ok",
            "brain_output": brain_output,
            "transcript": transcript,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": str(exc),
        }
    audit_record("llm.model.delete", "success", {"actor": actor["username"], "provider_id": provider_id, "model_id": model_id})
    return {"deleted": True, "provider_id": provider_id, "model_id": model_id}


@app.get("/api/v1/admin/okx/account-snapshot")
def admin_okx_account_snapshot(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_header(x_r20_admin_token)
    try: return okx_account_snapshot()
    except Exception as exc: raise HTTPException(status_code=502, detail=f"获取 OKX 当前订单失败：{exc}") from exc


@app.post("/api/v1/admin/positions/close")
def manual_close_position(payload: ManualCloseRequest) -> dict[str, Any]:
    actor = require_superadmin(REQUEST_SESSION.get())
    refresh_settings()
    if not settings.manual_close_enabled:
        raise HTTPException(status_code=403, detail="后台手动平仓功能未启用")
    import fcntl
    lock_path = DATA_DIR / ".ai_factor_trader.lock"; lock_path.parent.mkdir(parents=True, exist_ok=True)
    if actor.get("role") == "legacy" or not admin_auth.verify_password(int(actor["id"]), payload.admin_password):
        raise HTTPException(status_code=403, detail="管理员密码验证失败")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        try: fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: raise HTTPException(status_code=409, detail="交易主循环正在执行，暂不允许后台快速平仓；请等待本周期结束")
        try:
            result = fast_close_confirmed(payload.close_token, payload.confirmation)
            audit_record("position.close", "confirmed_closed", {"instId": result.get("instId"), "side": result.get("posSide"), "environment": result.get("environment"), "size": result.get("closed_size")})
            return result
        except ValueError as exc:
            audit_record("position.close", "rejected", {"error": str(exc)[:300]})
            raise HTTPException(status_code=409, detail=str(exc)) from exc
        except Exception as exc:
            audit_record("position.close", "verification_failed", {"error": str(exc)[:300]})
            raise HTTPException(status_code=502, detail=f"OKX 快速平仓未完成确认：{exc}") from exc
        finally: fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


@app.get("/api/v1/admin/instruments")
def admin_instruments(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    trackers = read_json("position_trackers.json", {})
    active = set(trackers.keys()) if isinstance(trackers, dict) else set()
    return {
        "instruments": [{**item, "protected": item["instId"] == "BTC-USDT-SWAP", "has_tracker": item["instId"] in active or item["name"] in active} for item in load_instruments()],
        "limits": {"minimum": 1, "maximum": 6, "btc_required": True},
    }


@app.post("/api/v1/admin/instruments")
def add_admin_instrument(payload: InstrumentAddRequest, x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    inst_id = payload.inst_id.upper()
    current = load_instruments()
    if any(item["instId"] == inst_id for item in current):
        raise HTTPException(status_code=409, detail="该币种已在交易池中")
    if len(current) >= 6:
        raise HTTPException(status_code=409, detail="交易池最多允许 6 个币种；请先删除一个无持仓币种")
    try:
        matches = okx.instruments("SWAP", inst_id)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"OKX 合约校验失败：{exc}") from exc
    raw = matches[0] if matches else {}
    if raw.get("instId") != inst_id or raw.get("settleCcy") != "USDT" or raw.get("state") != "live":
        raise HTTPException(status_code=400, detail="仅允许添加 OKX 在线可交易的 USDT 永续合约")
    item = from_okx_instrument(raw)
    save_instruments([*current, item])
    audit_record("instrument.add", "success", {"instId": inst_id})
    return {"added": item, "count": len(current) + 1, "effective": "next_process_cycle"}


@app.delete("/api/v1/admin/instruments/{inst_id}")
def delete_admin_instrument(inst_id: str, payload: InstrumentDeleteRequest, x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    inst_id = inst_id.upper()
    if payload.confirmation.strip().upper() != f"REMOVE {inst_id}":
        raise HTTPException(status_code=400, detail=f"确认短语必须精确为：REMOVE {inst_id}")
    if inst_id == "BTC-USDT-SWAP":
        raise HTTPException(status_code=403, detail="BTC 是全局黑天鹅哨兵基准，不允许从交易池删除")
    current = load_instruments()
    if len(current) <= 1:
        raise HTTPException(status_code=409, detail="交易池至少保留 1 个币种")
    if not any(item["instId"] == inst_id for item in current):
        raise HTTPException(status_code=404, detail="该币种不在交易池中")
    trackers = read_json("position_trackers.json", {})
    coin = inst_id.split("-", 1)[0]
    if isinstance(trackers, dict) and (inst_id in trackers or coin in trackers):
        raise HTTPException(status_code=409, detail="该币种存在持仓追踪记录，为防止失去风控接管，禁止删除")
    updated = [item for item in current if item["instId"] != inst_id]
    save_instruments(updated)
    audit_record("instrument.remove", "success", {"instId": inst_id})
    return {"removed": inst_id, "count": len(updated), "effective": "next_process_cycle"}


@app.get("/api/v1/admin/about")
def admin_about(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_header(x_r20_admin_token)
    import platform
    store = GatewayStore(GATEWAY_DB_PATH)
    return {
        "product": {"name": "R20 Quantum Trader", "version": "6.2.1", "control_plane": "R20 Gateway Runtime", "gateway_version": GATEWAY_VERSION},
        "runtime": {"python": platform.python_version(), "platform": platform.platform(), "backend_pid": os.getpid(), "gateway": gateway_status(x_r20_admin_token)},
        "components": [
            {"name": "FastAPI Control Plane", "version": "6.2.1"},
            {"name": "Gateway Event Runtime", "version": GATEWAY_VERSION},
            {"name": "SQLite", "version": __import__("sqlite3").sqlite_version},
        ],
        "repository": {"url": "https://github.com/555cute/r20-quantum-trader", "branch": git(["branch", "--show-current"]), "commit": git(["rev-parse", "--short", "HEAD"])},
        "update": update_status(),
        "security": {"authentication": "PBKDF2-SHA256 + server-side sessions", "session_hours": 12, "plugin_policy": "builtin-only", "prompt_transport": "python-direct"},
    }


@app.get("/api/v1/admin/update-status")
def admin_update_status(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    return update_status()


@app.post("/api/v1/admin/update")
def update_application(payload: UpdateRequest, x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    if payload.confirmation.strip().upper() != "UPDATE R20":
        raise HTTPException(status_code=400, detail="确认短语必须精确为：UPDATE R20")
    status_before = update_status()
    if status_before.get("error"):
        raise HTTPException(status_code=502, detail=status_before["error"])
    if status_before["dirty"]:
        raise HTTPException(status_code=409, detail="工作区存在未提交修改；为防止覆盖本地改动，后台拒绝更新")
    if not status_before["remote"]:
        raise HTTPException(status_code=502, detail="无法读取远程仓库状态")
    try:
        output = git(["pull", "--ff-only", "origin", status_before["branch"]])
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=f"更新失败：{exc}") from exc
    status_after = update_status()
    audit_record("application.update", "success", {"before": status_before.get("local"), "after": status_after.get("local")})
    return {
        "updated": status_before["local"] != status_after.get("local"),
        "before": status_before,
        "after": status_after,
        "git_output": output,
        "restart_required": True,
        "restart_note": "请重启 r20-quantum 与 keel-worker 服务，让新代码接管后台与调度。勿启用已废弃的 r20-scheduler。",
    }


@app.get("/api/v1/admin/prompt-library")
def prompt_library(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    from scripts.ai_brain_trader import SYSTEM_PROMPT
    from scripts.self_improvement_engine import EVOLUTION_SYSTEM_PROMPT
    library = load_library()
    profile = active_profile()
    return {
        "active_style": library["active_style"],
        "active_profile_id": library["active_profile_id"],
        "profiles": [{**item, "pipeline_views": {
            "trading_system": pipeline_view(SYSTEM_PROMPT, item, "trading_system"),
            "trading_user": pipeline_view(TRADING_USER_TEMPLATE, item, "trading_user"),
            "evolution_system": pipeline_view(EVOLUTION_SYSTEM_PROMPT, item, "evolution_system"),
            "evolution_user": pipeline_view(EVOLUTION_USER_TEMPLATE, item, "evolution_user"),
        }} for item in all_profiles()],
        "base_templates": {
            "trading_system": SYSTEM_PROMPT,
            "trading_user": TRADING_USER_TEMPLATE,
            "evolution_system": EVOLUTION_SYSTEM_PROMPT,
            "evolution_user": EVOLUTION_USER_TEMPLATE,
        },
        "pipelines": {
            "trading_system": pipeline_view(SYSTEM_PROMPT, profile, "trading_system"),
            "trading_user": pipeline_view(TRADING_USER_TEMPLATE, profile, "trading_user"),
            "evolution_system": pipeline_view(EVOLUTION_SYSTEM_PROMPT, profile, "evolution_system"),
            "evolution_user": pipeline_view(EVOLUTION_USER_TEMPLATE, profile, "evolution_user"),
        },
        "effective_templates": {
            "trading_system": apply_module_layout(SYSTEM_PROMPT, profile, "trading_system", "交易 System"),
            "trading_user": apply_module_layout(TRADING_USER_TEMPLATE, profile, "trading_user", "交易 User"),
            "evolution_system": apply_module_layout(EVOLUTION_SYSTEM_PROMPT, profile, "evolution_system", "自进化 System"),
            "evolution_user": apply_module_layout(EVOLUTION_USER_TEMPLATE, profile, "evolution_user", "自进化 User"),
        },
        "snapshots": rendered_snapshots(),
        "transport": "python-direct",
    }


@app.put("/api/v1/admin/prompt-library")
def update_prompt_library(payload: PromptLibraryUpdate, x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    library = load_library()
    library["active_style"] = payload.active_style
    library["custom"] = {
        "id": "custom", "name": "自定义", "description": "管理员自定义风格附加层。", "editable": True,
        "trading_system": payload.trading_system.strip(), "trading_user": payload.trading_user.strip(),
        "evolution_system": payload.evolution_system.strip(), "evolution_user": payload.evolution_user.strip(),
    }
    save_library(library)
    audit_record("prompt.library.update", "success", {"active_style": payload.active_style, "custom_characters": sum(len(getattr(payload, key)) for key in ("trading_system", "trading_user", "evolution_system", "evolution_user"))})
    return {"saved": True, "active_style": payload.active_style, "restart_note": "下一次 Python 交易主脑与自进化进程自动读取选中风格。"}


@app.get("/api/v1/admin/prompt-profiles")
def prompt_profiles(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_header(x_r20_admin_token)
    library = load_library()
    return {"active_profile_id": library["active_profile_id"], "profiles": all_profiles(), "allowed_variables": sorted(__import__("scripts.prompt_library", fromlist=["ALLOWED_VARIABLES"]).ALLOWED_VARIABLES)}


@app.post("/api/v1/admin/prompt-profiles")
def create_prompt_profile_api(payload: PromptProfileCreateRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try: profile = create_profile(payload.name, payload.description, payload.source_id)
    except ValueError as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("prompt.profile.create", "success", {"actor": actor["username"], "profile_id": profile["id"]})
    return {"profile": profile}


@app.put("/api/v1/admin/prompt-profiles/{profile_id}")
def update_prompt_profile_api(profile_id: str, payload: PromptProfileUpdateRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try: profile = update_profile(profile_id, payload.model_dump(exclude={"note"}), payload.note)
    except ValueError as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("prompt.profile.update", "success", {"actor": actor["username"], "profile_id": profile_id})
    return {"profile": profile, "validation": validate_profile(profile)}


@app.post("/api/v1/admin/prompt-profiles/{profile_id}/activate")
def activate_prompt_profile_api(profile_id: str, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try: profile = activate_profile(profile_id)
    except ValueError as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit_record("prompt.profile.activate", "success", {"actor": actor["username"], "profile_id": profile_id})
    return {"active_profile_id": profile_id, "profile": profile}


@app.delete("/api/v1/admin/prompt-profiles/{profile_id}")
def delete_prompt_profile_api(profile_id: str, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try: delete_profile(profile_id)
    except ValueError as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit_record("prompt.profile.delete", "success", {"actor": actor["username"], "profile_id": profile_id})
    return {"deleted": True}


@app.post("/api/v1/admin/prompt-profiles/validate")
def validate_prompt_profile_api(payload: PromptProfileUpdateRequest, x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_header(x_r20_admin_token)
    return validate_profile(payload.model_dump())


@app.get("/api/v1/admin/prompt-profiles/{profile_id}/history")
def prompt_profile_history_api(profile_id: str, x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_header(x_r20_admin_token)
    return {"history": profile_history(profile_id)}


@app.post("/api/v1/admin/prompt-profiles/{profile_id}/rollback")
def rollback_prompt_profile_api(profile_id: str, payload: PromptRollbackRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try: profile = rollback_profile(profile_id, payload.revision_id)
    except ValueError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc
    audit_record("prompt.profile.rollback", "success", {"actor": actor["username"], "profile_id": profile_id, "revision_id": payload.revision_id})
    return {"profile": profile}


@app.get("/api/v1/admin/prompt-profiles/{profile_id}/export")
def export_prompt_profile_api(profile_id: str, x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_header(x_r20_admin_token)
    try: return export_profile(profile_id)
    except ValueError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/admin/prompt-profiles/import")
def import_prompt_profile_api(payload: PromptImportRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try: profile = import_profile(payload.payload, payload.name_override)
    except ValueError as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("prompt.profile.import", "success", {"actor": actor["username"], "profile_id": profile["id"]})
    return {"profile": profile}


@app.get("/api/v1/admin/prompts")
def prompt_override(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    from scripts.ai_brain_trader import SYSTEM_PROMPT
    content = PROMPT_OVERRIDE_FILE.read_text(encoding="utf-8") if PROMPT_OVERRIDE_FILE.exists() else ""
    effective = SYSTEM_PROMPT if not content.strip() else f"{SYSTEM_PROMPT}\n\n【管理员提示词覆盖层（同样必须遵守上述风控和 JSON 约束）】\n{content.strip()}"
    return {
        "content": content,
        "enabled": bool(content.strip()),
        "base_prompt": SYSTEM_PROMPT,
        "effective_prompt": effective,
        "path": str(PROMPT_OVERRIDE_FILE),
    }


@app.put("/api/v1/admin/prompts")
def update_prompt_override(payload: PromptOverrideRequest, x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    content = payload.content.strip()
    if content:
        temp = PROMPT_OVERRIDE_FILE.with_suffix(".tmp")
        temp.write_text(content + "\n", encoding="utf-8")
        os.replace(temp, PROMPT_OVERRIDE_FILE)
    elif PROMPT_OVERRIDE_FILE.exists():
        PROMPT_OVERRIDE_FILE.unlink()
    audit_record("prompt.update", "success", {"enabled": bool(content), "characters": len(content)})
    return {"saved": True, "enabled": bool(content), "restart_note": "下一次 AI 推演循环将自动叠加此提示词覆盖层。"}


@app.get("/api/v1/admin/notifications")
def notification_config(x_r20_session: str | None = Header(default=None, alias="X-R20-Session"), x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token, x_r20_session)
    env = notification_env()
    return {
        "webhook": {"enabled": env.get("R20_NOTIFY_WEBHOOK_ENABLED", "0") == "1", "url": env.get("R20_NOTIFICATION_WEBHOOK", "")},
        "wechat": {"enabled": env.get("R20_NOTIFY_WECHAT_ENABLED", "0") == "1", "webhook": env.get("R20_WECHAT_WEBHOOK", "")},
        "telegram": {"enabled": env.get("R20_NOTIFY_TELEGRAM_ENABLED", "0") == "1", "bot_token": mask(env.get("R20_TELEGRAM_BOT_TOKEN", "")), "chat_id": env.get("R20_TELEGRAM_CHAT_ID", ""), "api_base": env.get("R20_TELEGRAM_API_BASE", "")},
        "qq": {"enabled": env.get("R20_NOTIFY_QQ_ENABLED", "0") == "1", "app_id": env.get("R20_QQ_APP_ID", ""), "client_secret": mask(env.get("R20_QQ_CLIENT_SECRET", "")), "openid": env.get("R20_QQ_OPENID", "")},
    }


@app.put("/api/v1/admin/channels/{channel}/toggle")
def toggle_channel(channel: str, payload: ChannelToggleRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session"), x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token, x_r20_session)
    keys = {
        "qq": "R20_NOTIFY_QQ_ENABLED",
        "telegram": "R20_NOTIFY_TELEGRAM_ENABLED", "wechat": "R20_NOTIFY_WECHAT_ENABLED",
        "webhook": "R20_NOTIFY_WEBHOOK_ENABLED",
    }
    if channel not in keys:
        raise HTTPException(status_code=404, detail="未知频道")
    if payload.enabled:
        env = notification_env()
        readiness = {
            "qq": bool(env.get("R20_QQ_APP_ID") and env.get("R20_QQ_CLIENT_SECRET") and env.get("R20_QQ_OPENID")),
            "telegram": bool(env.get("R20_TELEGRAM_BOT_TOKEN") and env.get("R20_TELEGRAM_CHAT_ID")),
            "wechat": bool(env.get("R20_WECHAT_WEBHOOK")),
            "webhook": bool(env.get("R20_NOTIFICATION_WEBHOOK")),
        }
        if not readiness[channel]:
            if channel == "qq" and env.get("R20_QQ_APP_ID") and env.get("R20_QQ_CLIENT_SECRET") and not env.get("R20_QQ_OPENID"):
                raise HTTPException(status_code=409, detail="QQ 缺少目标用户 OpenID，请先点击「⚡ 自动获取 OpenID」向 Bot 发送消息完成绑定")
            raise HTTPException(status_code=409, detail=f"{channel} 频道凭证或目标未配置完整，请先保存有效配置再启用")
    update_env({keys[channel]: "1" if payload.enabled else "0"})
    audit_record("channel.toggle", "success", {"channel": channel, "enabled": payload.enabled})
    return {"channel": channel, "enabled": payload.enabled}


@app.put("/api/v1/admin/notifications")
def update_notification_config(payload: NotificationConfigUpdate, x_r20_admin_token: str | None = Header(default=None), x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    refresh_settings()
    require_superadmin(x_r20_session)

    # Save credentials unconditionally to preserve user configurations
    secret_values = {}
    if payload.webhook_url:
        secret_values["R20_NOTIFICATION_WEBHOOK"] = payload.webhook_url.strip()
    if payload.wechat_webhook:
        secret_values["R20_WECHAT_WEBHOOK"] = payload.wechat_webhook.strip()
    if payload.telegram_bot_token:
        secret_values["R20_TELEGRAM_BOT_TOKEN"] = payload.telegram_bot_token.strip()
    if payload.qq_client_secret:
        secret_values["R20_QQ_CLIENT_SECRET"] = payload.qq_client_secret.strip()

    if secret_values:
        save_secrets(secret_values)
        remove_env(set(secret_values))

    env_update = {
        "R20_TELEGRAM_CHAT_ID": payload.telegram_chat_id.strip(),
        "R20_QQ_APP_ID": payload.qq_app_id.strip(),
        "R20_QQ_OPENID": payload.qq_openid.strip(),
    }
    if payload.telegram_api_base is not None:
        env_update["R20_TELEGRAM_API_BASE"] = payload.telegram_api_base.strip()

    # Determine readiness
    current_env = notification_env()
    readiness = {
        "qq": bool(payload.qq_app_id and (payload.qq_client_secret or current_env.get("R20_QQ_CLIENT_SECRET")) and payload.qq_openid),
        "telegram": bool((payload.telegram_bot_token or current_env.get("R20_TELEGRAM_BOT_TOKEN")) and payload.telegram_chat_id),
        "wechat": bool(payload.wechat_webhook or current_env.get("R20_WECHAT_WEBHOOK")),
        "webhook": bool(payload.webhook_url or current_env.get("R20_NOTIFICATION_WEBHOOK")),
    }

    warnings = []
    # If user checked enabled for an incomplete channel, auto-turn off that specific channel with a helpful warning instead of crashing entire save with 409
    eff_qq = payload.qq_enabled
    if payload.qq_enabled and not readiness["qq"]:
        eff_qq = False
        warnings.append("QQ 频道因缺少 OpenID 暂未开启（请点击「⚡ 自动获取 OpenID」绑定）")

    eff_tg = payload.telegram_enabled
    if payload.telegram_enabled and not readiness["telegram"]:
        eff_tg = False
        warnings.append("Telegram 频道因缺少 Token 或 Chat ID 暂未开启")

    eff_wx = payload.wechat_enabled
    if payload.wechat_enabled and not readiness["wechat"]:
        eff_wx = False
        warnings.append("企业微信频道因缺少 Webhook 暂未开启")

    eff_wh = payload.webhook_enabled
    if payload.webhook_enabled and not readiness["webhook"]:
        eff_wh = False
        warnings.append("通用 Webhook 因缺少 URL 暂未开启")

    env_update.update({
        "R20_NOTIFY_WEBHOOK_ENABLED": "1" if eff_wh else "0",
        "R20_NOTIFY_WECHAT_ENABLED": "1" if eff_wx else "0",
        "R20_NOTIFY_TELEGRAM_ENABLED": "1" if eff_tg else "0",
        "R20_NOTIFY_QQ_ENABLED": "1" if eff_qq else "0",
    })

    update_env(env_update)
    refresh_settings()

    audit_record("notifications.update", "success", {
        "webhook": eff_wh,
        "wechat": eff_wx,
        "telegram": eff_tg,
        "qq": eff_qq,
        "warnings": warnings,
    })
    msg = "全部通知配置已成功保存"
    if warnings:
        msg += f"（提示：{'；'.join(warnings)}）"
    return {"saved": True, "message": msg, "warnings": warnings}


@app.post("/api/v1/admin/notifications/diagnose")
def diagnose_notification(payload: NotificationTestRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session"), x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings(); require_admin_header(x_r20_admin_token, x_r20_session)
    result = diagnose_channel(payload.channel)
    audit_record("notifications.diagnose", "completed", {"channel": payload.channel, "status": result.get("status")})
    return {"channel": payload.channel, "result": result, "sent": False}


@app.post("/api/v1/admin/notifications/test")
def send_notification_test(payload: NotificationTestRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    refresh_settings(); require_superadmin(x_r20_session)
    if payload.confirmation.strip().upper() != f"SEND TEST {payload.channel.upper()}":
        raise HTTPException(status_code=400, detail=f"确认短语必须为：SEND TEST {payload.channel.upper()}")
    result = test_channel(payload.channel)
    audit_record("notifications.test", "completed", {"channel": payload.channel, "result": result})
    return {"channel": payload.channel, "result": result, "sent": True, "meaning": "远端接口已受理不等于用户客户端已读"}


@app.post("/api/v1/admin/notifications/qq/capture-openid/start")
def qq_capture_openid_start(payload: QQOpenIDCaptureStartRequest | None = None, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    refresh_settings(); require_superadmin(x_r20_session)
    from r20_backend.qq_bind import start_openid_capture
    app_id = payload.app_id if payload else None
    secret = payload.client_secret if payload else None
    timeout = payload.timeout if payload else 60
    try:
        res = start_openid_capture(app_id=app_id, client_secret=secret, timeout=timeout)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"启动 QQ OpenID 监听网关失败：{exc}") from exc
    audit_record("qq.capture_openid.start", "success", {"capture_id": res.get("capture_id"), "app_id": res.get("app_id")})
    return res


@app.get("/api/v1/admin/notifications/qq/capture-openid/{capture_id}")
def qq_capture_openid_poll(capture_id: str, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    refresh_settings(); require_superadmin(x_r20_session)
    from r20_backend.qq_bind import poll_openid_capture
    res = poll_openid_capture(capture_id)
    if res.get("status") == "captured":
        audit_record("qq.capture_openid.complete", "success", {"capture_id": capture_id, "openid": res.get("openid")})
    return res


@app.post("/api/v1/admin/notifications/qq/bind/start")
def qq_bind_start(x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    refresh_settings(); require_superadmin(x_r20_session)
    from r20_backend.qq_bind import create_bind_task
    try:
        task = create_bind_task()
    except Exception as exc:
        audit_record("qq.bind.start", "failed", {"error": str(exc)[:200]})
        raise HTTPException(status_code=502, detail=f"QQ 绑定任务创建失败：{exc}")
    qr_data_uri = ""
    try:
        import segno
        qr_data_uri = segno.make(task["connect_url"], error="M").png_data_uri(scale=6, border=2)
    except Exception:
        pass
    audit_record("qq.bind.start", "success", {"task_id": task["task_id"]})
    return {"task_id": task["task_id"], "qr_data_uri": qr_data_uri, "connect_url": task["connect_url"], "expires_in": task["expires_in"]}


@app.get("/api/v1/admin/notifications/qq/bind/{task_id}")
def qq_bind_poll(task_id: str, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    refresh_settings(); require_superadmin(x_r20_session)
    from r20_backend.qq_bind import poll_bind_task
    try:
        result = poll_bind_task(task_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=410, detail=str(exc))
    if result["status"] == "bound" or result["status"] == "awaiting_message":
        audit_record("qq.bind.complete", "success", {"app_id": result["app_id"], "status": result["status"], "openid_present": bool(result.get("openid"))})
    return result



@app.get("/api/v1/admin/notifications/schedule")
def notification_schedule(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    schedule = load_schedule()
    return {
        **schedule,
        "event_notifications": "开仓、平仓与风险事件实时推送，不受每日简报时间限制",
        "restart_note": "保存后调度器将在 60 秒内读取新时间，无需重启。",
    }


@app.put("/api/v1/admin/notifications/schedule")
def update_notification_schedule(payload: NotificationScheduleUpdate, x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    normalized: list[str] = []
    for value in payload.briefing_times:
        value = value.strip()
        try:
            parsed = time.strptime(value, "%H:%M")
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"无效时间：{value}；必须使用 HH:MM 24 小时格式") from exc
        canonical = f"{parsed.tm_hour:02d}:{parsed.tm_min:02d}"
        if canonical not in normalized:
            normalized.append(canonical)
    normalized.sort()
    schedule = load_schedule()
    schedule["briefing_times"] = normalized
    save_schedule(schedule)
    audit_record("notifications.schedule", "success", {"briefing_times": normalized, "timezone": "Asia/Shanghai"})
    return {**schedule, "saved": True, "restart_note": "调度器将在 60 秒内读取新时间。"}


@app.get("/api/v1/admin/backups/simple")
def simple_backup_config(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_header(x_r20_admin_token)
    jobs = list_backup_jobs(); job = next((x for x in jobs if x.get("id") == "nightly-default"), jobs[0] if jobs else None)
    if not job: raise HTTPException(status_code=404, detail="主灾备任务不存在")
    target = next((x for x in job.get("targets", []) if x.get("enabled")), None)
    if not target: target = next((x for x in job.get("targets", []) if x.get("type") == "local"), None)
    target_type = str((target or {}).get("type") or "local"); auth_mode = str((target or {}).get("auth_mode") or "")
    legacy_bypy = target_type == "baidu" and auth_mode != "oauth"
    destination = "baidu_oauth" if target_type == "baidu" and not legacy_bypy else target_type if target_type in {"local","s3","oss","webdav"} else "local"
    validation = validate_backup_job(job); latest = None
    manifests_dir = ROOT / "backups" / "manifests"
    for path in sorted(manifests_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:30] if manifests_dir.exists() else []:
        try:
            item=json.loads(path.read_text(encoding="utf-8"))
            if item.get("job_id")==job["id"]: latest=item; break
        except (OSError,json.JSONDecodeError): pass
    return {"job_id":job["id"],"target":target or {},"enabled":job["enabled"],"schedule_time":job["schedule_times"][0],"destination":destination,"retention":int((target or {}).get("retention") or 3),"legacy_bypy":legacy_bypy,"migration_note":"当前为旧版 ByPy 配置，请选择新的保存位置后保存完成迁移" if legacy_bypy else "","configured":bool((target or {}).get("credential_status",{}).get("configured")) if target else destination=="local","validation":validation,"latest":latest,"advanced_preserved":True}


@app.put("/api/v1/admin/backups/simple")
def update_simple_backup(payload: SimpleBackupUpdateRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor=require_superadmin(x_r20_session); jobs=list_backup_jobs(); job=next((x for x in jobs if x.get("id")=="nightly-default"), jobs[0] if jobs else None)
    if not job: raise HTTPException(status_code=404,detail="主灾备任务不存在")
    wanted_type="baidu" if payload.destination=="baidu_oauth" else payload.destination
    existing=next((x for x in job.get("targets",[]) if x.get("type")==wanted_type and (wanted_type!="baidu" or x.get("auth_mode")=="oauth")),None)
    if not existing:
        target_id=f"{wanted_type}-{__import__('uuid').uuid4().hex[:10]}"; existing={"id":target_id,"type":wanted_type,"label":{"local":"本地归档","s3":"S3存储","oss":"阿里云OSS","webdav":"WebDAV/OpenList","baidu":"百度网盘"}[wanted_type],"credential_ref":f"backup:{target_id}","enabled":False,"remote_path":"R20_Backups","path":"backups/local","retention":3,"retries":3,"auth_mode":"oauth" if wanted_type=="baidu" else "native"}; job.setdefault("targets",[]).append(existing)
    for target in job.get("targets",[]): target["enabled"] = target is existing
    existing["retention"] = payload.retention if wanted_type=="local" else 0
    if wanted_type in {"s3","oss","webdav"}: existing["endpoint"] = payload.endpoint.strip()
    if wanted_type in {"s3","oss"}: existing["bucket"] = payload.bucket.strip()
    if wanted_type=="baidu": existing["auth_mode"]="oauth"
    if payload.credentials: save_backup_credentials(existing["credential_ref"], payload.credentials)
    job["enabled"]=payload.enabled; job["schedule_times"]=[payload.schedule_time]
    try: saved=update_backup_job(job["id"],job)
    except ValueError as exc: raise HTTPException(status_code=400,detail=str(exc)) from exc
    audit_record("backup.simple.update","success",{"actor":actor["username"],"destination":payload.destination,"enabled":payload.enabled})
    return {"saved":True,"job":saved}


@app.post("/api/v1/admin/backups/simple/test")
def test_simple_backup(payload: SimpleBackupUpdateRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    require_superadmin(x_r20_session)
    if payload.destination == "local":
        directory=(ROOT/"backups"/"local").resolve(); directory.mkdir(parents=True,exist_ok=True)
        if not directory.is_relative_to((ROOT/"backups").resolve()): raise HTTPException(status_code=400,detail="本地灾备目录无效")
        return {"status":"ready","sent":False,"detail":"本地目录可写；未生成或上传归档"}
    wanted_type="baidu" if payload.destination=="baidu_oauth" else payload.destination
    target={"type":wanted_type,"endpoint":payload.endpoint.strip(),"bucket":payload.bucket.strip(),"auth_mode":"oauth" if wanted_type=="baidu" else "native"}
    required={"s3":{"access_key_id","secret_access_key"},"oss":{"access_key_id","secret_access_key"},"webdav":set(),"baidu":{"app_key","app_secret","refresh_token"}}[wanted_type]
    missing=sorted(key for key in required if not payload.credentials.get(key))
    if missing: raise HTTPException(status_code=400,detail=f"连接信息不完整：{', '.join(missing)}")
    if wanted_type in {"s3","oss","webdav"}:
        try: target["endpoint"]=__import__("r20_backend.net_security",fromlist=["validate_outbound_url"]).validate_outbound_url(target["endpoint"])
        except ValueError as exc: raise HTTPException(status_code=400,detail=str(exc)) from exc
    if wanted_type in {"s3","oss"} and not target["bucket"]: raise HTTPException(status_code=400,detail="Bucket 不能为空")
    # Intentionally no upload and no OAuth token exchange: this endpoint validates
    # configuration and safe reachability only, never creates remote objects.
    return {"status":"ready","sent":False,"detail":"配置格式与目标地址校验通过；未上传任何文件","destination":payload.destination}


@app.get("/api/v1/admin/backup-target-types")
def backup_target_types(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_header(x_r20_admin_token)
    return {"target_types": [
        {"type":"local","label":"本地归档","auth":"none","description":"项目 backups/ 内滚动保留"},
        {"type":"baidu","label":"百度网盘","auth":"oauth","description":"仅支持官方 OAuth，新配置不再提供 ByPy"},
        {"type":"s3","label":"S3 兼容存储","auth":"access-key","description":"AWS S3、R2、MinIO、COS 等 S3 兼容端点"},
        {"type":"oss","label":"阿里云 OSS","auth":"access-key","description":"官方 oss2 SDK"},
        {"type":"webdav","label":"WebDAV / NAS / OpenList","auth":"basic","description":"标准 WebDAV PUT/MKCOL"},
        {"type":"aliyundrive","label":"阿里云盘","auth":"webdav-or-oauth","description":"推荐开放平台或 OpenList WebDAV 桥接"},
        {"type":"quark","label":"夸克网盘（实验性）","auth":"webdav-or-experimental-oauth","description":"官方开放平台仍在内测，推荐 OpenList WebDAV 桥接"},
    ]}


@app.put("/api/v1/admin/backup-credentials")
def update_backup_credentials(payload: BackupCredentialUpdateRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try: status = save_backup_credentials(payload.credential_ref, payload.credentials)
    except ValueError as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("backup.credentials.update", "success", {"actor": actor["username"], "credential_ref": payload.credential_ref, "fields": status["fields"]})
    return {"saved": True, "credential_ref": payload.credential_ref, "status": status}


@app.get("/api/v1/admin/backup-jobs")
def backup_jobs_api(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_header(x_r20_admin_token)
    manifests_dir = ROOT / "backups" / "manifests"
    manifests = []
    for path in sorted(manifests_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:50] if manifests_dir.exists() else []:
        try:
            item = json.loads(path.read_text(encoding="utf-8")); item["manifest_file"] = path.name; manifests.append(item)
        except (OSError, json.JSONDecodeError): pass
    jobs = list_backup_jobs()
    for job in jobs:
        for target in job.get("targets", []): target["credential_status"] = backup_credential_status(str(target.get("credential_ref") or ""))
    return {"jobs": jobs, "validations": {job["id"]: validate_backup_job(job) for job in jobs}, "recent_manifests": manifests, "timezone": "Asia/Shanghai", "limits": {"maximum_jobs": 12}}


@app.post("/api/v1/admin/backup-jobs")
def create_backup_job_api(payload: BackupJobCreateRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try: job = create_backup_job(payload.name, payload.source_id)
    except ValueError as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("backup.job.create", "success", {"actor": actor["username"], "job_id": job["id"]})
    return {"job": job}


@app.put("/api/v1/admin/backup-jobs/{job_id}")
def update_backup_job_api(job_id: str, payload: BackupJobUpdateRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try: job = update_backup_job(job_id, payload.job)
    except ValueError as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("backup.job.update", "success", {"actor": actor["username"], "job_id": job_id, "enabled": job["enabled"]})
    return {"job": job, "validation": validate_backup_job(job)}


@app.delete("/api/v1/admin/backup-jobs/{job_id}")
def delete_backup_job_api(job_id: str, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try: delete_backup_job(job_id)
    except ValueError as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit_record("backup.job.delete", "success", {"actor": actor["username"], "job_id": job_id})
    return {"deleted": True}


@app.post("/api/v1/admin/backup-jobs/validate")
def validate_backup_job_api(payload: BackupJobUpdateRequest, x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_header(x_r20_admin_token)
    return validate_backup_job(payload.job)


@app.post("/api/v1/admin/backup-jobs/{job_id}/run")
def run_backup_job_api(job_id: str, payload: BackupJobRunRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    if payload.confirmation.strip().upper() != f"BACKUP {job_id}".upper():
        raise HTTPException(status_code=400, detail=f"确认短语必须精确为：BACKUP {job_id}")
    try: get_backup_job(job_id)
    except ValueError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc
    script = SCRIPTS_DIR / "nightly_backup_and_clean.py"
    result = subprocess.run([sys.executable, str(script), "--job-id", job_id], cwd=ROOT, text=True, capture_output=True, timeout=1800)
    BACKUP_LOG_FILE.parent.mkdir(exist_ok=True); BACKUP_LOG_FILE.write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
    audit_record("backup.job.run", "success" if result.returncode == 0 else "failed", {"actor": actor["username"], "job_id": job_id, "returncode": result.returncode})
    if result.returncode: raise HTTPException(status_code=502, detail=f"灾备任务失败：{result.stderr[-800:] or result.stdout[-800:]}")
    return {"completed": True, "output": result.stdout[-4000:]}


@app.get("/api/v1/admin/backup-jobs/{job_id}/export")
def export_backup_job_api(job_id: str, x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_header(x_r20_admin_token)
    try: return export_backup_job(job_id)
    except ValueError as exc: raise HTTPException(status_code=404, detail=str(exc)) from exc


@app.post("/api/v1/admin/backup-jobs/import")
def import_backup_job_api(payload: BackupJobImportRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    try: job = import_backup_job(payload.payload, payload.name_override)
    except ValueError as exc: raise HTTPException(status_code=400, detail=str(exc)) from exc
    audit_record("backup.job.import", "success", {"actor": actor["username"], "job_id": job["id"]})
    return {"job": job}


@app.post("/api/v1/admin/backup-jobs/verify")
def verify_backup_archive_api(payload: BackupVerifyRequest, x_r20_session: str | None = Header(default=None, alias="X-R20-Session")) -> dict[str, Any]:
    actor = require_superadmin(x_r20_session)
    candidate = (ROOT / payload.archive_path).resolve()
    if not candidate.is_relative_to((ROOT / "backups").resolve()): raise HTTPException(status_code=400, detail="只能验证项目 backups/ 目录内的归档")
    from scripts.backup_runtime import verify_archive
    try: result = verify_archive(candidate, payload.expected_sha256, payload.key_env)
    except Exception as exc: raise HTTPException(status_code=409, detail=str(exc)) from exc
    audit_record("backup.archive.verify", "success", {"actor": actor["username"], "archive": str(candidate.relative_to(ROOT)), "members": result["members"]})
    return result


@app.put("/api/v1/admin/backups/methods")
def update_backup_methods(payload: BackupMethodsUpdate, x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    methods = {
        "baidu": {"enabled": payload.baidu_enabled, "retention": 0},
        "local": {"enabled": payload.local_enabled, "retention": payload.local_retention},
        "sqlite": {"enabled": payload.sqlite_enabled, "retention": payload.sqlite_retention},
    }
    if not any(item["enabled"] for item in methods.values()):
        raise HTTPException(status_code=400, detail="至少启用一种灾备方式")
    save_backup_methods(methods)
    audit_record("backup.methods.update", "success", {key: value["enabled"] for key, value in methods.items()})
    return {"saved": True, "methods": load_backup_methods()}


@app.get("/api/v1/admin/backups")
def backup_status(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    backups_dir = ROOT / "backups"
    archive_paths = list(backups_dir.glob("*.tar.gz")) + list((backups_dir / "local").glob("*.tar.gz")) if backups_dir.exists() else []
    local_archives = [{"name": str(item.relative_to(backups_dir)), "bytes": item.stat().st_size, "mtime": int(item.stat().st_mtime)} for item in archive_paths]
    sqlite_snapshots = [{"name": item.name, "bytes": item.stat().st_size, "mtime": int(item.stat().st_mtime)} for item in (backups_dir / "sqlite").glob("*.db")] if (backups_dir / "sqlite").exists() else []
    return {
        "schedule": "每天北京时间 02:00，由 Gateway Scheduler 执行全部已启用灾备方式",
        "script": str(SCRIPTS_DIR / "nightly_backup_and_clean.py"),
        "methods": load_backup_methods(),
        "jobs": list_backup_jobs(),
        "local_archives": sorted(local_archives, key=lambda item: item["mtime"], reverse=True),
        "sqlite_snapshots": sorted(sqlite_snapshots, key=lambda item: item["mtime"], reverse=True),
        "last_log": BACKUP_LOG_FILE.read_text(encoding="utf-8")[-4000:] if BACKUP_LOG_FILE.exists() else "尚无后台手动灾备日志",
    }


@app.post("/api/v1/admin/backups/run")
def run_backup(payload: BackupRequest, x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    refresh_settings()
    require_admin_header(x_r20_admin_token)
    if payload.confirmation.strip().upper() != "BACKUP R20":
        raise HTTPException(status_code=400, detail="确认短语必须精确为：BACKUP R20")
    script = SCRIPTS_DIR / "nightly_backup_and_clean.py"
    result = subprocess.run([sys.executable, str(script)], cwd=ROOT, text=True, capture_output=True, timeout=600)
    BACKUP_LOG_FILE.parent.mkdir(exist_ok=True)
    BACKUP_LOG_FILE.write_text(result.stdout + "\n" + result.stderr, encoding="utf-8")
    if result.returncode:
        audit_record("backup.run", "failed", {"returncode": result.returncode})
        raise HTTPException(status_code=502, detail=f"灾备任务失败：{result.stderr[-800:] or result.stdout[-800:]}")
    audit_record("backup.run", "success", {})
    return {"completed": True, "output": result.stdout[-2500:]}


@app.get("/health", include_in_schema=False)
@app.get("/api/v1/health")
def health() -> dict[str, Any]:
    return {
        "service": "r20-standalone-backend",
        "version": "6.2.1",
        "status": "ok",
        "timestamp": int(time.time()),
        "credentials": {
            "okx_configured": bool(settings.okx_api_key and settings.okx_secret_key and settings.okx_passphrase),
            "llm_configured": bool(settings.llm_api_key),
            "simulated_trading": settings.okx_simulated,
        },
    }


@app.get("/api/v1/status")
def status() -> dict[str, Any]:
    return {
        "version": "6.2.1",
        "mode": "read_only_control_plane",
        "scripts": [
            script_state("ai_factor_trader.py"),
            script_state("ai_brain_trader.py"),
            script_state("daemon_web_sync.py"),
            script_state("self_improvement_engine.py"),
            script_state("nightly_backup_and_clean.py"),
        ],
        "last_decisions": read_json("ai_brain_decisions.json", {}),
        "position_trackers": read_json("position_trackers.json", {}),
    }


@app.get("/api/v1/cache/{resource}")
def cache(resource: str) -> JSONResponse:
    allowed = {
        "decisions": "ai_brain_decisions.json",
        "factors": "factor_library_snapshot.json",
        "ledger": "trading_ledger.json",
        "sentiment": "news_sentiment.json",
        "self-improvement": "self_improvement_report.json",
    }
    filename = allowed.get(resource)
    if not filename:
        raise HTTPException(status_code=404, detail="unknown cache resource")
    return JSONResponse(read_json(filename, {} if resource != "ledger" else []))


@app.get("/api/v1/market/{inst_id}")
def market(inst_id: str) -> dict[str, Any]:
    if not inst_id.endswith("-SWAP"):
        raise HTTPException(status_code=400, detail="only SWAP instrument ids are accepted")
    try:
        ticker = okx.ticker(inst_id)
        return {"instId": inst_id, "ticker": ticker[0] if ticker else {}, "source": "OKX REST"}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"OKX market request failed: {exc}") from exc


@app.get("/api/v1/account/positions")
def positions(x_r20_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin_header(x_r20_admin_token)
    if not settings.okx_api_key:
        raise HTTPException(status_code=503, detail="OKX credentials are not configured in .env")
    try:
        return {"positions": okx.positions(), "source": "OKX REST"}
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"OKX account request failed: {exc}") from exc


# LEGACY read-only UI only (Stage 3): public dashboard + relative-path API at /.
# Prefer keel.api.app for new deployments. Do not treat this mount as the primary API.
# Admin and /api/v1 routes above are evaluated before this catch-all mount.
from dashboard.app import app as dashboard_app
app.mount("/", dashboard_app)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host=settings.host, port=settings.port)
