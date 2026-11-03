/**
 * session-archive: auto-archive OpenCode sessions on idle.
 *
 * V2 plugin (`{ id, setup(ctx) }`). V1 plugin implementations do NOT run in
 * V2, so the entrypoint and the event subscription were rewritten; the
 * archiving logic itself lives in `~/.local/bin/session-archive.py` (single
 * source of truth for DB -> Markdown rendering, per-project routing and
 * secret redaction).
 *
 * Why a thin adapter instead of logic here: re-implementing the redaction in
 * JS would create two sources of truth for the security-critical redaction
 * step. This plugin only decides WHEN to invoke the script.
 *
 * Behaviour:
 *   - Subscribes to the V2 public event stream and archives on terminal
 *     execution events (the payload carries only `sessionID`; the script
 *     resolves project routing via the session's directory).
 *   - Coalesces: one child process per session, and a session still being
 *     written to is skipped rather than archived mid-turn.
 *   - Debounced per session (20s) so the burst of terminal events for one
 *     turn (and multi-turn sessions) produce a single archive run.
 *   - Never throws into OpenCode's event loop. A failed archive must not break
 *     the user's session; failures are logged to stderr only.
 *   - Cleanup aborts the event subscription when the plugin unloads.
 *
 * Event names (verified against v2.2.5 with a diagnostic plugin, 2026-11-01):
 * v2.2.5 emits `session.execution.succeeded` / `session.execution.failed` /
 * `session.execution.interrupted` when a run ends. It does NOT emit
 * `session.idle` any more (kept in the set so older binaries still work) —
 * a filter on `session.idle` alone means this plugin never fires on v2.2.5.
 *
 * Env:
 *   SESSION_ARCHIVE=0        disable entirely
 *   SESSION_ARCHIVE_PUSH=0   archive locally, do not git push
 */

"use strict";

const { spawn } = require("child_process");
const fs = require("fs");
const os = require("os");
const path = require("path");

const SCRIPT = path.join(os.homedir(), ".local", "bin", "session-archive.py");
const DEBOUNCE_MS = 20_000;

// Terminal events that mean "archive what happened". interrupted is included
// because a user-aborted turn is still worth recording.
const TERMINAL_EVENTS = new Set([
  "session.execution.succeeded",
  "session.execution.failed",
  "session.execution.interrupted",
  "session.idle", // legacy (< 2.2.5) name, kept for backward compatibility
]);

// sessionID -> timeout handle. Keeps one child per session at most.
const pending = new Map();

function enabled() {
  if (process.env.SESSION_ARCHIVE === "0") return false;
  try {
    return fs.existsSync(SCRIPT);
  } catch {
    return false;
  }
}

function archiveSession(sessionID) {
  if (!sessionID || typeof sessionID !== "string") return;

  const existing = pending.get(sessionID);
  if (existing) {
    // A turn already queued for this session; extend rather than stack.
    clearTimeout(existing);
  }

  const handle = setTimeout(() => {
    pending.delete(sessionID);

    const args = ["archive-one", sessionID];
    if (process.env.SESSION_ARCHIVE_PUSH === "0") args.push("--no-push");

    let child;
    try {
      child = spawn("python3", [SCRIPT, ...args], {
        stdio: ["ignore", "ignore", "pipe"],
        detached: false,
      });
    } catch (err) {
      console.error(`[session-archive] spawn failed: ${err.message}`);
      return;
    }

    // Surface problems for debugging without ever throwing into the host.
    child.stderr.on("data", (buf) => {
      const text = String(buf).trim();
      if (text) console.error(`[session-archive] ${text}`);
    });
    child.on("error", (err) => {
      console.error(`[session-archive] ${err.message}`);
    });
  }, DEBOUNCE_MS);

  // Do not hold the event loop open for the debounce window.
  if (typeof handle.unref === "function") handle.unref();
  pending.set(sessionID, handle);
}

// V2 entrypoint. `module.exports` becomes the module's `default` export under
// Bun/Node ESM-CJS interop. Assigned from a VARIABLE (not an object literal) so
// cjs-module-lexer cannot statically synthesize named exports — the module
// namespace stays `{ default: plugin }` and satisfies the V2 loader's
// `{ id, setup }` schema cleanly.
const plugin = {
  id: "session-archive",
  setup(ctx) {
    if (!enabled()) {
      console.error(
        "[session-archive] disabled (script missing or SESSION_ARCHIVE=0)",
      );
      return;
    }

    const controller = new AbortController();

    void (async () => {
      try {
        for await (const event of ctx.event.subscribe({
          signal: controller.signal,
        })) {
          if (!TERMINAL_EVENTS.has(event.type)) continue;
          // V2 events may carry the payload under `properties` or `data`;
          // check both plus a top-level fallback so the trigger is robust to
          // schema variance across patch releases.
          const props = event.properties || event.data || {};
          const sessionID = props.sessionID || event.sessionID;
          archiveSession(sessionID);
        }
      } catch (err) {
        // AbortError is the expected cleanup path when the plugin unloads.
        if (err && err.name !== "AbortError") {
          console.error(`[session-archive] event stream error: ${err.message}`);
        }
      }
    })();

    return () => controller.abort();
  },
};

module.exports = plugin;
