"""
AutoBlog Engine — Status Tracker

Writes real-time pipeline task status to a JSON file.
The monitor dashboard reads this file to show live progress.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Literal

import config

StatusState = Literal["idle", "running", "error", "done"]

_STATUS_FILE = config.VAULT_LOGS / "pipeline_status.json"
_lock = threading.Lock()

# Canonical step names (order matters)
PIPELINE_STEPS = [
    "Trend Research",
    "Keyword Research",
    "Content Writing",
    "Image Handling",
    "Publishing",
    "Health Check",
]


def _write(data: dict):
    """Atomically write status JSON."""
    with _lock:
        _STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2, default=str), encoding="utf-8")
        tmp.replace(_STATUS_FILE)


def _read() -> dict:
    """Read current status JSON (returns defaults if missing)."""
    try:
        if _STATUS_FILE.exists():
            return json.loads(_STATUS_FILE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return _default()


def _default() -> dict:
    return {
        "status": "idle",
        "current_task": None,
        "current_step": 0,
        "total_steps": len(PIPELINE_STEPS),
        "progress_pct": 0,
        "run_type": None,
        "started_at": None,
        "last_updated": datetime.now().isoformat(),
        "counters": {
            "topics_found": 0,
            "keywords_found": 0,
            "drafts_created": 0,
            "posts_published": 0,
        },
        "log_lines": [],
        "error": None,
    }


# ── Public API ────────────────────────────────────────────────────────────────

class StatusTracker:
    """
    Drop-in status tracker. Use as a context manager for the full pipeline,
    or call step_start/step_done manually.

    Example:
        tracker = StatusTracker("full")
        tracker.pipeline_start()

        tracker.step_start("Trend Research", step=1)
        topics = scanner.scan()
        tracker.step_done("Trend Research", topics_found=len(topics))

        tracker.pipeline_done()
    """

    def __init__(self, run_type: str = "full"):
        self.run_type = run_type
        self._state = _default()

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def pipeline_start(self):
        """Mark the pipeline as started."""
        self._state = _default()
        self._state.update({
            "status": "running",
            "run_type": self.run_type,
            "started_at": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat(),
        })
        self._log(f"Pipeline started ({self.run_type})")
        _write(self._state)

    def pipeline_resume(self, step: int, counters: dict, log_lines: list = None):
        """Resume the pipeline from a specific step."""
        self._state = _default()
        self._state.update({
            "status": "running",
            "run_type": self.run_type,
            "current_step": step - 1, # so step_start increments to step
            "counters": counters,
            "log_lines": log_lines or [],
            "started_at": datetime.now().isoformat(),
            "last_updated": datetime.now().isoformat(),
        })
        self._log(f"Pipeline resumed from step {step} ({self.run_type})")
        _write(self._state)

    def pipeline_done(self):
        """Mark the pipeline as completed."""
        self._state["status"] = "done"
        self._state["current_task"] = None
        self._state["progress_pct"] = 100
        self._state["last_updated"] = datetime.now().isoformat()
        self._log("Pipeline completed successfully ✅")
        _write(self._state)

    def pipeline_error(self, message: str):
        """Mark the pipeline as failed."""
        self._state["status"] = "error"
        self._state["error"] = message
        self._state["current_task"] = None
        self._state["last_updated"] = datetime.now().isoformat()
        self._log(f"Pipeline error: {message}")
        _write(self._state)



    # ── Steps ─────────────────────────────────────────────────────────────────

    def step_start(self, task_name: str, step: int | None = None):
        """Signal that a pipeline step has started."""
        if step is None:
            step = PIPELINE_STEPS.index(task_name) + 1 if task_name in PIPELINE_STEPS else self._state["current_step"] + 1

        total = self._state["total_steps"]
        pct = int(((step - 1) / total) * 100)

        self._state["current_task"] = task_name
        self._state["current_step"] = step
        self._state["progress_pct"] = pct
        self._state["last_updated"] = datetime.now().isoformat()
        self._log(f"▶ Step {step}/{total}: {task_name}")
        _write(self._state)

    def update_progress(self, pct_within_step: int):
        """Update the global progress bar based on the percentage completed of the *current step*."""
        total = self._state["total_steps"]
        step = self._state["current_step"]
        base_pct = ((step - 1) / total) * 100
        step_pct_worth = 100 / total
        self._state["progress_pct"] = int(base_pct + (step_pct_worth * (pct_within_step / 100)))
        self._state["last_updated"] = datetime.now().isoformat()
        _write(self._state)

    def step_done(self, task_name: str, **counters):
        """Signal that a step is done and optionally update counters."""
        step = self._state["current_step"]
        total = self._state["total_steps"]
        pct = int((step / total) * 100)
        self._state["progress_pct"] = pct

        for key, val in counters.items():
            if key in self._state["counters"]:
                self._state["counters"][key] = val

        self._state["last_updated"] = datetime.now().isoformat()
        self._log(f"✓ Done: {task_name}" + (f" ({', '.join(f'{k}={v}' for k, v in counters.items())})" if counters else ""))
        _write(self._state)

    def log(self, message: str):
        """Append a log line to the status."""
        self._log(message)
        self._state["last_updated"] = datetime.now().isoformat()
        _write(self._state)

    # ── Helpers ───────────────────────────────────────────────────────────────

    def _log(self, message: str, max_lines: int = 50):
        ts = datetime.now().strftime("%H:%M:%S")
        self._state["log_lines"].append(f"[{ts}] {message}")
        # Keep only the last N lines
        if len(self._state["log_lines"]) > max_lines:
            self._state["log_lines"] = self._state["log_lines"][-max_lines:]


# ── Module-level helpers ──────────────────────────────────────────────────────

def get_status() -> dict:
    """Read the current pipeline status (for the monitor server)."""
    return _read()


def reset_status():
    """Reset to idle (call on startup)."""
    _write(_default())

def update_global_progress(pct_within_step: int):
    """Helper to update progress globally without needing the tracker instance."""
    state = _read()
    if state["status"] not in ("running", "error"):
        return
    total = state.get("total_steps", 6)
    step = state.get("current_step", 1)
    if total <= 0: return
    base_pct = ((step - 1) / total) * 100
    step_pct_worth = 100 / total
    state["progress_pct"] = int(base_pct + (step_pct_worth * (pct_within_step / 100)))
    state["last_updated"] = datetime.now().isoformat()
    _write(state)

def force_stop():
    """Forcefully set the pipeline status to error (used when monitor kills the process)."""
    state = _read()
    if state["status"] == "running":
        state["status"] = "error"
        state["error"] = "Pipeline forcefully stopped by user"
        state["current_task"] = None
        state["last_updated"] = datetime.now().isoformat()
        state["log_lines"].append(f"[{datetime.now().strftime('%H:%M:%S')}] 🛑 Pipeline forcefully stopped by user")
        _write(state)
