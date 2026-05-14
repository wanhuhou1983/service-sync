"""Service Dashboard - Data models with push & PG sync support."""

import json
import os
import sqlite3
from datetime import datetime

DB_PATH = os.environ.get("DB_PATH", "/data/nodes.db")

# Fixed node display order
NODE_ORDER = {"S1": 0, "Lenovo": 1, "MacMini": 2, "TencentCloud": 3}


def get_db():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS nodes (
            node_name TEXT PRIMARY KEY,
            node_ip TEXT,
            hostname TEXT,
            platform TEXT,
            docker_version TEXT,
            last_seen TEXT,
            status TEXT DEFAULT 'offline',
            pg_host TEXT DEFAULT '',
            pg_port TEXT DEFAULT '5432',
            pg_user TEXT DEFAULT 'postgres',
            pg_password TEXT DEFAULT '',
            pg_dbs TEXT DEFAULT '[]'
        );

        CREATE TABLE IF NOT EXISTS containers (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_name TEXT,
            container_id TEXT,
            name TEXT,
            image TEXT,
            status TEXT,
            state TEXT,
            created TEXT,
            started_at TEXT,
            labels TEXT,
            run_config TEXT DEFAULT '{}',  -- JSON: deploy settings
            reported_at TEXT,
            UNIQUE(node_name, name)
        );

        CREATE TABLE IF NOT EXISTS images (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_name TEXT,
            tag TEXT,
            image_id TEXT,
            created TEXT,
            size INTEGER,
            reported_at TEXT,
            UNIQUE(node_name, tag)
        );

        -- Tasks for push / deploy / pg-sync
        CREATE TABLE IF NOT EXISTS tasks (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            task_type TEXT NOT NULL,           -- 'deploy' | 'pg_sync' | 'pg_query'
            source_node TEXT DEFAULT '',
            target_node TEXT NOT NULL,
            service_name TEXT DEFAULT '',
            params TEXT DEFAULT '{}',          -- JSON: image_tag, db_name, etc.
            status TEXT DEFAULT 'pending',     -- pending | running | completed | failed
            progress TEXT DEFAULT '',           -- status message
            created_at TEXT,
            started_at TEXT DEFAULT '',
            completed_at TEXT DEFAULT '',
            result TEXT DEFAULT ''
        );

        -- PG connection info per node (stored from agent heartbeat if auto-detected,
        -- or configured manually)
        CREATE TABLE IF NOT EXISTS pg_instances (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            node_name TEXT,
            name TEXT,               -- friendly name like "main-db", "test-db"
            db_name TEXT,
            host TEXT,
            port TEXT DEFAULT '5432',
            user TEXT DEFAULT 'postgres',
            password TEXT DEFAULT '',
            auto_detected INTEGER DEFAULT 0,
            created_at TEXT,
            UNIQUE(node_name, name, db_name)
        );
    """)
    conn.commit()
    conn.close()


# ─── Heartbeat ──────────────────────────────────────────

def save_heartbeat(payload: dict) -> dict:
    conn = get_db()
    now = payload.get("timestamp", datetime.utcnow().isoformat())
    node_name = payload["node_name"]

    conn.execute("""
        INSERT INTO nodes (node_name, node_ip, hostname, platform, docker_version, last_seen, status)
        VALUES (?, ?, ?, ?, ?, ?, 'online')
        ON CONFLICT(node_name) DO UPDATE SET
            node_ip=excluded.node_ip,
            hostname=excluded.hostname,
            platform=excluded.platform,
            docker_version=excluded.docker_version,
            last_seen=excluded.last_seen,
            status='online'
    """, (node_name, payload.get("node_ip", ""), payload.get("hostname", ""),
          payload.get("platform", ""), payload.get("docker_version", ""), now))

    # Upsert containers
    for c in payload.get("containers", []):
        labels_json = json.dumps(c.get("labels", {}), ensure_ascii=False)
        run_config_json = json.dumps(c.get("run_config", {}), ensure_ascii=False)
        conn.execute("""
            INSERT INTO containers (node_name, container_id, name, image, status, state, created, started_at, labels, run_config, reported_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_name, name) DO UPDATE SET
                container_id=excluded.container_id,
                image=excluded.image,
                status=excluded.status,
                state=excluded.state,
                started_at=excluded.started_at,
                labels=excluded.labels,
                run_config=excluded.run_config,
                reported_at=excluded.reported_at
        """, (node_name, c.get("id", ""), c.get("name", ""), c.get("image", ""),
              c.get("status", ""), c.get("state", ""), c.get("created", ""),
              c.get("started_at", ""), labels_json, run_config_json, now))

    # Upsert images
    for img in payload.get("images", []):
        conn.execute("""
            INSERT INTO images (node_name, tag, image_id, created, size, reported_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(node_name, tag) DO UPDATE SET
                image_id=excluded.image_id,
                size=excluded.size,
                reported_at=excluded.reported_at
        """, (node_name, img.get("tag", ""), img.get("id", ""),
              img.get("created", ""), img.get("size", 0), now))

    # Auto-detect PG instances from container labels
    for c in payload.get("containers", []):
        labels = c.get("labels", {}) or {}
        image = c.get("image", "")
        if "postgres" in image.lower():
            labels.update({"svc-dashboard:detected-pg": "true"})
            pg_db = labels.get("pg-db", c.get("name", "postgres"))
            pg_port = labels.get("pg-port", "5432")
            conn.execute("""
                INSERT INTO pg_instances (node_name, name, db_name, host, port, user, password, auto_detected, created_at)
                VALUES (?, ?, ?, ?, ?, 'postgres', '', 1, ?)
                ON CONFLICT(node_name, name, db_name) DO UPDATE SET
                    host=excluded.host,
                    port=excluded.port,
                    auto_detected=1
            """, (node_name, c["name"], pg_db, node_name, pg_port, now))

    conn.commit()
    conn.close()

    # Check pending tasks for this node
    pending = get_pending_tasks(node_name)
    return {
        "status": "ok",
        "node": node_name,
        "containers": len(payload.get("containers", [])),
        "images": len(payload.get("images", [])),
        "tasks": pending,
    }


# ─── Tasks (Deploy / PG Sync) ──────────────────────────

def get_pending_tasks(node_name: str) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM tasks WHERE target_node = ? AND status IN ('pending', 'running') ORDER BY created_at",
        (node_name,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def create_task(task_type: str, target_node: str, params: dict,
                source_node: str = "", service_name: str = "") -> dict:
    conn = get_db()
    now = datetime.utcnow().isoformat()
    cursor = conn.execute(
        "INSERT INTO tasks (task_type, source_node, target_node, service_name, params, status, created_at) VALUES (?, ?, ?, ?, ?, 'pending', ?)",
        (task_type, source_node, target_node, service_name, json.dumps(params, ensure_ascii=False), now)
    )
    task_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return {"task_id": task_id, "status": "pending"}


def update_task(task_id: int, status: str, progress: str = "", result: str = ""):
    conn = get_db()
    now = datetime.utcnow().isoformat()
    fields = {"status": status, "progress": progress, "result": result}
    if status in ("running",):
        fields["started_at"] = now
    if status in ("completed", "failed"):
        fields["completed_at"] = now

    sets = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [task_id]
    conn.execute(f"UPDATE tasks SET {sets} WHERE id=?", vals)
    conn.commit()
    conn.close()


def get_task(task_id: int) -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
    conn.close()
    if row:
        return dict(row)
    return {}


def get_recent_tasks(limit: int = 20) -> list:
    conn = get_db()
    rows = conn.execute("SELECT * FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Nodes ──────────────────────────────────────────────

def get_all_nodes() -> list:
    conn = get_db()
    rows = conn.execute("SELECT * FROM nodes").fetchall()
    conn.close()
    nodes = [dict(r) for r in rows]
    # Sort by defined order, unknown nodes at the end
    nodes.sort(key=lambda n: NODE_ORDER.get(n["node_name"], 99))
    return nodes


def get_node(node_name: str) -> dict:
    conn = get_db()
    row = conn.execute("SELECT * FROM nodes WHERE node_name=?", (node_name,)).fetchone()
    conn.close()
    return dict(row) if row else {}


def update_pg_config(node_name: str, config: dict):
    conn = get_db()
    conn.execute(
        "UPDATE nodes SET pg_host=?, pg_port=?, pg_user=?, pg_password=?, pg_dbs=? WHERE node_name=?",
        (config.get("host", ""), config.get("port", "5432"),
         config.get("user", "postgres"), config.get("password", ""),
         json.dumps(config.get("dbs", [])), node_name)
    )
    conn.commit()
    conn.close()


# ─── Containers / Images ───────────────────────────────

def get_node_containers(node_name: str) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM containers WHERE node_name = ? ORDER BY name",
        (node_name,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_container_run_config(node_name: str, container_name: str) -> dict:
    """Retrieve the run_config (deploy settings) for a specific container."""
    conn = get_db()
    row = conn.execute(
        "SELECT run_config FROM containers WHERE node_name = ? AND name = ?",
        (node_name, container_name)
    ).fetchone()
    conn.close()
    if row:
        return json.loads(row["run_config"])
    return {}


def get_node_images(node_name: str) -> list:
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM images WHERE node_name = ? ORDER BY tag",
        (node_name,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


# ─── Overview with Diff ─────────────────────────────────

def get_services_overview() -> dict:
    conn = get_db()
    nodes = conn.execute("SELECT node_name, status FROM nodes").fetchall()
    nodes = [dict(r) for r in nodes]
    # Sort by defined order
    nodes.sort(key=lambda n: NODE_ORDER.get(n["node_name"], 99))
    node_names = [n["node_name"] for n in nodes]
    node_status = {n["node_name"]: n["status"] for n in nodes}

    # Get distinct container names with their images across all nodes
    services = {}
    for nn in node_names:
        containers = conn.execute(
            "SELECT name, image, status, state, started_at FROM containers WHERE node_name = ?",
            (nn,)
        ).fetchall()
        for c in containers:
            svc_name = c["name"]
            if svc_name not in services:
                services[svc_name] = {}
            services[svc_name][nn] = {
                "image": c["image"],
                "status": c["status"],
                "state": c["state"],
                "started_at": c["started_at"],
            }

    conn.close()

    result = {"nodes": node_names, "node_status": node_status, "services": []}

    for svc_name, node_data in sorted(services.items()):
        versions = set()
        running_count = 0
        for nn in node_names:
            if nn in node_data:
                versions.add(node_data[nn]["image"])
                if node_data[nn]["state"] == "running":
                    running_count += 1

        if len(versions) <= 1 and running_count > 0:
            status = "aligned"
        elif len(versions) > 1:
            status = "differs"
        elif running_count == 0:
            status = "stopped"
        else:
            status = "partial"

        # Check if PG service
        is_pg = any("postgres" in nd.get("image", "").lower()
                    for nd in node_data.values())

        entry = {
            "name": svc_name,
            "status": status,
            "is_pg": is_pg,
            "nodes": {},
        }
        for nn in node_names:
            entry["nodes"][nn] = node_data.get(nn, None)

        result["services"].append(entry)

    return result


# ─── PG Instances ───────────────────────────────────────

def get_pg_instances(node_name: str = "") -> list:
    conn = get_db()
    if node_name:
        rows = conn.execute(
            "SELECT * FROM pg_instances WHERE node_name=? ORDER BY name",
            (node_name,)
        ).fetchall()
    else:
        rows = conn.execute("SELECT * FROM pg_instances ORDER BY node_name, name").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_pg_instance(node_name: str, name: str, db_name: str, host: str = "",
                    port: str = "5432", user: str = "postgres", password: str = ""):
    conn = get_db()
    now = datetime.utcnow().isoformat()
    conn.execute("""
        INSERT INTO pg_instances (node_name, name, db_name, host, port, user, password, auto_detected, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, 0, ?)
        ON CONFLICT(node_name, name, db_name) DO UPDATE SET
            host=excluded.host, port=excluded.port, user=excluded.user, password=excluded.password
    """, (node_name, name, db_name, host or node_name, port, user, password, now))
    conn.commit()
    conn.close()


# Init on import
init_db()
