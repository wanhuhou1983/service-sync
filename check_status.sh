#!/bin/bash
echo "=== Dashboard status ==="
sleep 3
curl -s http://127.0.0.1:8081/api/nodes | python3 -c '
import sys, json
nodes = json.load(sys.stdin)
for n in nodes:
    print(f"{n[\"node_name\"]:15} {n[\"status\"]:8} {n.get(\"pg_user\",\"\")} containers={n.get(\"containers\",\"?\")} images={n.get(\"images\",\"?\")}")
'
echo ""
echo "=== PG config check ==="
curl -s http://127.0.0.1:8081/api/nodes | python3 -c '
import sys, json
nodes = json.load(sys.stdin)
for n in nodes:
    has_pw = bool(n.get("pg_password",""))
    print(f"{n[\"node_name\"]:15} pg_host={n.get(\"pg_host\",\"\")} pg_user={n.get(\"pg_user\",\"\")} has_pw={has_pw}")
'
