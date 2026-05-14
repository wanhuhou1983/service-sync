"""Service Dashboard - Main app with push & PG sync routes."""

import asyncio
import json
import os
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from app.models import (
    save_heartbeat, get_all_nodes, get_node,
    get_services_overview, get_node_containers, get_node_images,
    create_task, update_task, get_task, get_recent_tasks,
    update_pg_config, get_pg_instances, add_pg_instance,
    get_container_run_config,
)

app = FastAPI(title="Service Dashboard", version="2.0.0")

templates_dir = Path(__file__).parent / "templates"
static_dir = Path(__file__).parent / "static"
templates = Jinja2Templates(directory=str(templates_dir))
if static_dir.exists():
    app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")


# ─── API: Heartbeat ────────────────────────────────────

class HeartbeatPayload(BaseModel):
    node_name: str
    node_ip: str = ""
    hostname: str = ""
    platform: str = ""
    docker_version: str = ""
    containers: list = []
    images: list = []
    timestamp: str = ""


@app.post("/api/heartbeat")
async def api_heartbeat(payload: HeartbeatPayload):
    result = save_heartbeat(payload.model_dump())
    return result


# ─── Registry Helper ───────────────────────────────────

REGISTRY_URL = os.environ.get("REGISTRY_URL", "100.96.28.120:5000")

def _ensure_registry_image_sync(img_tag: str, svc_name: str) -> str:
    """Push image to local registry if not already there. Returns registry-qualified tag."""
    if not img_tag:
        return img_tag
    # Already in our registry
    if img_tag.startswith(REGISTRY_URL):
        return img_tag
    try:
        import docker
        client = docker.from_env()
        # Check if image exists locally
        try:
            img = client.images.get(img_tag)
            registry_tag = f"{REGISTRY_URL}/{img_tag}"
            # Tag and push
            img.tag(registry_tag)
            client.images.push(registry_tag)
            print(f"  Pushed {img_tag} → {registry_tag}")
            return registry_tag
        except docker.errors.ImageNotFound:
            print(f"  Image {img_tag} not found locally, using original tag")
    except Exception as e:
        print(f"  Warning: registry push failed: {e}")
    return img_tag


async def _ensure_registry_image(img_tag: str, svc_name: str) -> str:
    """Async wrapper — runs registry push in thread pool to avoid blocking."""
    return await asyncio.to_thread(_ensure_registry_image_sync, img_tag, svc_name)


# ─── API: Deploy/Push ──────────────────────────────────

@app.post("/api/deploy")
async def api_deploy(request: Request):
    """Create a deploy task. Auto-push image to registry if needed."""
    data = await request.json()
    target_node = data.get("target_node")
    service_name = data.get("service_name")
    image_tag = data.get("image_tag", "")
    source_node = data.get("source_node", "")

    if not all([target_node, service_name]):
        raise HTTPException(400, "Missing: target_node, service_name")

    # Ensure image is in our registry (non-blocking)
    final_tag = await _ensure_registry_image(image_tag, service_name)

    # Fetch source container config for container creation on target
    run_config = get_container_run_config(source_node, service_name) if source_node else {}

    params = {
        "image_tag": final_tag,
        "service_name": service_name,
        "run_config": run_config,
    }

    task = create_task(
        task_type="deploy",
        target_node=target_node,
        source_node=source_node,
        service_name=service_name,
        params=params,
    )
    return task


@app.get("/api/tasks/recent")
async def api_tasks_recent(limit: int = 20):
    return get_recent_tasks(limit)


@app.get("/api/tasks/{task_id}")
async def api_task_status(task_id: int):
    task = get_task(task_id)
    if not task:
        raise HTTPException(404, "Task not found")
    return task


@app.post("/api/task/{task_id}/update")
async def api_task_update(task_id: int, request: Request):
    """Agent reports task progress/completion."""
    data = await request.json()
    update_task(
        task_id,
        data.get("status", "running"),
        data.get("progress", ""),
        data.get("result", ""),
    )
    return {"status": "ok"}


# ─── API: PG Sync ──────────────────────────────────────

@app.post("/api/pg-sync")
async def api_pg_sync(request: Request):
    """Create a PG sync task between two nodes."""
    data = await request.json()
    source_node = data.get("source_node", "")
    target_node = data.get("target_node")
    db_name = data.get("db_name", "")
    sync_mode = data.get("mode", "schema")  # schema | data | full
    source_pg_name = data.get("source_pg_name", db_name)
    target_pg_name = data.get("target_pg_name", db_name)

    if not target_node:
        raise HTTPException(400, "Missing: target_node")

    # Fetch PG connection info for source and target nodes
    source_info = get_node(source_node) or {}
    target_info = get_node(target_node) or {}

    # PG credentials passed in task params for agent execution.
    # Passwords are cleared from params when task completes (see update_task).
    params = {
        "source_node": source_node,
        "db_name": db_name,
        "mode": sync_mode,
        "source_pg_name": source_pg_name,
        "target_pg_name": target_pg_name,
        "source_host": source_info.get("pg_host") or source_info.get("node_ip", ""),
        "source_port": source_info.get("pg_port", "5432"),
        "source_user": source_info.get("pg_user", "postgres"),
        "source_password": source_info.get("pg_password", ""),
        "target_host": target_info.get("pg_host") or "localhost",
        "target_port": target_info.get("pg_port", "5432"),
        "target_user": target_info.get("pg_user", "postgres"),
        "target_password": target_info.get("pg_password", ""),
    }

    task = create_task(
        task_type="pg_sync",
        target_node=target_node,
        source_node=source_node,
        service_name=f"pg-{db_name}",
        params=params,
    )
    return task


@app.post("/api/pg-query")
async def api_pg_query(request: Request):
    """Execute a SQL query on a node's PG and return results. SELECT only."""
    data = await request.json()
    node_name = data.get("node_name")
    db_name = data.get("db_name", "postgres")
    query = data.get("query", "").strip()

    if not all([node_name, query]):
        raise HTTPException(400, "Missing: node_name, query")

    # Security: only allow SELECT statements
    first_word = query.split()[0].upper() if query.split() else ""
    if first_word not in ("SELECT", "WITH", "EXPLAIN"):
        raise HTTPException(403, "Only SELECT/WITH/EXPLAIN queries are allowed")

    task = create_task(
        task_type="pg_query",
        target_node=node_name,
        service_name=f"pg-{db_name}",
        params={"db_name": db_name, "query": query, "need_result": True},
    )
    return task


@app.get("/api/pg-instances")
async def api_pg_instances(node_name: str = ""):
    return get_pg_instances(node_name)


@app.post("/api/pg-instances")
async def api_add_pg_instance(request: Request):
    data = await request.json()
    add_pg_instance(
        node_name=data["node_name"],
        name=data["name"],
        db_name=data.get("db_name", ""),
        host=data.get("host", ""),
        port=data.get("port", "5432"),
        user=data.get("user", "postgres"),
        password=data.get("password", ""),
    )
    return {"status": "ok"}


# ─── API: Misc ─────────────────────────────────────────

@app.get("/api/nodes")
async def api_nodes():
    return get_all_nodes()


@app.get("/api/overview")
async def api_overview():
    return get_services_overview()


@app.get("/api/node/{node_name}")
async def api_node_detail(node_name: str):
    containers = get_node_containers(node_name)
    images = get_node_images(node_name)
    return {"node_name": node_name, "containers": containers, "images": images}


# ─── Web UI ────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    nodes = get_all_nodes()
    overview = get_services_overview()
    tasks = get_recent_tasks(limit=10)
    services_json = json.dumps(overview["services"], ensure_ascii=False)
    node_names_json = json.dumps(overview["nodes"], ensure_ascii=False)
    return templates.TemplateResponse(request, "index.html", {
        "nodes": nodes,
        "services": overview["services"],
        "services_json": services_json,
        "node_names": overview["nodes"],
        "node_names_json": node_names_json,
        "node_status": overview["node_status"],
        "tasks": tasks,
    })


@app.get("/tasks", response_class=HTMLResponse)
async def tasks_page(request: Request):
    tasks = get_recent_tasks(limit=50)
    return templates.TemplateResponse(request, "tasks.html", {
        "tasks": tasks,
    })


@app.get("/pg", response_class=HTMLResponse)
async def pg_page(request: Request):
    nodes = get_all_nodes()
    instances = get_pg_instances()
    return templates.TemplateResponse(request, "pg.html", {
        "nodes": nodes,
        "instances": instances,
    })


@app.get("/node/{node_name}", response_class=HTMLResponse)
async def node_detail(request: Request, node_name: str):
    nodes = get_all_nodes()
    containers = get_node_containers(node_name)
    images = get_node_images(node_name)
    node_info = get_node(node_name)
    pg_instances = get_pg_instances(node_name)
    return templates.TemplateResponse(request, "node.html", {
        "node_name": node_name,
        "node": node_info,
        "containers": containers,
        "images": images,
        "pg_instances": pg_instances,
    })


@app.get("/health")
async def health():
    return {"status": "ok", "time": datetime.utcnow().isoformat()}
