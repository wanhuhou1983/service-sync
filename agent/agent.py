#!/usr/bin/env python3
"""Service Dashboard Agent v2 - Reports status AND executes push/PG sync tasks.

Each agent runs on a node and:
1. Every 30s: Reports Docker status to dashboard
2. On heartbeat response: Checks for pending tasks
3. Executes tasks: docker deploy, PG sync, PG query
4. Reports progress back to dashboard
"""

import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import traceback
import urllib.request
import urllib.error

# ─── Config ─────────────────────────────────────────────

DASHBOARD_URL = os.environ.get("DASHBOARD_URL", "http://100.96.28.120:8080")
NODE_NAME = os.environ.get("NODE_NAME", socket.gethostname())
NODE_IP = os.environ.get("NODE_IP", "")
INTERVAL = int(os.environ.get("REPORT_INTERVAL", "30"))
COMPOSE_DIR = os.environ.get("COMPOSE_DIR", "")  # Where docker-compose.yml lives on this node

# PG connections: JSON string like {"main":"host=... dbname=... user=... password=... password=..."}
PG_CONNECTIONS_JSON = os.environ.get("PG_CONNECTIONS", "{}")

try:
    import docker
except ImportError:
    print("ERROR: docker package not installed. pip install docker")
    sys.exit(1)


# ─── Docker helpers ────────────────────────────────────

def get_containers(client):
    containers = []
    for c in client.containers.list(all=True):
        try:
            containers.append({
                "id": c.short_id,
                "name": c.name,
                "image": c.image.tags[0] if c.image.tags else c.image.short_id,
                "status": c.status,
                "state": c.attrs.get("State", {}).get("Status", "unknown"),
                "created": c.attrs.get("Created", ""),
                "started_at": c.attrs.get("State", {}).get("StartedAt", ""),
                "ports": str(c.attrs.get("NetworkSettings", {}).get("Ports", {})),
                "labels": c.labels,
                # Full config for re-creation on other nodes
                "run_config": _get_run_config(c),
            })
        except Exception:
            containers.append({
                "id": c.short_id, "name": c.name,
                "image": "?", "status": "?", "state": "?",
                "created": "", "started_at": "", "ports": "", "labels": {},
                "run_config": {},
            })
    return containers


def _get_run_config(c):
    """Extract docker run settings from a container for re-creation."""
    cfg = c.attrs.get("Config", {}) or {}
    hc = c.attrs.get("HostConfig", {}) or {}
    nc = c.attrs.get("NetworkSettings", {}) or {}

    # Port bindings: {"80/tcp": [{"HostIp":"","HostPort":"8080"}]}
    port_bindings = hc.get("PortBindings") or {}
    ports = []
    for cp, bindings in port_bindings.items():
        for b in bindings or []:
            hp = b.get("HostPort", "")
            hi = b.get("HostIp", "")
            ports.append(f"{hi}:{hp}:{cp}" if hi else f"{hp}:{cp}")

    # Volumes: ["/host:/container:ro"]
    binds = hc.get("Binds") or []

    # Env: ["KEY=VALUE", ...]
    env = cfg.get("Env") or []

    # Restart policy
    rp = hc.get("RestartPolicy") or {}
    restart_name = rp.get("Name", "")

    return {
        "ports": ports,
        "binds": binds,
        "env": env,
        "restart": restart_name,
        "network_mode": hc.get("NetworkMode", ""),
        "cmd": cfg.get("Cmd") or [],
        "entrypoint": cfg.get("Entrypoint") or [],
        "workdir": cfg.get("WorkingDir", ""),
        "user": cfg.get("User", ""),
        "hostname": cfg.get("Hostname", ""),
        "privileged": hc.get("Privileged", False),
        "labels": cfg.get("Labels") or {},
    }


def get_images(client):
    images = []
    for img in client.images.list(all=True):
        for tag in img.tags:
            images.append({
                "tag": tag,
                "id": img.short_id,
                "created": img.attrs.get("Created", ""),
                "size": img.attrs.get("Size", 0),
            })
    return images


# ─── HTTP helpers ──────────────────────────────────────

def api_post(path, data, timeout=15):
    url = f"{DASHBOARD_URL.rstrip('/')}/{path.lstrip('/')}"
    body = json.dumps(data).encode("utf-8")
    req = urllib.request.Request(
        url, data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as e:
        print(f"  HTTP {e.code}: {e.read().decode()[:200]}")
    except Exception as e:
        print(f"  Request failed: {e}")
    return None


# ─── Container Creation ────────────────────────────────

def _create_container_from_config(client, name, img_tag, cfg):
    """Create a container from run_config dict (captured from source node)."""
    print(f"    Creating container {name} from config...")

    # Build port bindings: ["8080:80/tcp", "0.0.0.0:3000:3000"] → {"80/tcp":(...)}
    port_bindings = {}
    for p in cfg.get("ports", []):
        parts = p.split(":")
        if len(parts) == 3:
            hi, hp, cp = parts
            port_bindings[cp] = [(hi, hp)]
        elif len(parts) == 2:
            hp, cp = parts
            port_bindings[cp] = hp
        # else: raw format, skip

    # Restart policy
    restart_policy = {"Name": cfg.get("restart", "unless-stopped")}

    # Volumes — filter out binds whose host path doesn't exist on this machine
    raw_volumes = cfg.get("binds", [])
    volumes = []
    for v in raw_volumes:
        # Format: "host_path:container_path:mode" or "host_path:container_path"
        parts = v.split(":")
        host_path = parts[0]
        if os.path.exists(host_path):
            volumes.append(v)
        else:
            print(f"    Skipping bind mount (host path not found): {v}")

    # Environment
    env = cfg.get("env", [])

    # Labels (merge with empty dict)
    labels = cfg.get("labels") or {}

    # Network: if custom network specified, ensure it exists on this host
    net_mode = cfg.get("network_mode") or None
    if net_mode and net_mode not in ("bridge", "host", "none", "default"):
        try:
            client.networks.get(net_mode)
        except docker.errors.NotFound:
            print(f"    Network '{net_mode}' not found, creating it...")
            client.networks.create(net_mode, driver="bridge")
            print(f"    Created network '{net_mode}'")

    try:
        c = client.containers.run(
            img_tag,
            name=name,
            detach=True,
            platform=cfg.get("platform", "linux/amd64"),  # default to amd64 for cross-arch compat
            ports=port_bindings if port_bindings else None,
            volumes=volumes if volumes else None,
            environment=env if env else None,
            restart_policy=restart_policy,
            network_mode=net_mode,
            command=cfg.get("cmd") or None,
            entrypoint=cfg.get("entrypoint") or None,
            working_dir=cfg.get("workdir") or None,
            user=cfg.get("user") or None,
            hostname=cfg.get("hostname") or None,
            privileged=cfg.get("privileged", False),
            labels=labels or None,
            remove=False,
        )
        print(f"    Container {name} created ({c.short_id})")
    except Exception as e:
        print(f"    Failed to create container: {e}")
        raise


# ─── Task Executors ────────────────────────────────────

def execute_deploy(task):
    """Pull image and restart service via docker compose."""
    tid = task["id"]
    params = json.loads(task["params"]) if isinstance(task["params"], str) else task["params"]
    svc = params.get("service_name", task.get("service_name", ""))
    img_tag = params.get("image_tag", "")

    print(f"  [Task {tid}] Deploy: pulling {img_tag or svc}...")
    api_post(f"/api/task/{tid}/update", {"status": "running", "progress": f"Pulling {img_tag or svc}..."})

    try:
        client = docker.from_env()

        if img_tag:
            # Pull specific image
            print(f"    Pulling {img_tag}...")
            # Try pull; if platform manifest not found, retry with linux/amd64
            explicit_platform = params.get("platform", "")
            try:
                if explicit_platform:
                    client.images.pull(img_tag, platform=explicit_platform)
                else:
                    client.images.pull(img_tag)
            except docker.errors.NotFound as e:
                err_msg = str(e)
                if "no matching manifest" in err_msg or "arm64" in err_msg or "not found" in err_msg.lower():
                    print(f"    Platform mismatch, retrying with linux/amd64: {err_msg[:100]}")
                    client.images.pull(img_tag, platform="linux/amd64")
                else:
                    raise
            print(f"    Pulled {img_tag}")

        if COMPOSE_DIR and svc:
            # Docker compose restart
            print(f"    Restarting via docker compose ({COMPOSE_DIR})...")
            subprocess.run(
                ["docker", "compose", "up", "-d", svc],
                cwd=COMPOSE_DIR, check=True, capture_output=True, timeout=120,
            )
            print(f"    Service {svc} restarted")
        elif svc:
            # Try to restart existing container, or create from run_config
            try:
                c = client.containers.get(svc)
                c.restart(timeout=10)
                print(f"    Container {svc} restarted")
            except docker.errors.NotFound:
                print(f"    Container {svc} not found, trying to create...")
                run_config = params.get("run_config", {})
                if run_config:
                    _create_container_from_config(client, svc, img_tag, run_config)
                else:
                    print(f"    No run_config available, skipping container creation")

        api_post(f"/api/task/{tid}/update", {
            "status": "completed",
            "progress": f"Deployed {img_tag or svc}",
            "result": "ok",
        })
        print(f"  [Task {tid}] Deploy completed")
        return  # Skip repeated error handling below

        api_post(f"/api/task/{tid}/update", {
            "status": "completed",
            "progress": f"Deployed {img_tag or svc}",
            "result": "ok",
        })
        print(f"  [Task {tid}] Deploy completed")

    except subprocess.TimeoutExpired:
        api_post(f"/api/task/{tid}/update", {
            "status": "failed", "progress": "Timeout waiting for compose",
        })
    except Exception as e:
        api_post(f"/api/task/{tid}/update", {
            "status": "failed", "progress": str(e),
            "result": traceback.format_exc(),
        })
        print(f"  [Task {tid}] Deploy FAILED: {e}")


def execute_pg_sync(task):
    """Sync PostgreSQL from source to target via pg_dump | psql."""
    tid = task["id"]
    params = json.loads(task["params"]) if isinstance(task["params"], str) else task["params"]
    source_node = params.get("source_node", "")
    db_name = params.get("db_name", "")
    mode = params.get("mode", "schema")

    print(f"  [Task {tid}] PG Sync: {mode} from {source_node} to {NODE_NAME}, db={db_name}")

    # Build connection info from task params (provided by dashboard)
    s_host = params.get("source_host", "")
    s_port = params.get("source_port", "5432")
    s_user = params.get("source_user", "postgres")
    s_pass = params.get("source_password", "")
    t_host = params.get("target_host", "localhost")
    t_port = params.get("target_port", "5432")
    t_user = params.get("target_user", "postgres")
    t_pass = params.get("target_password", "")

    # For self-sync (source == target), reuse source creds for target
    if not t_pass and source_node == NODE_NAME:
        t_pass = s_pass
        t_user = s_user
        t_host = "127.0.0.1"

    source_conn = f"host={s_host} port={s_port} dbname={db_name} user={s_user}"
    target_conn = f"host={t_host} port={t_port} dbname={db_name} user={t_user}"

    dump_args = ""
    if mode == "schema":
        dump_args = "--schema-only --no-owner --no-privileges"
    elif mode == "data":
        dump_args = "--data-only --no-owner"
    else:
        dump_args = "--no-owner --no-privileges"

    api_post(f"/api/task/{tid}/update", {"status": "running", "progress": f"Dumping {mode} from {source_node}..."})

    try:
        # Step 1: pg_dump from source
        print(f"    Dumping from source ({s_host}:{s_port})...")
        dump_env = os.environ.copy()
        if s_pass:
            dump_env["PGPASSWORD"] = s_pass
        dump_cmd = f"pg_dump {dump_args} '{source_conn}'"
        print(f"    Running: pg_dump...")

        dump_proc = subprocess.Popen(
            dump_cmd, shell=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=dump_env,
        )

        # Step 2: Stream into psql on target
        api_post(f"/api/task/{tid}/update", {"status": "running", "progress": "Restoring to target..."})
        print(f"    Restoring to target ({t_host}:{t_port})...")

        restore_env = os.environ.copy()
        if t_pass:
            restore_env["PGPASSWORD"] = t_pass

        # For schema sync: create DB if not exists, then restore
        if mode in ("schema", "full"):
            createdb_cmd = f"createdb '{target_conn}' 2>/dev/null || true"
            subprocess.run(createdb_cmd, shell=True, env=restore_env, capture_output=True, timeout=30)

        restore_cmd = f"psql '{target_conn}'"
        restore_proc = subprocess.Popen(
            restore_cmd, shell=True, stdin=dump_proc.stdout,
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            env=restore_env,
        )
        dump_proc.stdout.close()

        stdout, stderr = restore_proc.communicate(timeout=300)
        dump_proc.wait(timeout=60)

        if restore_proc.returncode == 0:
            api_post(f"/api/task/{tid}/update", {
                "status": "completed",
                "progress": f"PG {mode} sync completed",
                "result": stdout.decode()[:500],
            })
            print(f"  [Task {tid}] PG sync completed")
        else:
            err = stderr.decode()[:500]
            # psql often returns warnings as errors, check if actually failed
            if "FATAL" in err or "ERROR" in err:
                api_post(f"/api/task/{tid}/update", {
                    "status": "failed",
                    "progress": f"PG restore error",
                    "result": err,
                })
                print(f"  [Task {tid}] PG sync FAILED: {err[:200]}")
            else:
                # Warnings only, treat as success
                api_post(f"/api/task/{tid}/update", {
                    "status": "completed",
                    "progress": f"PG {mode} sync completed (with warnings)",
                    "result": err[:500],
                })
                print(f"  [Task {tid}] PG sync completed (with warnings)")

    except subprocess.TimeoutExpired:
        api_post(f"/api/task/{tid}/update", {
            "status": "failed", "progress": "PG sync timed out (5 min)",
        })
    except FileNotFoundError as e:
        api_post(f"/api/task/{tid}/update", {
            "status": "failed", "progress": f"pg_dump/psql not found: install postgresql-client",
            "result": str(e),
        })
    except Exception as e:
        api_post(f"/api/task/{tid}/update", {
            "status": "failed", "progress": str(e),
            "result": traceback.format_exc(),
        })
        print(f"  [Task {tid}] PG sync FAILED: {e}")


def _resolve_ip(node_name: str) -> str:
    """Resolve node hostname to IP. Uses /etc/hosts or fallback."""
    # Simple mapping
    ip_map = {
        "S1": "100.96.28.120",
        "MacMini": "100.77.50.100",
        "Lenovo": "100.95.148.117",
    }
    return ip_map.get(node_name, node_name)


# ─── Main Loop ─────────────────────────────────────────

def main():
    print(f"=== Service Dashboard Agent v2 ===")
    print(f"  Node:       {NODE_NAME}")
    print(f"  Dashboard:  {DASHBOARD_URL}")
    print(f"  Interval:   {INTERVAL}s")
    print(f"  Compose:    {COMPOSE_DIR or '(none)'}")

    # Check for pg tools
    has_pg_dump = shutil.which("pg_dump") is not None
    has_psql = shutil.which("psql") is not None
    print(f"  PG tools:   pg_dump={'✓' if has_pg_dump else '✗'}  psql={'✓' if has_psql else '✗'}")

    # Init Docker
    try:
        client = docker.from_env()
        ver = client.version()
        print(f"  Docker:     ✓ v{ver.get('Version', '?')}")
    except Exception as e:
        print(f"  Docker:     ✗ {e}")
        sys.exit(1)

    print(f"\n  Entering main loop (Ctrl+C to stop)...\n")

    while True:
        try:
            print(f"[{time.strftime('%H:%M:%S')}] Collecting status...", end=" ")
            sys.stdout.flush()

            containers = get_containers(client)
            images = get_images(client)

            payload = {
                "node_name": NODE_NAME,
                "node_ip": NODE_IP or socket.gethostbyname(socket.gethostname()),
                "hostname": socket.gethostname(),
                "platform": platform.platform(),
                "docker_version": client.version().get("Version", "unknown"),
                "containers": containers,
                "images": images,
                "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S+08:00"),
            }

            result = api_post("/api/heartbeat", payload, timeout=10)

            if result:
                print(f"✓ {result.get('containers', 0)} ctn, {result.get('images', 0)} img", end="")

                # Check for pending tasks
                tasks = result.get("tasks", [])
                if tasks:
                    print(f", {len(tasks)} task(s)!")
                    for task in tasks:
                        task_type = task.get("task_type", "")
                        print(f"  → Executing task {task['id']}: {task_type}")

                        if task_type == "deploy":
                            execute_deploy(task)
                        elif task_type == "pg_sync":
                            execute_pg_sync(task)
                        else:
                            print(f"  Unknown task type: {task_type}")
                            api_post(f"/api/task/{task['id']}/update", {
                                "status": "failed", "progress": f"Unknown type: {task_type}",
                            })
                else:
                    print()
            else:
                print("✗ heartbeat failed")

        except KeyboardInterrupt:
            print("\nStopping.")
            break
        except Exception as e:
            print(f"✗ {e}")

        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
