import os
import sqlite3
from datetime import datetime
from typing import Any, Optional

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "flow_studio.db")
PROFILES_DIR = os.path.join(BASE_DIR, "profiles")
UPLOADS_DIR = os.path.join(BASE_DIR, "uploads_ref")
OUTPUTS_DIR = os.path.join(BASE_DIR, "outputs")

for d in (PROFILES_DIR, UPLOADS_DIR, OUTPUTS_DIR):
    os.makedirs(d, exist_ok=True)


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with get_conn() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS accounts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                email TEXT DEFAULT '',
                proxy_url TEXT DEFAULT '',
                profile_dir TEXT NOT NULL,
                flow_project_url TEXT DEFAULT 'https://flow.google.com/',
                last_ip TEXT DEFAULT '',
                status TEXT DEFAULT 'ready',
                credits_info TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                aspect_ratio TEXT DEFAULT 'portrait',
                model_name TEXT DEFAULT 'Omni 1.1 Flash',
                video_count TEXT DEFAULT 'x1',
                confirm_mode TEXT DEFAULT 'never',
                image_model TEXT DEFAULT 'Nano Banana 2.1',
                gen_mode TEXT DEFAULT 'references',
                global_ref_media TEXT DEFAULT '',
                global_ref_desc TEXT DEFAULT '',
                merged_video_path TEXT DEFAULT '',
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS scenes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                scene_order INTEGER NOT NULL DEFAULT 1,
                duration_sec INTEGER NOT NULL DEFAULT 8,
                ref_media_path TEXT DEFAULT '',
                ref_description TEXT DEFAULT '',
                scene_prompt TEXT NOT NULL,
                composed_prompt TEXT DEFAULT '',
                assigned_account_id INTEGER DEFAULT NULL,
                account_ip_used TEXT DEFAULT '',
                status TEXT DEFAULT 'pending',
                video_path TEXT DEFAULT '',
                error_msg TEXT DEFAULT '',
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY(project_id) REFERENCES projects(id) ON DELETE CASCADE
            );
            """
        )
        conn.execute(
            "UPDATE accounts SET flow_project_url = 'https://flow.google.com/' WHERE flow_project_url = 'https://labs.google/fx/tools/flow' OR flow_project_url = ''"
        )
        # Safe schema migration for existing projects table
        for col_sql in (
            "ALTER TABLE projects ADD COLUMN video_count TEXT DEFAULT 'x1'",
            "ALTER TABLE projects ADD COLUMN confirm_mode TEXT DEFAULT 'never'",
            "ALTER TABLE projects ADD COLUMN image_model TEXT DEFAULT 'Nano Banana 2.1'",
        ):
            try:
                conn.execute(col_sql)
            except Exception:
                pass
        conn.commit()


def compose_scene_prompt(
    scene_prompt: str,
    ref_description: str = "",
    global_ref_desc: str = "",
    duration_sec: int = 8,
) -> str:
    """Combine Reference Media Description + Scene Prompt into a cohesive 8s Veo prompt."""
    parts = []
    eff_ref_desc = (ref_description or "").strip() or (global_ref_desc or "").strip()
    if eff_ref_desc:
        parts.append(f"[Reference Visual / Subject Consistency: {eff_ref_desc}]")
    clean_prompt = (scene_prompt or "").strip()
    if clean_prompt:
        parts.append(clean_prompt)
    parts.append(f"(Duration: {duration_sec}s continuous cinematic shot, consistent lighting and subject identity)")
    return " ".join(parts)


init_db()
