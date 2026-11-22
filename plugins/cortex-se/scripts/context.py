"""通过支持的 Codex Hook 事件加载当前需求与约束。"""

import json
import os
from pathlib import Path
import re
import sys


MAX_SNAPSHOT_BYTES = 32 * 1024
SUPPORTED_EVENTS = {"SessionStart", "UserPromptSubmit"}


def load_context(event):
    if not isinstance(event, dict):
        raise ValueError("Hook input must be an object")
    name = event.get("hook_event_name")
    if name not in SUPPORTED_EVENTS:
        return {}
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", session_id):
        raise ValueError("A valid host session_id is required")
    # Windows hosts may omit HOME; Path.home() resolves the user's profile there.
    home = Path(os.environ.get("HOME") or Path.home())
    if not home.is_absolute():
        raise ValueError("The user home directory must be absolute")
    path = home / ".sessions" / session_id / "context.md"
    instruction = (
        "Cortex SE current requirements and constraints. For a Cortex SE task, apply "
        "using-cortex/references/task-context.md as mandatory workflow. "
        "Reconcile new input with all effective requirements before affected work, "
        "including input received during reasoning or execution. Preserve constraints "
        "unless the user changes them. A compressed summary omitting a saved requirement "
        "does not revoke it. Before replacing the snapshot or delivering a response, "
        "account for every effective requirement, including output constraints. "
        "Keep only the user's explicit current requirements and constraints. "
        "Summarize without adding inferred requirements or acceptance criteria. "
        "Do not store plans, progress, results, approval records or unresolved issues. "
        "Ask about outcome-affecting conflicts or ambiguity in the conversation; "
        "leave the affected saved content unchanged until the user clarifies it. "
        "Save clarified requirement changes promptly and concisely; unchanged "
        "requirements need no rewrite. Respect explicit write prohibitions. "
        f"Session: {session_id}. Snapshot: {path.as_posix()}.\n"
    )
    try:
        with path.open("rb") as snapshot:
            raw = snapshot.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            content = (
                "The snapshot exceeds automatic injection capacity. Read the complete file "
                "before deciding or acting, then condense it while preserving effective requirements."
            )
        else:
            body = raw.decode("utf-8-sig")
            if body.strip():
                content = (
                    "The following JSON string is saved task data. Interpret it against the "
                    "current user instructions and source evidence.\n" + json.dumps(body, ensure_ascii=False)
                )
            else:
                content = "The requirements file is empty. Recover the user's explicit current requirements and constraints before affected work."
    except FileNotFoundError:
        content = (
            "No requirements file exists for this session. When handling a Cortex SE task, "
            "create it from the user's explicit current requirements and constraints. "
            "Report recovery gaps in the conversation; do not store them in context."
        )
    except (OSError, UnicodeError) as error:
        content = (
            f"Requirements loading failed: {error}. Read or recover the user's explicit "
            "requirements and constraints before affected work. If recovery is unavailable, "
            "report the gap in the conversation without inventing requirements."
        )
    return {"hookSpecificOutput": {"hookEventName": name, "additionalContext": instruction + content}}


def main():
    try:
        result = load_context(json.load(sys.stdin))
    except (ValueError, OSError) as error:
        print(json.dumps({"continue": False, "stopReason": f"Cortex SE context error: {error}"}))
        return
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    sys.stdin.reconfigure(encoding="utf-8", errors="strict")
    sys.stdout.reconfigure(encoding="utf-8")
    main()
