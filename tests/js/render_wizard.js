/*
 * Executes the app's real JavaScript against a deliberately permissive DOM
 * shim, and renders every wizard step and dashboard tab.
 *
 * What this catches: exceptions thrown while rendering — reference errors,
 * typos, and misused return values. The bug that motivated it was
 * `box.append(x).lastChild`, where `Node.append()` returns undefined; the shim
 * reproduces that faithfully, so the same mistake fails here.
 *
 * What it does NOT catch: markup correctness, selector accuracy, styling, or
 * layout. Queries return permissive stand-ins rather than parsing HTML, so a
 * wrong selector passes here and only a browser would find it.
 */
const fs = require("fs");
const path = require("path");

class El {
  constructor(tag = "div") {
    this.tagName = tag;
    this.children = [];
    this._html = "";
    this.style = {};
    this.dataset = {};
    this.value = "";
    this.disabled = false;
    this.className = "";
    this.textContent = "";
    this.classList = { add() {}, remove() {}, toggle() {} };
  }
  set innerHTML(v) { this._html = String(v); }
  get innerHTML() { return this._html; }
  // Faithful: the real Node.append() returns undefined.
  append(...nodes) { for (const n of nodes) if (n) this.children.push(n); }
  appendChild(n) { this.children.push(n); return n; }
  removeChild(n) { this.children = this.children.filter((c) => c !== n); }
  remove() {}
  addEventListener() {}
  removeEventListener() {}
  setAttribute() {}
  focus() {}
  get firstChild() { return this.children[0] || null; }
  get lastChild() { return this.children[this.children.length - 1] || null; }
  // Permissive: always resolves, so a wrong selector does not fail here.
  querySelector() { return new El(); }
  querySelectorAll() { return [new El(), new El(), new El(), new El()]; }
}

const template = new El("template");
Object.defineProperty(template, "content", { get: () => ({ firstChild: new El() }) });

global.document = {
  createElement: (tag) => (tag === "template" ? new El("template") : new El(tag)),
  getElementById: () => new El(),
  querySelector: () => new El(),
  querySelectorAll: () => [],
  body: new El("body"),
};
// $() builds a template, sets innerHTML, and takes content.firstChild.
global.document.createElement = (tag) => {
  const el = new El(tag);
  if (tag === "template") {
    Object.defineProperty(el, "content", { get: () => ({ firstChild: new El() }) });
  }
  return el;
};
global.window = global;
// Serves the same shapes the real API does, so the script's own startup
// (home() runs on load) populates S with realistic data instead of {}.
global.fetch = async (url) => {
  const route = String(url).replace("/api/", "").split("?")[0];
  const body = ({
    bootstrap: () => boot,
    repositories: () => ({ local: [{ name: "r", path: "/p", branch: "main", dirty: false }],
                           github: [], github_available: false, cwd: "/p" }),
    draft: () => draft,
    "draft/reset": () => draft,
    dashboard: () => dash,
    sessions: () => ({ sessions: boot.sessions }),
    session: () => dash.session,
    conversation: () => ({ messages: [{ seq: 1, sender: "claude", target: null,
                                        timestamp: "2026-08-12T01:02:03.000Z",
                                        message_type: "chat", content: "hi" }] }),
    gates: () => ({ gates: [{ gate_id: "AUTH-01", status: "pending", description: "login",
                              evidence: [], tests: [], commits: [], satisfied: false }],
                    summary: { satisfied: 0, required: 1, blockers: [] } }),
    diff: () => ({ diff: "diff --git a b" }),
    memory: () => ({ markdown: "## Goal\n\nship it" }),
    events: () => ({ events: [{ seq: 1, created_at: "t", event_type: "session.created",
                                actor_name: "synchri", payload: {} }] }),
    contract: () => ({ revision: 1, short_digest: "abc", core_text: "CONTRACT",
                       acknowledgments: dash.acknowledgments }),
    changes: () => dash.changes,
  }[route] || (() => ({})))();
  return { ok: true, status: 200, json: async () => body };
};
global.EventSource = class { constructor() {} addEventListener() {} close() {} };
global.confirm = () => true;
global.alert = () => {};
global.setTimeout = (fn) => 0;
global.clearTimeout = () => {};
global.setInterval = () => 0;
global.clearInterval = () => {};

// --- fixtures (defined before the script loads; see the fetch stub) --------
const FIXTURES_BELOW = true;

const boot = {
  modes: [
    { mode: "interactive", label: "Interactive", summary: "s" },
    { mode: "long_horizon", label: "Long Horizon", summary: "s" },
    { mode: "review_audit", label: "Review", summary: "s" },
  ],
  runtimes: [{ key: "claude_code", label: "Claude Code" }, { key: "codex", label: "Codex" }],
  roles: [{ key: "primary_builder", label: "Primary Builder" },
          { key: "adversarial_reviewer", label: "Adversarial Reviewer" }],
  permissions: [{ group: "Repository", capabilities: [
    { key: "repo.edit", label: "Edit files", description: "d", risk: "medium", default: "allow" },
    { key: "git.push", label: "Push branches", description: "d", risk: "high", default: "deny" },
  ] }],
  sessions: [],
  workspace: "/tmp/ws",
};

const draft = {
  draft: {
    mode: "long_horizon", repo_path: "/tmp/repo", base_branch: "main", worktree_name: null,
    agents: [{ name: "claude", runtime: "claude_code", role: "primary_builder",
               role_label: "Primary Builder" }],
    permissions: { "repo.edit": "allow", "git.push": "deny" },
    spec: "# Spec\n\n## Acceptance\n- AUTH-01 login", deadline: null, name: "s",
  },
  steps: [
    { key: "mode", label: "Mode" }, { key: "repository", label: "Repository" },
    { key: "worktree", label: "Worktree" }, { key: "agents", label: "Agents" },
    { key: "permissions", label: "Permissions" }, { key: "spec", label: "Spec" },
    { key: "deadline", label: "Deadline" }, { key: "review", label: "Review" },
  ],
  summary: {
    mode: "Long Horizon", mode_summary: "s",
    repository: { name: "repo", path: "/tmp/repo", branch: "main", dirty: true },
    worktree: { name: "synchri-lh-a-b-1234", explanation: "e" },
    agents: [{ name: "claude", role_label: "Primary Builder", runtime: "claude_code" }],
    permissions: boot.permissions.map((g) => ({
      ...g, capabilities: g.capabilities.map((c) => ({ ...c, decision: c.default })) })),
    spec: "Loaded — 5 words", deadline: "10 hours", escalation: ["a", "b"],
    warnings: ["w"], problems: [], ready: true,
  },
  detected_gates: [{ gate_id: "AUTH-01", description: "login" }],
  gate_note: "Detected 1", problems: [], ready: true,
  repo_status: { name: "repo", root: "/tmp/repo", branch: "main", is_dirty: true,
                 dirty_files: ["a.txt"], branches: ["main", "dev"], remote: null },
  draft_id: "default", version: 1,
};

const dash = {
  session: {
    name: "s", mode_label: "Long Horizon", status: "active",
    repository: { name: "repo", root: "/tmp/repo" },
    worktree: { name: "wt", branch: "b", base_branch: "main", path: "/p", repo_root: "/r" },
    contract_revision: 1,
  },
  status: "active", time_remaining: "09:00:00", phase: "exploration",
  current_actor: "claude",
  gates: { summary: { satisfied: 1, required: 2, blockers: [] }, items: [] },
  tests: { passed: 3, failed: 0, command: "pytest", exit_code: 0, output_tail: "ok" },
  test_command: "pytest",
  changes: { commits: 2, files_changed: 1, insertions: 3, deletions: 0, recent: [] },
  open_blockers: [], open_escalations: [],
  acknowledgments: { expected: ["claude"], accepted: [], conflicts: [], waiting: ["claude"],
                     all_accepted: false },
  user_intervention_required: true, worktree_present: true,
};


const html = fs.readFileSync(path.join(__dirname, "..", "..", "synchri", "ui", "static", "app.html"), "utf8");
const script = html.split("<script>")[1].split("</" + "script>")[0];

// Evaluate the page's script, then hand back its top-level functions.
const load = new Function(
  script + "\n;return {renderWizard, renderSession, paint, S, home, newSession, stepAgents};"
);
const app = load();

async function main() {
  const failures = [];
  Object.assign(app.S, { boot, draft, dash, session: "sess_x", view: "wizard" });

  for (const step of draft.steps.map((s) => s.key)) {
    app.S.step = step;
    app.S.repos = { local: [{ name: "r", path: "/p", branch: "main", dirty: false }],
                    github: [], github_available: false, cwd: "/p" };
    try {
      await app.renderWizard();
      // renderWizard deliberately catches a broken step so the page does not
      // blank; the failure is recorded instead, and that is what we check.
      if (app.S.stepError) failures.push(`wizard step ${app.S.stepError}`);
    } catch (e) {
      failures.push(`wizard step '${step}' threw: ${e.message}`);
    }
  }

  app.S.view = "session";
  try {
    app.renderSession();
  } catch (e) {
    failures.push(`dashboard: ${e.message}`);
  }

  if (failures.length) {
    console.error(failures.join("\n"));
    process.exit(1);
  }
  console.log("ok");
}
main();
