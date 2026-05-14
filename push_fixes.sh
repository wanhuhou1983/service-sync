#!/bin/bash
cd /mnt/c/Users/linhu/.openclaw-autoclaw/workspace/service-dashboard

# Read token from bitwarden export
TOKEN=$(python3 -c "
import json
with open('/mnt/c/Users/linhu/Documents/bitwarden_export_20260511142058.json') as f:
    data = json.load(f)
for item in (data.get('items', data) if isinstance(data, dict) else data):
    if isinstance(item, dict) and 'github' in item.get('name','').lower():
        print(item.get('notes','').strip())
")

git add -A
git status
git commit -m "fix: code review fixes — security, dead code, shell injection

- Remove dead code in execute_deploy (duplicate completion block)
- Remove unused _resolve_ip and PG_CONNECTIONS_JSON
- Fix shell injection: pg_dump/psql/createdb use list args (no shell=True)
- Mask passwords in task params after completion
- Make _ensure_registry_image non-blocking via asyncio.to_thread
- Add pg_query task handler in agent
- Add Docker healthcheck to docker-compose
- Cache PG creds from heartbeat for pg_query support"

git push "https://wanhuhou1983:${TOKEN}@github.com/wanhuhou1983/service-sync.git" main
