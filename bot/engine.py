"""
Patch validation, application, and rule loading.

Only the engine (not rules.py) is allowed to use subprocess, os, etc.
"""

import ast
import importlib.util
import logging
import os
import subprocess
import tempfile
import types
from pathlib import Path

log = logging.getLogger(__name__)

# ── Forbidden AST nodes ───────────────────────────────────────────────────────

_FORBIDDEN_MODULES = frozenset({
    "os", "sys", "io", "subprocess", "socket", "urllib", "http",
    "requests", "httpx", "aiohttp", "pathlib", "shutil", "glob",
    "pickle", "marshal", "shelve", "dbm", "sqlite3", "importlib",
    "builtins", "ctypes", "threading", "multiprocessing", "concurrent",
    "asyncio", "signal", "mmap", "select", "selectors", "ssl",
    "hashlib", "hmac", "secrets", "tempfile", "fnmatch", "linecache",
    "tokenize", "compileall", "py_compile", "zipfile", "tarfile",
    "gzip", "bz2", "lzma", "zlib", "struct", "codecs", "ftplib",
    "smtplib", "telnetlib", "xmlrpc", "email", "html", "xml",
    "csv", "configparser", "logging", "unittest", "doctest",
    "pdb", "profile", "cProfile", "timeit", "trace", "gc",
    "inspect", "dis", "ast", "symtable", "token", "keyword",
    "pty", "tty", "termios", "fcntl", "grp", "pwd", "resource",
    "readline", "rlcompleter", "curses", "tkinter", "wx",
})

_FORBIDDEN_BUILTINS = frozenset({
    "open", "exec", "eval", "compile", "__import__", "breakpoint",
    "input", "print",  # print is fine in dev but noisy in prod; can be removed via patch
})


def check_ast_safety(code: str) -> list[str]:
    """
    Walk the AST of `code` and return a list of violation descriptions.
    Empty list means the code is safe to load.
    """
    violations: list[str] = []

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        return [f"Syntax error: {exc}"]

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root in _FORBIDDEN_MODULES:
                    violations.append(f"Forbidden import: {alias.name}")

        elif isinstance(node, ast.ImportFrom):
            if node.module:
                root = node.module.split(".")[0]
                if root in _FORBIDDEN_MODULES:
                    violations.append(f"Forbidden import: from {node.module}")

        elif isinstance(node, ast.Call):
            if isinstance(node.func, ast.Name) and node.func.id in _FORBIDDEN_BUILTINS:
                violations.append(f"Forbidden call: {node.func.id}()")

    return violations


# ── Patch validation ──────────────────────────────────────────────────────────

def _parse_patch_targets(patch_text: str) -> list[str]:
    """
    Extract target filenames from a unified diff.
    Returns the list of files the patch would modify/create/delete.
    """
    targets: list[str] = []
    for line in patch_text.splitlines():
        if line.startswith("+++ "):
            path = line[4:].split("\t")[0].strip()
            # Strip a/ or b/ git prefixes
            for prefix in ("b/", "a/"):
                if path.startswith(prefix):
                    path = path[len(prefix):]
                    break
            if path not in ("/dev/null", ""):
                targets.append(path)
    return targets


def _apply_patch_to_content(patch_text: str, current_content: str) -> tuple[bool, str, str]:
    """
    Apply patch_text to current_content in a temp dir.
    Returns (success, new_content, error_message).
    Tries -p1 (git style) then -p0 (plain style).
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        tmp = Path(tmpdir)
        rules_file = tmp / "rules.py"
        patch_file = tmp / "change.patch"

        patch_file.write_text(patch_text, encoding="utf-8")

        for strip in (1, 0):
            rules_file.write_text(current_content, encoding="utf-8")
            result = subprocess.run(
                ["patch", f"-p{strip}", "--input", str(patch_file), str(rules_file)],
                capture_output=True,
                text=True,
            )
            # Clean up .orig that patch leaves behind
            orig = tmp / "rules.py.orig"
            if orig.exists():
                orig.unlink()

            if result.returncode == 0:
                return True, rules_file.read_text(encoding="utf-8"), ""

        return False, "", result.stderr.strip()


def validate_patch(patch_text: str, rules_path: Path) -> tuple[bool, str, str | None]:
    """
    Full validation pipeline.

    Returns:
        (True, "", new_rules_content)   — patch is safe and applies cleanly
        (False, error_message, None)    — patch is rejected
    """
    if not patch_text.strip():
        return False, "Patch is empty.", None

    # 1. Check which files the patch touches
    targets = _parse_patch_targets(patch_text)
    if not targets:
        return False, "Could not detect target files in patch.", None

    bad = [t for t in targets if t != "rules.py"]
    if bad:
        return False, f"Patch touches forbidden file(s): {', '.join(bad)}", None

    # 2. Apply to a temp copy
    current = rules_path.read_text(encoding="utf-8")
    ok, new_content, err = _apply_patch_to_content(patch_text, current)
    if not ok:
        return False, f"Patch does not apply cleanly:\n{err}", None

    # 3. AST safety check
    violations = check_ast_safety(new_content)
    if violations:
        bullet = "\n".join(f"• {v}" for v in violations)
        return False, f"Safety violations in patched rules.py:\n{bullet}", None

    return True, "", new_content


# ── Patch application ─────────────────────────────────────────────────────────

def apply_patch(patch_text: str, rules_path: Path, proposal_id: int) -> tuple[bool, str]:
    """
    Apply a pre-validated patch to the live rules.py and git-commit the result.
    Returns (success, error_message).
    """
    current = rules_path.read_text(encoding="utf-8")
    ok, new_content, err = _apply_patch_to_content(patch_text, current)
    if not ok:
        return False, f"Patch failed on live file: {err}"

    rules_path.write_text(new_content, encoding="utf-8")

    # Attempt a git commit — non-fatal if git isn't configured
    try:
        rules_dir = rules_path.parent
        git_env = {
            **os.environ,
            "GIT_AUTHOR_NAME": "Nomic Bot",
            "GIT_AUTHOR_EMAIL": "nomic-bot@localhost",
            "GIT_COMMITTER_NAME": "Nomic Bot",
            "GIT_COMMITTER_EMAIL": "nomic-bot@localhost",
        }
        subprocess.run(["git", "add", "rules.py"], cwd=rules_dir, capture_output=True, check=True)
        subprocess.run(
            ["git", "commit", "-m", f"Proposal #{proposal_id} passed"],
            cwd=rules_dir,
            capture_output=True,
            env=git_env,
            check=True,
        )
        log.info("Committed proposal #%d to rules repo.", proposal_id)
    except Exception as exc:
        log.warning("git commit failed (non-fatal): %s", exc)

    return True, ""


# ── Rule loading ──────────────────────────────────────────────────────────────

def load_rules(rules_path: Path) -> types.ModuleType:
    """Dynamically load rules.py as a module."""
    spec = importlib.util.spec_from_file_location("nomic_rules", rules_path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def get_rule(rules: types.ModuleType, name: str, default):
    """Get a constant from rules, falling back to default."""
    return getattr(rules, name, default)


def call_rule(rules: types.ModuleType, func_name: str, *args, default=None):
    """
    Call a function from rules.py, returning `default` (or calling it if
    callable) if the function doesn't exist or raises.
    """
    func = getattr(rules, func_name, None)
    if func is None:
        return default() if callable(default) else default
    try:
        return func(*args)
    except Exception as exc:
        log.error("rules.%s(%s) raised: %s", func_name, args, exc)
        return default() if callable(default) else default
