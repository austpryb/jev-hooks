"""Minimal TypeSafe System One client for hooks. No SDK, no dependencies.

Every hook fails OPEN: if the key is missing, the network is down, or Jev
returns 429/529, ask() returns None and the hook exits 0 having done nothing.
A judgment service must never be the reason a coding session stalls.

API shape (docs.typesafe.ai/api, 2026-09-19):
  POST https://api.typesafe.ai/v1/systemone  Authorization: Bearer <key>
  {"state": str|obj|arr, "model": "jev-latest", "questions": {id: Question}}
  Noul   -> {"type":"noul","noul": 0..1}
  Choice -> {"type":"choice","choice": str, "probabilities": {...}, "confidence": 0..1}
  Score  -> {"type":"score","score": float, "probabilities": {...}, "confidence": 0..1}
"""
import json, os, sys, time, urllib.request, urllib.error

BASE = os.environ.get("TYPESAFE_BASE_URL", "https://api.typesafe.ai").rstrip("/")
MODEL = os.environ.get("JEV_HOOKS_MODEL", "jev-latest")
TIMEOUT = float(os.environ.get("JEV_HOOKS_TIMEOUT", "6"))
LOG = os.environ.get("JEV_HOOKS_LOG")  # path; append one JSON line per call when set


def noul(instructions, true=None, false=None):
    q = {"type": "noul", "instructions": instructions}
    if true or false:
        q["criteria"] = {k: v for k, v in (("true", true), ("false", false)) if v}
    return q


def choice(instructions, criteria):
    return {"type": "choice", "instructions": instructions, "criteria": criteria}


def score(instructions, levels):
    return {"type": "score", "instructions": instructions, "criteria": levels}


def ask(state, questions, retries=1):
    """Returns {id: answer} or None. Never raises."""
    key = os.environ.get("TYPESAFE_API_KEY")
    if not key or not questions:
        return None
    body = json.dumps({"state": state, "model": MODEL, "questions": questions}).encode()
    req = urllib.request.Request(f"{BASE}/v1/systemone", data=body, method="POST",
                                 headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"})
    t0 = time.time()
    for attempt in range(retries + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                out = json.loads(r.read())
                _log(len(body), out.get("usage"), time.time() - t0, "ok")
                return out.get("answers") or None
        except urllib.error.HTTPError as e:
            if e.code in (429, 529) and attempt < retries:
                time.sleep(_retry_after(e, 0.4 * (attempt + 1)))
                continue
            _log(len(body), None, time.time() - t0, f"http {e.code}")
            return None
        except Exception as e:  # timeouts, DNS, JSON — all fail open
            _log(len(body), None, time.time() - t0, type(e).__name__)
            return None
    return None


def _retry_after(e, default):
    """Honour Retry-After (seconds) or retry-after-ms on a 429/529, capped at 2s so
    the hook stays inside its harness timeout. Anything unparseable -> default."""
    try:
        h = e.headers.get("retry-after-ms")
        if h:
            return min(2.0, max(0.0, float(h) / 1000))
        h = e.headers.get("Retry-After")
        if h:
            return min(2.0, max(0.0, float(h)))
    except Exception:
        pass
    return default


def safe_id(s):
    """A session id is used as a file name; keep only [A-Za-z0-9_-]."""
    s = "".join(ch for ch in str(s or "") if ch.isalnum() or ch in "_-")
    return s or "unknown"


def prune(dirpath, days=14):
    """Best-effort: delete files older than `days` in dirpath."""
    try:
        cutoff = time.time() - days * 86400
        for name in os.listdir(dirpath):
            fp = os.path.join(dirpath, name)
            if os.path.isfile(fp) and os.path.getmtime(fp) < cutoff:
                os.unlink(fp)
    except Exception:
        pass


def _log(nbytes, usage, secs, outcome):
    if not LOG:
        return
    try:
        with open(LOG, "a") as f:
            f.write(json.dumps({"t": time.time(), "bytes": nbytes, "usage": usage, "secs": round(secs, 3), "outcome": outcome}) + "\n")
    except Exception:
        pass


def read_stdin():
    try:
        return json.load(sys.stdin)
    except Exception:
        return {}
