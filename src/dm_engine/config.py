"""Paths, env, and the test-mode data guard. Contract: PLAN.md §12.1.

Env vars (see .env.example — the engine holds no secrets):
  DM_ENGINE_DB    absolute path to the SQLite file; defaults to <root>/data/dm.db
  DM_ENGINE_TEST  '1' puts the engine in test mode (gmail_compat refuses to build a
                  real service, and the db_path guard below tightens)
  DM_ENGINE_DASHBOARD_METRICS_DIR / DM_ENGINE_DASHBOARD_APPROVALS_DIR
                  redirect the two DASHBOARD output dirs. Without these the only
                  choices are writing into the operator's live dashboard or, under
                  DM_ENGINE_TEST=1, `report.assert_sandboxed` refusing outright —
                  so a full end-to-end rehearsal of `metrics-emit` had nowhere
                  safe to land. Point them at a temp dir to rehearse the pipeline.

The guard exists so a test run — or an agent with a stray env var — mechanically
cannot open a database outside the repo or a temp dir (PLAN.md §10 row (n)).
"""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

#: Repo root: src/dm_engine/config.py -> src/dm_engine -> src -> <root>
REPO_ROOT = Path(__file__).resolve().parents[2]


class ConfigError(RuntimeError):
    """Configuration would let the engine read or write outside its sandbox."""


@dataclass(frozen=True)
class Config:
    root: Path
    db_path: Path
    migrations_dir: Path
    variants_dir: Path
    review_dir: Path
    outbox_dir: Path
    locks_dir: Path
    gmail_skill_root: Path
    suppression_file: Path
    dashboard_metrics_dir: Path
    dashboard_approvals_dir: Path
    pending_actions: Path
    test_mode: bool


def _tmp_roots() -> list[Path]:
    """Directories a test is allowed to keep its database in."""
    roots = [Path(tempfile.gettempdir())]
    for var in ("TMPDIR", "PYTEST_DEBUG_TEMPROOT"):
        value = os.environ.get(var)
        if value:
            roots.append(Path(value))
    resolved = []
    for path in roots:
        try:
            resolved.append(path.resolve())
        except OSError:
            continue
    return resolved


def _check_db_path(db_path: Path, root: Path, test_mode: bool) -> None:
    allowed = [root.resolve()]
    if test_mode:
        allowed.extend(_tmp_roots())
    if any(db_path.is_relative_to(base) for base in allowed):
        return
    where = "repo or a temp dir" if test_mode else "the repo"
    raise ConfigError(
        f"refusing db_path {db_path}: it must live inside {where} "
        f"(allowed roots: {', '.join(str(p) for p in allowed)}). "
        "Set DM_ENGINE_DB to a path under the project, or unset it."
    )


def _dir_env(name: str, default: Path) -> Path:
    """An overridable output directory. Empty or unset keeps the default."""
    value = os.environ.get(name)
    if not value or not value.strip():
        return default
    return Path(value).expanduser().resolve()


def load_config() -> Config:
    """Build the Config, create missing data subdirs, enforce the db_path guard."""
    root = REPO_ROOT
    test_mode = os.environ.get("DM_ENGINE_TEST") == "1"

    db_env = os.environ.get("DM_ENGINE_DB")
    db_path = Path(db_env).expanduser() if db_env else root / "data" / "dm.db"
    db_path = db_path.resolve()
    _check_db_path(db_path, root, test_mode)

    home = Path.home()
    gmail_skill_root = home / ".config" / "skills" / "gmail-helper"
    dashboard = home / "work" / "dashboard" / "data"
    cfg = Config(
        root=root,
        db_path=db_path,
        migrations_dir=root / "data" / "migrations",
        variants_dir=root / "data" / "variants",
        review_dir=root / "data" / "review",
        outbox_dir=root / "data" / "outbox",
        locks_dir=root / "data" / "locks",
        gmail_skill_root=gmail_skill_root,
        suppression_file=gmail_skill_root / "suppression_list.txt",
        dashboard_metrics_dir=_dir_env("DM_ENGINE_DASHBOARD_METRICS_DIR", dashboard / "metrics"),
        dashboard_approvals_dir=_dir_env("DM_ENGINE_DASHBOARD_APPROVALS_DIR",
                                      dashboard / "approvals"),
        pending_actions=home / ".config" / "pending-actions.md",
        test_mode=test_mode,
    )
    ensure_dirs(cfg)
    return cfg


def ensure_dirs(cfg: Config) -> None:
    """Create the engine-owned data subdirs. Never touches ~/.config or ~/work/dashboard."""
    for path in (cfg.db_path.parent, cfg.variants_dir, cfg.review_dir,
                 cfg.outbox_dir, cfg.locks_dir):
        path.mkdir(parents=True, exist_ok=True)
