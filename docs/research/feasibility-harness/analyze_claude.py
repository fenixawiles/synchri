import json, sys, re
R = sys.argv[1]
reqs = [json.loads(l) for l in open(f"{R}/requests.jsonl")]
for i, r in enumerate(reqs):
    b = r["body"] or {}
    if not isinstance(b, dict) or "messages" not in b:
        print(i, r["method"], r["path"]); continue
    sysblocks = b.get("system") or []
    systext = "\n".join(x.get("text", "") for x in sysblocks) if isinstance(sysblocks, list) else str(sysblocks)
    msgs = json.dumps(b["messages"])
    print(f"--- request {i}: {r['path']}")
    print("model:", b.get("model"), "| max_tokens:", b.get("max_tokens"), "| temperature:", b.get("temperature", "<absent>"),
          "| top_p:", b.get("top_p", "<absent>"), "| top_k:", b.get("top_k", "<absent>"))
    print("thinking:", b.get("thinking"), "| output_config:", b.get("output_config"), "| context_management:", json.dumps(b.get("context_management"))[:200])
    print("metadata:", json.dumps(b.get("metadata"))[:300])
    print("tools:", [t.get("name") for t in b.get("tools") or []])
    print("system chars:", len(systext), "| messages chars:", len(msgs))
    hay = systext + msgs
    for label, pat in [("cwd path", re.escape(R)), ("env canary", "CANARY-ENV"), ("date", r"20\d\d-\d\d-\d\d"),
                       ("platform", r"[Pp]latform"), ("git", r"[Gg]it repo|gitStatus|git status"), ("model id in prompt", r"claude-[a-z]+-\d"),
                       ("CLAUDE.md content", "ZEBRA"), ("memory", r"auto[- ]?memory|MEMORY\.md")]:
        m = re.search(pat, hay)
        print(f"  {label}: {'YES -> ' + hay[max(0,m.start()-60):m.end()+60].replace(chr(10),' ') if m else 'no'}")
    print("headers of interest:", {k: v for k, v in r["headers"].items() if k.lower().startswith(("anthropic", "x-claude", "user-agent", "x-app"))})
out = open(f"{R}/stdout.txt").read().splitlines()
for line in out:
    try: d = json.loads(line)
    except ValueError: continue
    if d.get("type") == "system" and d.get("subtype") == "init":
        print("STREAM init model:", d.get("model"), "| tools:", d.get("tools"), "| mcp_servers:", d.get("mcp_servers"), "| permissionMode:", d.get("permissionMode"))
    if d.get("type") == "assistant":
        print("STREAM assistant message.model:", (d.get("message") or {}).get("model"))
    if d.get("type") == "result":
        print("STREAM result: subtype", d.get("subtype"), "| is_error", d.get("is_error"), "| result", repr(d.get("result"))[:120])
        print("   usage", json.dumps(d.get("usage"))[:300]); print("   modelUsage", json.dumps(d.get("modelUsage"))[:300]); print("   total_cost_usd", d.get("total_cost_usd"))
