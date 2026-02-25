import json
import os
import re
import subprocess
import sys
import time
import uuid
import logging
import base64
from collections import defaultdict
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Dict

from dotenv import load_dotenv
load_dotenv() 

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import JSONResponse

from pydantic import BaseModel
from starlette.middleware.base import BaseHTTPMiddleware
import uvicorn

# ----------------------------
# CONSTANTS & CONFIG
# ----------------------------
IS_WINDOWS = sys.platform.startswith("win")
ACTIONS_PATH = Path(os.getenv("GANTRY_ACTIONS_PATH", "actions.json"))

API_TOKEN = os.getenv("GANTRY_TOKEN", "")
IP_ALLOWLIST = set(filter(None, (os.getenv("GANTRY_IP_ALLOWLIST", "")).split(",")))

# --- RATE LIMIT CONFIG ---
RATE_LIMIT_ENABLED = os.getenv("GANTRY_RATE_LIMIT_ENABLED", "true").strip().lower() in ("1", "true", "yes")
RATE_LIMIT_BYPASS_IPS = set(filter(None, (os.getenv("GANTRY_RATE_LIMIT_BYPASS_IPS", "")).split(",")))

PLACEHOLDER_RE = re.compile(r"\{\{([a-zA-Z0-9_]+)\}\}")

# ----------------------------
# RATE LIMITER STATE (In-Memory)
# ----------------------------
# Structure: { "action_name": { "ip_address": [timestamp1, timestamp2] } }
RATE_LIMIT_STORE: Dict[str, Dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))

# ----------------------------
# LOGGING SETUP
# ----------------------------
LOG_LEVEL = os.getenv("GANTRY_LOG_LEVEL", "INFO").upper()
LOG_FILE = os.getenv("GANTRY_LOG_FILE", "logs/gantry.log")
LOG_MAX_BYTES = int(os.getenv("GANTRY_LOG_MAX_BYTES", str(2 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.getenv("GANTRY_LOG_BACKUP_COUNT", "5"))

logger = logging.getLogger("gantry")
logger.setLevel(getattr(logging, LOG_LEVEL, logging.INFO))
logger.propagate = False

_fmt = logging.Formatter(
    fmt="%(asctime)s | %(levelname)s | %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

# Console handler
_ch = logging.StreamHandler(sys.stdout)
_ch.setFormatter(_fmt)
logger.addHandler(_ch)

# Rotating file handler
if LOG_FILE.strip():
    log_path = Path(LOG_FILE)
    if log_path.parent:
        log_path.parent.mkdir(parents=True, exist_ok=True)
    
    _fh = RotatingFileHandler(
        LOG_FILE, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT, encoding="utf-8"
    )
    _fh.setFormatter(_fmt)
    logger.addHandler(_fh)

# ----------------------------
# LIFESPAN (Startup/Shutdown)
# ----------------------------
@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(
        f"BOOT | Starting Gantry | pid={os.getpid()} | actions_path={ACTIONS_PATH.resolve()} "
        f"| token={'set' if bool(API_TOKEN) else 'unset'} | ip_allowlist={sorted(IP_ALLOWLIST) if IP_ALLOWLIST else 'none'} "
        f"| rate_limits={'enabled' if RATE_LIMIT_ENABLED else 'disabled'}"
    )
    yield
    logger.info(f"SHUTDOWN | stopping Gantry | pid={os.getpid()}")

app = FastAPI(lifespan=lifespan)

# ----------------------------
# UTILS
# ----------------------------
def load_actions() -> Dict[str, Any]:
    if not ACTIONS_PATH.exists():
        raise RuntimeError(f"actions file not found: {ACTIONS_PATH}")
    data = json.loads(ACTIONS_PATH.read_text(encoding="utf-8"))
    actions = (data.get("actions") or {})
    if not isinstance(actions, dict):
        raise RuntimeError("actions.json: 'actions' must be an object")
    return actions

def client_ip(req: Request) -> str:
    xff = req.headers.get("x-forwarded-for", "")
    if xff:
        return xff.split(",")[0].strip()
    xri = req.headers.get("x-real-ip", "")
    if xri:
        return xri.strip()
    host = req.client.host if req.client else ""
    return host or ""

def require_auth(req: Request, request_id: str) -> None:
    ip = client_ip(req)

    if IP_ALLOWLIST:
        if ip not in IP_ALLOWLIST:
            logger.warning(f"{request_id} | AUTH | 403 | ip_not_allowed ip={ip}")
            raise HTTPException(status_code=403, detail=f"IP not allowed: {ip}")

    if API_TOKEN:
        token = req.headers.get("X-Token", "")
        if token != API_TOKEN:
            logger.warning(f"{request_id} | AUTH | 401 | bad_token ip={ip}")
            raise HTTPException(status_code=401, detail="Unauthorized")

def check_rate_limit(action_name: str, action_config: dict, ip: str, request_id: str) -> None:
    """Enforces per-action rate limiting based on actions.json configuration."""
    if not RATE_LIMIT_ENABLED or ip in RATE_LIMIT_BYPASS_IPS:
        return

    rl_config = action_config.get("rate_limit")
    if not isinstance(rl_config, dict):
        return  # No rate limit defined for this action

    max_req = int(rl_config.get("max_requests", 0))
    window = int(rl_config.get("window_seconds", 0))

    if max_req <= 0 or window <= 0:
        return

    now = time.time()
    
    # Get the history array for this specific action and IP
    history = RATE_LIMIT_STORE[action_name][ip]

    # Clean up memory: remove timestamps older than the window
    history = [ts for ts in history if now - ts < window]

    if len(history) >= max_req:
        logger.warning(f"{request_id} | RATELIMIT | 429 | blocked action={action_name} ip={ip} ({max_req} req / {window}s)")
        raise HTTPException(status_code=429, detail="Too Many Requests. Rate limit exceeded.")

    # Record the new request
    history.append(now)
    RATE_LIMIT_STORE[action_name][ip] = history


class RunBody(BaseModel):
    params: Dict[str, Any] = {}
    dry_run: bool = False

def render_args(args: list[str], params: Dict[str, Any]) -> list[str]:
    """Replaces {{key}} placeholders with values from params."""
    rendered: list[str] = []
    for a in args:
        def repl(m: re.Match) -> str:
            key = m.group(1)
            if key not in params:
                raise HTTPException(status_code=400, detail=f"Missing param: {key}")
            return str(params[key])
        rendered.append(PLACEHOLDER_RE.sub(repl, a))
    return rendered

# ----------------------------
# REQUEST LOGGING MIDDLEWARE
# ----------------------------
class RequestLoggerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        request_id = request.headers.get("X-Request-Id") or str(uuid.uuid4())[:8]
        request.state.request_id = request_id

        ip = client_ip(request)
        path = request.url.path
        method = request.method

        start = time.perf_counter()
        try:
            response = await call_next(request)
            ms = int((time.perf_counter() - start) * 1000)
            
            # Don't log 429s as normal requests, they are handled heavily by the logic above
            if response.status_code != 429:
                logger.info(f"{request_id} | REQ | {method} {path} | {response.status_code} | {ms}ms | ip={ip}")
            return response
        except Exception:
            ms = int((time.perf_counter() - start) * 1000)
            logger.exception(f"{request_id} | REQ | {method} {path} | EXCEPTION | {ms}ms | ip={ip}")
            raise

app.add_middleware(RequestLoggerMiddleware)

@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    rid = getattr(request.state, "request_id", "unknown")
    logger.error(f"{rid} | UNHANDLED 500 | {request.method} {request.url.path} | err={type(exc).__name__}: {exc}", exc_info=True)
    return JSONResponse(status_code=500, content={"ok": False, "detail": str(exc)})

# ----------------------------
# ENDPOINTS
# ----------------------------
@app.get("/health")
def health(req: Request):
    rid = getattr(req.state, "request_id", "--------")
    logger.debug(f"{rid} | HEALTH")
    return {"ok": True}

@app.get("/actions")
def list_actions(req: Request):
    rid = getattr(req.state, "request_id", "--------")
    actions = load_actions()
    logger.info(f"{rid} | ACTIONS | count={len(actions)}")
    return {"ok": True, "actions": sorted(actions.keys())}

@app.post("/run/{action_name}")
def run_action(action_name: str, body: RunBody, req: Request):
    rid = getattr(req.state, "request_id", "--------")
    ip = client_ip(req)

    # 1. Load action config 
    actions = load_actions()
    action = actions.get(action_name)
    if not isinstance(action, dict):
        logger.warning(f"{rid} | RUN | 404 | unknown_action={action_name} ip={ip}")
        raise HTTPException(status_code=404, detail=f"Unknown action: {action_name}")

    # 2. Decode Base64 params immediately 
    decode_list = action.get("decode_b64_params", [])
    for k in decode_list:
        if k in body.params and isinstance(body.params[k], str):
            try:
                b64_val = body.params[k]
                # Fix padding if necessary
                b64_val += "=" * ((4 - len(b64_val) % 4) % 4)
                body.params[k] = base64.b64decode(b64_val).decode('utf-8')
            except Exception:
                # If it's not actually base64, just leave it as is
                pass

    # 3. Check for "detected" keyword 
    if body.params.get("ip") == "detected":
        body.params["ip"] = ip

    # 4. Auth & Rate Limit checks
    require_auth(req, rid)
    check_rate_limit(action_name, action, ip, rid)

    exe = action.get("exe")
    cwd = action.get("cwd")
    args = action.get("args") or []
    timeout_s = int(action.get("timeout_seconds", 300))
    capture = bool(action.get("capture_output", True))
    is_background = bool(action.get("background", False)) 
    
    
    stealth = bool(action.get("stealth", False))

    # 5. Render final arguments for the script
    final_args = render_args(args, body.params)
    cmd = [exe] + final_args

    if body.dry_run:
        logger.info(f"{rid} | RUN | dry_run action={action_name} ip={ip} cmd={cmd} cwd={cwd}")
        return {"ok": True, "dry_run": True, "cmd": cmd, "cwd": cwd, "timeout_seconds": timeout_s}

    logger.info(f"{rid} | RUN | start action={action_name} ip={ip} cmd={cmd} cwd={cwd} timeout={timeout_s}s capture={capture} bg={is_background}")

    t0 = time.perf_counter()

    # ----------------------------
    # BACKGROUND EXECUTION
    # ----------------------------
    if is_background:
        try:
            kwargs = {"cwd": cwd or None, "shell": False}
            if IS_WINDOWS:
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW | 0x00000008 
            else:
                kwargs["start_new_session"] = True

            subprocess.Popen(cmd, **kwargs)
            return {
                "ok": True,
                "action": action_name,
                "background": True,
                "message": "Task started in background",
                "request_id": rid
            }
        except Exception as e:
            logger.exception(f"{rid} | RUN | failed_bg action={action_name}")
            raise HTTPException(status_code=500, detail=f"Failed: {e}")


    # ----------------------------
    # BLOCKING EXECUTION
    # ----------------------------
    try:
        kwargs = {
            "cwd": cwd or None,
            "text": True,
            "capture_output": capture,
            "timeout": timeout_s,
            "shell": False,
        }
        if IS_WINDOWS:
            kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW

        p = subprocess.run(cmd, **kwargs)
        ms = int((time.perf_counter() - t0) * 1000)
        logger.info(f"{rid} | RUN | done action={action_name} rc={p.returncode} {ms}ms")
    except subprocess.TimeoutExpired:
        raise HTTPException(status_code=504, detail="Timed out")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed: {e}")

    out = (p.stdout or "")[-8000:] if capture else ""
    err = (p.stderr or "")[-8000:] if capture else ""

    # Stealth Return Block
    if stealth:
        encoded_out = base64.b64encode(out.encode('utf-8')).decode('utf-8') if out else ""
        return {
            "ok": (p.returncode == 0),
            "data": encoded_out
        }
    
    return {
        "ok": (p.returncode == 0),
        "action": action_name,
        "returncode": p.returncode,
        "stdout_tail": out,
        "stderr_tail": err,
        "cmd": cmd,
        "request_id": rid,
    }

# ----------------------------
# SELF-HEALING PORT CLEANUP (Cross-Platform)
# ----------------------------
def nuke_port(port: int):
    my_pid = str(os.getpid())
    try:
        if IS_WINDOWS:
            output = subprocess.check_output(f'netstat -ano | findstr ":{port}" | findstr LISTENING', shell=True).decode()
            pids = set(re.findall(r'\s(\d+)\s*$', output, re.MULTILINE))
            for pid in pids:
                if pid == my_pid or pid == "0":
                    continue
                logger.info(f"CLEANUP | Port {port} is blocked by PID {pid}. Killing...")
                subprocess.run(f'taskkill /F /T /PID {pid}', shell=True, capture_output=True)
        else:
            output = subprocess.check_output(f'lsof -t -i:{port}', shell=True).decode()
            pids = set(output.strip().split('\n'))
            for pid in pids:
                if not pid or pid == my_pid:
                    continue
                logger.info(f"CLEANUP | Port {port} is blocked by PID {pid}. Killing...")
                subprocess.run(f'kill -9 {pid}', shell=True, capture_output=True)
        time.sleep(1)
    except Exception:
        pass

if __name__ == "__main__":
    PORT = int(os.getenv("GANTRY_PORT", 8787))
    nuke_port(PORT)
    logger.info(f"LAUNCH | Starting Gantry on port {PORT}")
    
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info", access_log=False)