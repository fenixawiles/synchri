import json, sys, re
R = sys.argv[1]
for l in open(f"{R}/requests.jsonl"):
    r = json.loads(l); b = r["body"] or {}
    if not isinstance(b, dict) or "input" not in b:
        print(r["method"], r["path"]); continue
    print("model:", b.get("model"), "| reasoning:", b.get("reasoning"), "| temperature:", b.get("temperature", "<absent>"),
          "| top_p:", b.get("top_p", "<absent>"), "| max_output_tokens:", b.get("max_output_tokens", "<absent>"), "| store:", b.get("store"))
    print("tools:", [t.get("name") or t.get("type") for t in b.get("tools") or []], "| tool_choice:", b.get("tool_choice"))
    print("client_metadata:", json.dumps(b.get("client_metadata"))[:200], "| prompt_cache_key:", b.get("prompt_cache_key"))
    text = json.dumps(b["input"]) + json.dumps(b.get("instructions", ""))
    print("input items:", len(b["input"]), "| instructions chars:", len(b.get("instructions") or ""), "| total chars:", len(text))
    for label, pat in [("cwd path", re.escape(R)), ("env canary", "CANARY-ENV"), ("date", r"current_date"), ("codex home path", "codexhome"),
                       ("skills", "skills_instructions"), ("permission profile", "permission_profile"), ("ZEBRA config", "ZEBRA")]:
        print(f"  {label}: {'YES' if re.search(pat, text) else 'no'}")
out = open(f"{R}/stdout.txt").read()
print("stdout event types:", sorted({json.loads(x).get('type') for x in out.splitlines() if x.startswith('{')}), "| 'model' key in any event:", '"model"' in out)
