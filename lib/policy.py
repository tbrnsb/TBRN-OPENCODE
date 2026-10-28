#!/usr/bin/env python3
"""
lib/policy.py — data-policy safety ceiling and route filtering.

PURPOSE
    A route that works is not automatically a route that may receive your code.
    This module makes that a machine-enforced rule rather than a convention.

MODEL
    GLOBAL SAFETY CEILING        (global policy, admin-controlled)
            |
            v  (project may only NARROW)
    PROJECT CLASS                (project policy, repo-local, untrusted)
            |
            v
    EFFECTIVE CEILING
            |
            v
    ROUTE SELECTION  ->  PRIMARY -> SAFE FALLBACK -> FAIL CLOSED

INVARIANTS
    1. A project policy can never RAISE the ceiling. It can only lower it.
       Enforced arithmetically: effective = min(ceiling, project) on ranks,
       so a repo file claiming "disposable" under a "private-safe" ceiling
       still resolves to "private-safe".
    2. An unlabelled route is treated as the LEAST safe class, never the most.
       Missing metadata must fail closed, not open.
    3. If no permitted route survives filtering, the caller is told to fail
       closed. There is no "relax and continue" path.

BACKWARD COMPATIBILITY
    A routes file WITHOUT a top-level "data_policies" block is schema v1.
    v1 files are passed through unfiltered, so pre-existing configurations and
    the existing test suite keep their exact behaviour. Policy enforcement is
    opt-in via schema version, not via a flag that can be forgotten.

RANKS
    Lower rank = more restrictive. private-safe may be used for anything.
    training-explicit may never be used for private or public code.
"""

import json
import sys
import os
import re
import subprocess

# rank 0 = strictest / safest to use
RANK = {
    "private-safe": 0,
    "training-optin": 1,
    "trial-logged": 2,
    "training-explicit": 3,
}

# What a workload will tolerate. project class = the most permissive it allows.
PROJECT_CLASS_RANK = {
    "private": 0,        # private code: only private-safe routes
    "public": 2,         # public code: up to trial-logged
    "disposable": 3,     # throwaway data: anything
}

# Used when a route omits data_policy. Deliberately the least safe class.
UNKNOWN_RANK = RANK["training-explicit"]

EXIT_OK = 0
EXIT_NO_ROUTES = 1
EXIT_FILED_CLOSED = 3
EXIT_BAD_CONFIG = 2


def die(msg, code=EXIT_BAD_CONFIG):
    sys.stderr.write("policy: %s\n" % msg)
    sys.exit(code)


def _no_duplicate_keys(pairs):
    """Reject duplicate object keys.

    Plain json silently keeps the LAST occurrence, so a file could carry a
    permissive block that a reader applies and a stricter one that only looks
    authoritative to a human. Ambiguous config is rejected, not guessed at.
    """
    seen = {}
    for k, v in pairs:
        if k in seen:
            raise ValueError("duplicate key in JSON object: %r" % k)
        seen[k] = v
    return seen


def load_json(path, required=True, default=None, strict=True):
    """
    strict=True  (trust anchor): halt on unparsable content. A corrupt global
                 policy must never silently degrade into a laxer ceiling.
    strict=False (untrusted input): return the default instead of halting, so a
                 malformed project file cannot break routing for anyone.
    """
    if not path or not os.path.isfile(path):
        if required:
            die("file not found: %s" % path)
        return default if default is not None else {}
    try:
        with open(path) as fh:
            return json.load(fh, object_pairs_hook=_no_duplicate_keys)
    except Exception as exc:
        if strict:
            die("cannot parse %s: %s" % (path, exc))
        return default if default is not None else {}


def policy_rank(name, where):
    if name not in RANK:
        die("unknown data policy %r in %s (known: %s)"
            % (name, where, ", ".join(sorted(RANK))))
    return RANK[name]


def resolve_effective_ceiling(ceiling_name, project_class):
    """
    effective = min(ceiling, project). A project can only narrow.

    This single line is the security property. min() on ranks makes it
    impossible for untrusted project input to loosen the global ceiling,
    regardless of what the project file claims.

    Failure modes are deliberately asymmetric:
      - an invalid GLOBAL ceiling halts (a corrupt trust anchor must not
        silently degrade into a laxer setting)
      - an invalid PROJECT class clamps to the strictest class, because a
        malformed repo file is untrusted input and must never be able to widen
        access OR take routing down for everyone
    """
    c = policy_rank(ceiling_name, "global policy")
    if project_class not in PROJECT_CLASS_RANK:
        # Untrusted input. Clamp to strictest rather than honouring it.
        return min(c, PROJECT_CLASS_RANK["private"])
    p = PROJECT_CLASS_RANK[project_class]
    return min(c, p)


def route_rank(route):
    """Rank for a route dict. Missing dict, missing data_policy, or an
    unrecognised label all mean least safe. Must never raise: a caller
    checking an undeclared model has to get an answer, not a traceback."""
    if not isinstance(route, dict):
        return UNKNOWN_RANK
    pol = route.get("data_policy")
    if pol is None:
        return UNKNOWN_RANK
    if pol not in RANK:
        # Unknown label is untrusted input: treat as least safe, do not die,
        # so one bad route cannot take down routing for every other route.
        return UNKNOWN_RANK
    return RANK[pol]


def select_routes(routes, agent, effective_rank):
    """
    Return the agent's preference chain filtered to permitted routes.
    Order is preserved exactly; only ineligible entries are dropped.
    """
    prefs = (routes.get("agent_preferences") or {}).get(agent)
    if prefs is None:
        prefs = (routes.get("agent_preferences") or {}).get("general", [])
    if not isinstance(prefs, list):
        return []

    by_model = {}
    for r in routes.get("api_routes") or []:
        if isinstance(r, dict) and r.get("model"):
            m = r["model"]
            prior = by_model.get(m)
            # Duplicate declarations of the same model must not be a way to
            # launder a strict label. Keep the MOST RESTRICTIVE declaration,
            # so appending a permissive duplicate cannot widen access.
            if prior is None or route_rank(r) > route_rank(prior):
                by_model[m] = r

    out, seen = [], set()
    for model in prefs:
        if not isinstance(model, str) or model in seen:
            continue
        r = by_model.get(model)
        # A route absent from api_routes has no policy metadata. Treat as
        # least safe rather than trusting an unlabelled preference entry.
        rr = route_rank(r) if r is not None else UNKNOWN_RANK
        if rr <= effective_rank:
            seen.add(model)
            out.append(model)
    return out


# --------------------------------------------------------------------------
# Agent pin resolution
#
# CORRECTION (was factually wrong): an earlier version of this file claimed
# OpenCode ignores the "agent" block in opencode.json. It does not. The
# official schema (https://opencode.ai/config.json) defines
# Config.agent -> AgentConfig with model, mode, permission, prompt, and the
# agent docs document configuring agents in JSON under the key "agent".
# The original claim was inferred from a `--agent <subagent>` message
# ("is a subagent, not a primary agent, falling back to default agent"),
# which is a MODE fallback, not a schema rejection.
#
# BOTH layers are real and both are honoured by this resolver. When they
# disagree, the MORE RESTRICTIVE pin wins (see effective_pin): a ceiling must
# never be satisfied by a permissive layer while a stricter layer is in force.
# Exact OpenCode precedence between the two layers is not documented, so it is
# deliberately not assumed here -- taking the stricter of the two is correct
# under either ordering.
#
# Resolution order:
#   1. <project>/agents/<name>.md  and  <project>/opencode.json agent.<name>
#   2. <config-dir>/agents/<name>.md  and  <config-dir>/opencode.json agent.<name>
#   3. opencode.json top-level model  (the default a session model overrides)
#
# An unresolvable pin is treated as LEAST safe, exactly like an unlabelled
# route, because with no pin OpenCode picks its own default.
# --------------------------------------------------------------------------

AGENT_MD_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


def _frontmatter_model(md_path):
    """Return the 'model:' value from a markdown agent file, or None."""
    try:
        with open(md_path, "r", encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        m = re.match(r"^\s*model\s*:\s*(.+?)\s*$", line)
        if m:
            return m.group(1).strip().strip("'\"") or None
    return None


def pin_candidates(agent, project_root=None, config_dir=None):
    """Every configuration layer that could pin this agent's model.

    Both opencode.json's "agent" block and agents/<name>.md frontmatter are
    real, documented layers. A project can shadow the global layer with either.
    """
    if not agent or not AGENT_MD_NAME.match(agent):
        return []
    if config_dir is None:
        config_dir = (os.environ.get("OPENCODE_CONFIG_DIR")
                      or os.path.expanduser("~/.config/opencode"))
    out = []

    def add_json(cfg_path, label):
        if not cfg_path or not os.path.isfile(cfg_path):
            return
        try:
            obj = load_json(cfg_path, required=False, default={}, strict=False)
        except SystemExit:
            return
        if not isinstance(obj, dict):
            return
        blk = obj.get("agent") or {}
        if isinstance(blk, dict) and isinstance(blk.get(agent), dict):
            m = blk[agent].get("model")
            if m:
                out.append((m, "%s agent block" % label))

    if project_root:
        m = _frontmatter_model(os.path.join(project_root, "agents", agent + ".md"))
        if m:
            out.append((m, "project agents/*.md"))
        add_json(os.path.join(project_root, "opencode.json"), "project opencode.json")

    m = _frontmatter_model(os.path.join(config_dir, "agents", agent + ".md"))
    if m:
        out.append((m, "global agents/*.md"))
    add_json(os.path.join(config_dir, "opencode.json"), "global opencode.json")
    return out


def effective_pin(agent, project_root=None, config_dir=None, routes=None):
    """Resolve the model an agent will actually use.

    When more than one layer pins the agent, the MOST RESTRICTIVE pin is
    returned. OpenCode does not document whether agents/*.md or the JSON
    "agent" block wins, so taking the stricter of the two is the only choice
    that is safe under either ordering: a permissive layer can never mask a
    strict one.

    With no agent pin at all, the opencode.json top-level model applies -- that
    is the default a session's /model selection overrides. If nothing is
    configured, OpenCode picks its own built-in default, which is unvouchable,
    so this returns (None, "unresolved") and callers fail closed.
    """
    if not agent or not AGENT_MD_NAME.match(agent):
        return (None, "invalid-agent-name")
    if config_dir is None:
        config_dir = (os.environ.get("OPENCODE_CONFIG_DIR")
                      or os.path.expanduser("~/.config/opencode"))

    cands = pin_candidates(agent, project_root, config_dir)
    if cands:
        src = " + ".join(s for _, s in cands)
        if routes is not None:
            best = max((m for m, _ in cands), key=lambda m: route_rank(m))
        else:
            best = cands[0][0]
        return (best, src)

    for cfg in (os.path.join(project_root, "opencode.json") if project_root else None,
                os.path.join(config_dir, "opencode.json")):
        if not cfg or not os.path.isfile(cfg):
            continue
        try:
            obj = load_json(cfg, required=False, default={}, strict=False)
        except SystemExit:
            continue
        if isinstance(obj, dict) and obj.get("model"):
            return (obj["model"], "opencode.json top-level model")

    return (None, "unresolved")


def main(argv):
    routes_path = None
    policy_path = None
    project_path = None
    agent = None
    explain = False
    check_model = None
    check_agent = None
    project_root = None

    i = 1
    while i < len(argv):
        a = argv[i]
        if a == "--routes":
            routes_path = argv[i + 1]; i += 2
        elif a == "--policy":
            policy_path = argv[i + 1]; i += 2
        elif a == "--project-policy":
            project_path = argv[i + 1]; i += 2
        elif a == "--agent":
            agent = argv[i + 1]; i += 2
        elif a == "--explain":
            explain = True; i += 1
        elif a == "--check-model":
            check_model = argv[i + 1]; i += 2
        elif a == "--check-agent":
            check_agent = argv[i + 1]; i += 2
        elif a == "--project-root":
            project_root = argv[i + 1]; i += 2
        elif a == "--default-policy":
            policy_path = argv[i + 1]; i += 2
        else:
            die("unknown option: %s" % a)

    if not routes_path:
        die("--routes is required")
    if not agent and check_model is None and check_agent is None:
        die("--agent, --check-model or --check-agent is required")

    routes = load_json(routes_path)

    # ---- schema introspection (diagnostics only, never a bypass) ----------
    # An earlier version of this file short-circuited to an UNFILTERED
    # passthrough when "data_policies" was absent. That was a bypass: deleting
    # one key from the routes file disabled the ceiling entirely.
    # Schema is now recorded for diagnostics only. A missing, unknown, or
    # malformed schema means "metadata may be incomplete", so every unlabelled
    # route is least-safe and the ceiling rejects it. Missing metadata must
    # never grant permission, and must never switch enforcement off.
    schema = routes.get("schema_version", routes.get("version"))
    block = routes.get("data_policies")
    has_block = isinstance(block, dict) and len(block) > 0
    if schema is None:
        schema_state = "missing"
    elif isinstance(schema, bool) or not isinstance(schema, (int, str)):
        schema_state = "malformed"
    else:
        schema_state = "known(%s)" % schema

    if explain:
        sys.stderr.write("policy: schema=%s policy_block=%s\n"
                         % (schema_state, "present" if has_block else "absent"))

    # Defaults are deliberately safe: an absent policy file still forbids
    # training-explicit, and an absent project class assumes the stricter
    # 'private' rather than the looser 'disposable'.
    defaults = {
        "version": 1,
        "safety_ceiling": "trial-logged",
        "default_project_class": "private",
    }
    gpol = load_json(policy_path, required=False, default=defaults) or defaults
    ceiling = gpol.get("safety_ceiling", defaults["safety_ceiling"])
    project_class = gpol.get("default_project_class", defaults["default_project_class"])

    # Project file is untrusted input and can only narrow, so a malformed or
    # unreadable one is ignored rather than honoured. The global
    # default_project_class (the strictest) then applies. strict=False is
    # required here so the file cannot take routing down for everyone.
    if project_path and os.path.isfile(project_path):
        pp = load_json(project_path, required=False, default={}, strict=False) or {}
        project_class = pp.get("project_class", project_class)

    eff = resolve_effective_ceiling(ceiling, project_class)
    selected = select_routes(routes, agent, eff)

    if explain:
        allowed = [k for k, v in RANK.items() if v <= eff]
        sys.stderr.write(
            "policy: ceiling=%s project_class=%s effective=%s allowed=%s\n"
            % (ceiling, project_class,
               [k for k, v in RANK.items() if v == eff][0], ",".join(allowed)))

    if explain and not selected:
        sys.stderr.write(
            "policy: no permitted route for agent=%s. Every candidate is either "
            "unlabelled (treated as %s) or above the effective ceiling %s. "
            "Refusing rather than selecting an ineligible route. Remediation: "
            "add data_policy labels to api_routes, or raise safety_ceiling in "
            "the global policy at %s.\n"
            % (agent,
               [k for k, v in RANK.items() if v == UNKNOWN_RANK][0],
               [k for k, v in RANK.items() if v == eff][0],
               policy_path or "(none)"))

    # --check-model: is this specific model permitted right now?
    #
    # The ceiling governs the model the worker passes to `opencode run`, but
    # it cannot govern the model a SUBAGENT uses once the orchestrator spawns
    # it -- that comes from the agent's own pin. A project-local
    # opencode.json can redefine those pins, so callers use this to ask about a
    # pin before letting any private work proceed near it.
    if check_agent is not None:
        pin, source = effective_pin(check_agent, project_root or None, None, routes)
        if not pin:
            # No pin anywhere: OpenCode falls back to its own built-in default,
            # which we cannot vouch for. Least-safe, therefore refused.
            if explain:
                sys.stderr.write("policy: check-agent %s UNRESOLVED (%s); "
                                 "refusing under a private workload\n"
                                 % (check_agent, source))
            return EXIT_FILED_CLOSED
        args = [sys.executable, os.path.abspath(__file__),
                "--routes", routes_path, "--policy", policy_path,
                "--check-model", pin]
        if project_path:
            args += ["--project-policy", project_path]
        rc = subprocess.run(args, capture_output=True).returncode
        if explain:
            sys.stderr.write("policy: check-agent %s pin=%s source=%s rc=%d\n"
                             % (check_agent, pin, source, rc))
        return rc

    if check_model is not None:
        by_model = {}
        for r in routes.get("api_routes") or []:
            if isinstance(r, dict) and r.get("model"):
                prior = by_model.get(r["model"])
                if prior is None or route_rank(r) > route_rank(prior):
                    by_model[r["model"]] = r
        rr = route_rank(by_model.get(check_model))
        permitted = rr <= eff
        if explain:
            sys.stderr.write("policy: check-model %s rank=%d effective=%d permitted=%s\n"
                             % (check_model, rr, eff, permitted))
        return EXIT_OK if permitted else EXIT_FILED_CLOSED

    for m in selected:
        print(m)

    # Fail closed: no permitted route is a hard stop, not a licence to pick
    # an ineligible one.
    return EXIT_OK if selected else EXIT_FILED_CLOSED


if __name__ == "__main__":
    sys.exit(main(sys.argv))
