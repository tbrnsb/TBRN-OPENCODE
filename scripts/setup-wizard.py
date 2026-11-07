#!/usr/bin/env python3
"""
TBRN-OPENCODE-V2 Setup Wizard — beautiful, intuitive, zero-friction.

Run:  python3 scripts/setup-wizard.py              # interactive menu (recommended)
      python3 scripts/setup-wizard.py --auto       # agent-driven: asks questions, configures everything
      python3 scripts/setup-wizard.py --quick      # 3-step quick setup
      python3 scripts/setup-wizard.py --validate   # check config
"""

import argparse
import json
import sys
import os
import textwrap
from pathlib import Path

# ─── Paths ──────────────────────────────────────────────────────────────
CFG_DIR     = Path.home() / ".config" / "opencode"
DB_DIR      = Path.home() / ".local" / "share" / "session-archive" / "sulav-global-sessions"
PROVIDER_F  = CFG_DIR / "provider-routes.json"
OPENCODE_J  = CFG_DIR / "opencode.json"
CHAINS_F    = CFG_DIR / "chains.json"
REPO_ROOT   = Path(__file__).parent.parent
SAMPLE_F    = CFG_DIR / "provider-routes.example.json"
SAMPLE_FALLBACK = REPO_ROOT / "config" / "provider-routes.example.json"

# ─── Role → Model Recommendations (the "secret sauce") ──────────────────
# These are the BEST models for each role. The auto-mode will pick these.
# Updated 2026-11-15 with latest available models
ROLE_RECOMMENDATIONS = {
    # role: (provider_key, model_id, why)
    "mastermind":  ("openrouter", "anthropic/claude-sonnet-latest", "Best planning & dispatching"),
    "planner":     ("openrouter", "anthropic/claude-sonnet-latest", "Best at structured reasoning"),
    "coder":       ("nvidia",     "nvidia/nemotron-3-ultra-550b-a55b", "Best coding & reasoning"),
    "tester":      ("nvidia",     "nvidia/nemotron-3.5-lightning-30b-a3b", "Fast, thorough validation"),
    "debugger":    ("nvidia",     "nvidia/nemotron-3-ultra-550b-a55b", "Deep root-cause analysis"),
    "critic":      ("openrouter", "google/gemini-3.8-flash",       "Sharp critique, different family"),
    "designer":    ("openrouter", "anthropic/claude-sonnet-latest", "Best UI/UX taste"),
    "copywriter":  ("openrouter", "openai/gpt-oss-120b",           "Natural, human-like writing"),
    "explorer":    ("groq",       "openai/gpt-oss-120b",           "Fast repo exploration"),
    "code-reviewer":("openrouter", "anthropic/claude-sonnet-latest", "Thorough code review"),
    "archiver":    ("groq",       "openai/gpt-oss-20b",            "Fast commits & logs"),
    "security-reviewer": ("openrouter", "anthropic/claude-sonnet-latest", "Security expertise"),
    "final-verifier":    ("nvidia",     "nvidia/nemotron-3-ultra-550b-a55b", "Final acceptance"),
    "ui-reviewer":       ("openrouter", "anthropic/claude-sonnet-latest", "UI critique"),
    "general":           ("openrouter", "google/gemini-3.8-flash",        "General purpose"),
}

# ─── Provider Presets ───────────────────────────────────────────────────
PRESETS = {
    "nvidia":     ("NVIDIA (Free Tier)",       "https://build.nvidia.com",
                   [("nvidia/nemotron-3-ultra-550b-a55b", "Nemotron 3 Ultra — best reasoning/coding ⭐"),
                    ("nvidia/nemotron-3.5-lightning-30b-a3b", "Nemotron 3.5 Lightning — fast, cheap"),
                    ("nvidia/nemotron-3.5-super-vl-120b-a12b", "Nemotron 3.5 Super VL — vision + reasoning")],
                   "nemotron", "private-safe"),
    "openrouter": ("OpenRouter (Many Models)",  "https://openrouter.ai/keys",
                   [("anthropic/claude-sonnet-latest", "Claude Sonnet — best all-rounder ⭐"),
                    ("google/gemini-3.8-flash",        "Gemini 3.8 Flash — fast, multimodal"),
                    ("openai/gpt-oss-120b",            "GPT-OSS 120B — open-weight, strong")],
                   "openrouter", "training-optin"),
    "groq":       ("Groq (Ultra-Fast)",         "https://console.groq.com/keys",
                   [("openai/gpt-oss-120b",       "GPT-OSS 120B — versatile, fast ⭐"),
                    ("openai/gpt-oss-20b",         "GPT-OSS 20B — lightning fast")],
                   "openrouter", "training-optin"),
    "anthropic":  ("Anthropic Direct",          "https://console.anthropic.com/settings/keys",
                   [("anthropic/claude-sonnet-latest", "Claude Sonnet — best all-rounder ⭐"),
                    ("anthropic/claude-haiku-latest",  "Claude Haiku — fast, cheap")],
                   "openrouter", "training-optin"),
    "custom":     ("Custom / Local (Ollama…)",  "",
                   [], "custom", "private-safe"),
}

# ─── Helpers ────────────────────────────────────────────────────────────
def jload(p, d=None):
    try: return json.loads(p.read_text())
    except: return d if d is not None else {}

def jsave(p, d): p.write_text(json.dumps(d, indent=2) + "\n")

def ensure(): 
    DB_DIR.mkdir(parents=True, exist_ok=True)
    CFG_DIR.mkdir(parents=True, exist_ok=True)

def hr(c="─"): print(c * 60)

def banner():
    print("\n" + "═" * 60)
    print("   ████████╗██╗  ██╗███████╗    ██╗  ██╗██║   ██╗██╗██████╗")
    print("   ╚══██╔══╝██║  ██║██╔════╝    ██║  ██║██║   ██║██║██╔══██╗")
    print("      ██║   ███████║█████╗      ███████║██║   ██║██║██████╔╝")
    print("      ██║   ██╔══██║██╔══╝      ██╔══██║╚██╗ ██╔╝██║██╔═══╝")
    print("      ██║   ██║  ██║███████╗    ██║  ██║ ╚████╔╝ ██║██║")
    print("      ╚═╝   ╚═╝  ╚═╝╚══════╝    ╚═╝  ╚═╝  ╚═══╝  ╚═╝╚═╝")
    print("                    V2  •  Setup Wizard")
    print("═" * 60)

def status():
    r = jload(PROVIDER_F, {}).get("api_routes", [])
    o = jload(OPENCODE_J)
    c = jload(CHAINS_F)
    print(f"\n  📁 Archive:   {DB_DIR}")
    print(f"  🔑 Providers: {len(r)} configured")
    print(f"  ⚙️  OpenCode:  {'v2 ✓' if o.get('plugins') else 'v1 ✗ (needs update)'}")
    print(f"  🚫 Explore:   {'disabled ✓' if o.get('agents',{}).get('explore',{}).get('disabled') else 'enabled ✗'}")
    print(f"  🎭 Roles:     {len(c.get('roles', {}))} roles in chains.json")
    hr()

def ask(prompt, default=None, required=False, hide=False):
    suf = f" [{default}]" if default else ""
    while True:
        try:
            import getpass
            val = getpass.getpass(f"  {prompt}{suf}: ") if hide else input(f"  {prompt}{suf}: ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n  cancelled"); return None
        if val: return val
        if default is not None: return default
        if not required: return None
        print("  This field is required.")

def pick(options, prompt="Select", back=True):
    for i, o in enumerate(options, 1):
        desc = f" — {o[2]}" if len(o) > 2 else ""
        print(f"  {i}) {o[0]}{desc}")
    if back: print("  0) Back")
    while True:
        try: c = int(input(f"  {prompt} (1-{len(options)}): ").strip())
        except: return None
        if c == 0: return None
        if 1 <= c <= len(options): return options[c-1]
        print(f"  Enter 1-{len(options)} or 0")

def yn(p, d=True): 
    return (input(f"  {p} [{'Y/n' if d else 'y/N'}]: ").strip().lower() or ('y' if d else 'n'))[0] == 'y'

def print_box(title, lines, width=56):
    print(f"\n  ┌{'─'*(width+2)}┐")
    print(f"  │ {title.center(width)} │")
    print(f"  ├{'─'*(width+2)}┤")
    for line in lines:
        print(f"  │ {line.ljust(width)} │")
    print(f"  └{'─'*(width+2)}┘")

# ─── AUTO-MODE: Agent asks questions, configures everything ─────────────
def do_auto():
    """Agent-driven setup: asks a few questions, picks best models for each role."""
    print_box("AUTO-CONFIGURE", [
        "I'll ask a few questions, then configure everything automatically.",
        "You'll get the BEST model for each role — no guessing needed."
    ])
    if not yn("\n  Start auto-config?"): return
    
    hr()
    print("  QUESTION 1 of 3")
    hr()
    print("  Which provider do you prefer for your PRIMARY models?")
    print("  (This will be used for most roles)\n")
    primary_opts = [
        ("openrouter", "OpenRouter", "One key → Claude, Gemini, GPT-OSS, etc. ⭐ Recommended"),
        ("nvidia",     "NVIDIA",     "Free tier, Nemotron models — great for coding"),
        ("anthropic",  "Anthropic",  "Direct Claude access — best quality, paid"),
        ("groq",       "Groq",       "Ultra-fast GPT-OSS — speed over quality"),
    ]
    prim = pick(primary_opts, "Choose provider")
    if not prim: return
    primary_key = prim[0]
    primary_pre = PRESETS[primary_key]
    
    hr()
    print("  QUESTION 2 of 3")
    hr()
    print("  Do you have a SECONDARY provider for fallback / variety?")
    print("  (e.g., Groq for speed, or NVIDIA for free tier)\n")
    secondary_opts = [o for o in primary_opts if o[0] != primary_key] + [("none", "None", "Skip secondary")]
    sec = pick(secondary_opts, "Choose provider")
    secondary_key = sec[0] if sec and sec[0] != "none" else None
    
    hr()
    print("  QUESTION 3 of 3")
    hr()
    print("  Enter your API keys (hidden input):\n")
    pkey = ask(f"{primary_pre[0]} API key", hide=True, required=True)
    if not pkey: return
    
    skey = None
    if secondary_key:
        spre = PRESETS[secondary_key]
        skey = ask(f"{spre[0]} API key", hide=True, required=True)
        if not skey: return
    
    # ─── BUILD COMPLETE CONFIG ──────────────────────────────────────────
    print("\n  Configuring all 15 roles with optimal models…")
    
    routes = []
    
    # Add primary provider routes for each role
    for role, (prov, model, why) in ROLE_RECOMMENDATIONS.items():
        if prov == primary_key:
            routes.append({
                "credential": pkey,
                "model": model,
                "family": PRESETS[prov][3],
                "data_policy": PRESETS[prov][4],
                "_role": role,
                "_why": why
            })
        elif prov == secondary_key and skey:
            routes.append({
                "credential": skey,
                "model": model,
                "family": PRESETS[prov][3],
                "data_policy": PRESETS[prov][4],
                "_role": role,
                "_why": why
            })
        else:
            # Fallback: use primary for everything if no secondary
            fallback_model = PRESETS[primary_key][2][0][0] if PRESETS[primary_key][2] else model
            routes.append({
                "credential": pkey,
                "model": fallback_model,
                "family": PRESETS[primary_key][3],
                "data_policy": PRESETS[primary_key][4],
                "_role": role,
                "_why": f"fallback via {primary_pre[0]}"
            })
    
    # Save
    jsave(PROVIDER_F, {"version": 2, "api_routes": routes})

    # Chains are the source of truth for what agents actually run. Without this
    # step the routes were written and the chains kept pointing at whatever
    # models the repo shipped with (the original bug).
    existing = jload(CHAINS_F, {})
    default_yes = not existing.get("roles")
    q = ("\n  Write chains.json for all 15 roles with these providers?"
         if default_yes else
         "\n  Replace the models in your existing chains.json? (rung counts kept)")
    if yn(q, d=default_yes):
        write_chains(primary_key, secondary_key, existing)
    else:
        print("  Keeping existing chains.json — edit it by hand or re-run auto mode.")
    
    # Show what was configured
    print_box("CONFIGURED ROLES", [
        f"{r['_role']:18} → {r['model']:40} ({r['_why']})" 
        for r in routes
    ])
    
    # Validate
    print("\n  Validating…")
    do_validate(quiet=True)
    
    print("\n  ✅ AUTO-CONFIGURE COMPLETE")
    print("  Run:  opencode")
    print("  Press Tab to switch agents (M.A.X.I.M. is primary).")

# ─── QUICK SETUP ────────────────────────────────────────────────────────
def do_quick():
    hr(); print("  QUICK SETUP — 3 steps, ~30 seconds"); hr()
    if not yn("Continue?"): return
    
    opts = [(k, v[0], v[1]) for k,v in PRESETS.items()]
    sel = pick(opts, "Choose provider")
    if not sel: return
    pid, pname, purl = sel
    pre = PRESETS[pid]
    
    print(f"\n  Get key at: {purl}")
    key = ask("API key", hide=True, required=True)
    if not key: return
    
    if pre[2]:
        mopts = [(m[0], m[0], m[1]) for m in pre[2]] + [("custom","Other","Enter manually")]
        msel = pick(mopts, "Choose model")
        if not msel: return
        model = ask("Model ID", required=True) if msel[0]=="custom" else msel[0]
    else:
        model = ask("Model ID (provider/model)", required=True)
    if not model: return
    
    r = jload(PROVIDER_F, {})
    r.setdefault("api_routes", []).append({
        "credential": key, "model": model,
        "family": pre[3], "data_policy": pre[4]
    })
    jsave(PROVIDER_F, r)
    print(f"\n  ✓ Added {pname}: {model}")
    print("  ⚠ Quick Setup only adds a provider route — chains.json is unchanged.")
    print("    Run Auto-Configure (menu 1) to point the 15 roles at your models.")
    do_validate(quiet=True)
    print("\n  Done! Run:  opencode")

# ─── ADD PROVIDER ───────────────────────────────────────────────────────
def do_add():
    hr(); print("  ADD PROVIDER"); hr()
    r = jload(PROVIDER_F, {})
    for i, rt in enumerate(r.get("api_routes", []), 1):
        k = rt.get("credential","")[:8]+"…"
        print(f"  {i}) {rt['model']}  ({k})  family={rt['family']}  policy={rt['data_policy']}")
    if not r.get("api_routes"): print("  (none)")
    hr()
    
    opts = [(k, v[0]) for k,v in PRESETS.items()]
    sel = pick(opts, "Choose provider")
    if not sel: return
    pid, pname = sel
    pre = PRESETS[pid]
    
    if pid == "custom":
        name = ask("Provider name", required=True); 
        if not name: return
        key = ask("API key", hide=True, required=True)
        if not key: return
        model = ask("Model ID", required=True)
        if not model: return
        fam = ask("Family (for critic)", default="custom")
        pol = pick([("1","private-safe"),("2","training-optin"),("3","trial-logged"),("4","training-explicit")], "Data policy", back=False)
        policy = pol[0] if pol else "private-safe"
    else:
        print(f"  Get key: {pre[1]}")
        key = ask("API key", hide=True, required=True)
        if not key: return
        if pre[2]:
            mopts = [(m[0], m[0], m[1]) for m in pre[2]] + [("custom","Other","Enter manually")]
            msel = pick(mopts, "Choose model")
            if not msel: return
            model = ask("Model ID", required=True) if msel[0]=="custom" else msel[0]
        else:
            model = ask("Model ID", required=True)
        if not model: return
        fam, policy = pre[3], pre[4]
    
    r.setdefault("api_routes", []).append({
        "credential": key, "model": model, "family": fam, "data_policy": policy
    })
    jsave(PROVIDER_F, r)
    print(f"\n  ✓ Added {model}  (family={fam}, policy={policy})")

# ─── OTHER ACTIONS ──────────────────────────────────────────────────────
def do_archive():
    hr(); print("  SESSION ARCHIVE"); hr()
    print(f"  Current: {DB_DIR}")
    p = ask("New path", default=str(DB_DIR))
    if p and p != str(DB_DIR):
        Path(p).mkdir(parents=True, exist_ok=True)
        print(f"  ✓ Set to: {p}")
        print("  ⚠ Update lib/session-archive.py SESSION_ARCHIVE_ROOT to persist")
    else: print("  (unchanged)")

def do_validate(quiet=False):
    if not quiet: hr(); print("  VALIDATE"); hr()
    errs, warns = [], []
    o = jload(OPENCODE_J)
    if not o.get("plugins"): errs.append("opencode.json: missing 'plugins' (need V2)")
    elif not quiet: print("  ✓ plugins")
    if not o.get("agents",{}).get("explore",{}).get("disabled"): errs.append("agents.explore not disabled")
    elif not quiet: print("  ✓ explore disabled")
    
    r = jload(PROVIDER_F, {})
    if not r.get("api_routes"): errs.append("no provider routes")
    elif not quiet: print(f"  ✓ {len(r['api_routes'])} provider routes")
    
    c = jload(CHAINS_F)
    if not c.get("roles"): errs.append("chains.json: no roles")
    else:
        # Every rung must resolve to a model the machine knows: a route model
        # or a catalog model from `opencode models`. Unknown IDs are warnings
        # (the catalog can resolve models a routes file never lists).
        known = {rt.get("model") for rt in r.get("api_routes", []) if rt.get("model")}
        import subprocess
        try:
            out = subprocess.run(["opencode", "models"], capture_output=True,
                                 text=True, timeout=25).stdout
            known |= {ln.strip().split()[0] for ln in out.splitlines() if ln.strip()}
        except Exception:
            pass
        unknown = []
        for role, spec in c["roles"].items():
            if len(spec.get("rungs", [])) not in (3, 4):
                errs.append(f"chains.json: {role} has {len(spec.get('rungs', []))} rungs (need 3-4)")
            for m in spec.get("rungs", []):
                if known and m not in known:
                    unknown.append(f"{role}: {m}")
        if unknown:
            warns.append(f"{len(unknown)} chain model(s) not found in catalog/routes")
        if not quiet: print(f"  ✓ {len(c['roles'])} roles")
    
    if not (CFG_DIR/"model-health.json").exists(): warns.append("model-health.json missing (auto-created)")
    
    if warns and not quiet:
        for w in warns: print(f"  ⚠ {w}")
    if errs:
        if not quiet:
            for e in errs: print(f"  ✗ {e}")
        return False
    if not quiet: print("  ✓ All good")
    return True

def do_refresh():
    hr(); print("  REFRESH AGENTS"); hr()
    import subprocess
    gen = str(REPO_ROOT / "scripts" / "gen-fallbacks.py")
    run = subprocess.run(
        [sys.executable, gen, "--out", str(CFG_DIR / "agents"),
         "--chains", str(CHAINS_F)],
        capture_output=True, text=True)
    print(run.stdout.strip() or run.stderr.strip())
    if run.returncode != 0:
        print("  ✗ generation failed"); return False
    chk = subprocess.run(
        [sys.executable, gen, "--check", "--out", str(CFG_DIR / "agents"),
         "--chains", str(CHAINS_F)],
        capture_output=True, text=True)
    print(chk.stdout.strip() or chk.stderr.strip())
    if chk.returncode == 0:
        n = len(list((CFG_DIR / "agents").glob("*.md")))
        print(f"  ✓ {n} agents regenerated and in sync")
        return True
    print("  ✗ agents still out of sync"); return False

def do_view():
    hr(); print("  PROVIDER ROUTES"); hr()
    for i, rt in enumerate(jload(PROVIDER_F, {}).get("api_routes", []), 1):
        k = rt.get("credential","")[:8]+"…"
        why = rt.get("_why", "")
        print(f"  {i}) {rt['model']}")
        print(f"     key: {k}  |  family: {rt['family']}  |  policy: {rt['data_policy']}")
        if why: print(f"     💡 {why}")
    if not jload(PROVIDER_F, {}).get("api_routes"): print("  (none)")
    hr()

def do_recommendations():
    """Show the model recommendations table."""
    hr(); print("  MODEL RECOMMENDATIONS PER ROLE (2026-11-07)"); hr()
    print(f"  {'Role':<18} {'Provider':<12} {'Model':<40} Why")
    print("  " + "─"*80)
    for role, (prov, model, why) in ROLE_RECOMMENDATIONS.items():
        pname = PRESETS[prov][0] if prov in PRESETS else prov
        print(f"  {role:<18} {pname:<12} {model:<40} {why}")
    hr()
    print("  These are used automatically in --auto mode.\n"
          "  On OpenCode Go, use opencode-go/* models instead; see SETUP.md §Model Lanes.")

# ─── CHAIN GENERATION ───────────────────────────────────────────────────
# Roles that ship with 3 rungs; everything else gets 4. Used only when
# chains.json does not exist yet, so an existing chain's shape is preserved.
THREE_RUNG_ROLES = {"archiver", "security-reviewer", "final-verifier",
                    "ui-reviewer", "general"}


def _auto_pool(role, primary_key, secondary_key):
    """Ordered model pool for one role: its recommendation first (when the
    recommended provider is one of the chosen ones), then the secondary
    provider's models, then the primary's. Duplicates removed."""
    primary_models = [m for m, _ in PRESETS[primary_key][2]]
    secondary_models = [m for m, _ in (PRESETS[secondary_key][2] if secondary_key else [])]
    prov, model, _ = ROLE_RECOMMENDATIONS.get(
        role, (primary_key, primary_models[0] if primary_models else "", ""))
    first = model if prov in (primary_key, secondary_key) else (
        primary_models[0] if primary_models else model)
    pool = []
    for m in [first] + secondary_models + primary_models:
        if m and m not in pool:
            pool.append(m)
    while len(pool) < 4:
        pool.append(pool[-1] if pool else "custom/model")
    return pool


def write_chains(primary_key, secondary_key, existing):
    """Write chains.json for all 15 roles from the chosen providers.

    Rung COUNTS from an existing chains.json are preserved (the user's rung
    order is the source of truth); only the models are filled from the chosen
    providers. A timestamped backup is written first.
    """
    import shutil, time as _time
    roles = (existing or {}).get("roles", {})
    out = {}
    for role in ROLE_RECOMMENDATIONS:
        n = len(roles.get(role, {}).get("rungs", []))
        n = n if n in (3, 4) else (3 if role in THREE_RUNG_ROLES else 4)
        pool = _auto_pool(role, primary_key, secondary_key)
        out[role] = {
            "label": roles.get(role, {}).get("label", role.replace("-", " ").title()),
            "mode": "primary" if role == "mastermind" else "subagent",
            "rungs": pool[:n],
            "_flag": (f"setup-wizard {_time.strftime('%Y-%m-%d')}: auto-configured "
                      f"for {primary_key}" + (f" + {secondary_key}" if secondary_key else "")),
        }

    flat = []
    for spec in out.values():
        for m in spec["rungs"]:
            if m not in flat:
                flat.append(m)
    last_resort = (flat + flat + flat)[:3] if flat else []
    chains = {
        "_comment": ("Generated by setup-wizard from the chosen providers. "
                     "Edit freely: chains.json is the single source of truth; "
                     "re-run the generator after editing."),
        "last_resort": last_resort,
        "_last_resort_comment": "Ordered global floor after a role's own rungs.",
        "families": (existing or {}).get("families", {}),
        "roles": out,
    }
    if CHAINS_F.exists():
        backup = CHAINS_F.with_name(
            CHAINS_F.name + ".bak." + _time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(CHAINS_F, backup)
        print(f"  ✓ Backed up chains.json → {backup.name}")
    jsave(CHAINS_F, chains)
    print(f"  ✓ Wrote chains.json ({len(out)} roles)")

# ─── MAIN MENU ──────────────────────────────────────────────────────────
def main_menu():
    ensure()
    if not PROVIDER_F.exists():
        src = SAMPLE_F if SAMPLE_F.exists() else SAMPLE_FALLBACK
        if src.exists():
            import shutil; shutil.copy2(src, PROVIDER_F)
            print(f"  ✓ Created provider-routes.json from {src.name}")
        print("  ✓ Created provider-routes.json from template")
    
    while True:
        banner(); status()
        print("  🚀 1) Auto-Configure    (agent asks 3 questions, sets up ALL roles)")
        print("  ⚡ 2) Quick Setup        (1 provider, 1 model, done)")
        print("  ➕ 3) Add Provider       (add NVIDIA, OpenRouter, Groq, Anthropic, Custom)")
        print("  📁 4) Session Archive    (change storage location)")
        print("  ✅ 5) Validate Config    (check everything works)")
        print("  🔄 6) Refresh Agents     (regenerate 55 agents from chains.json)")
        print("  👁  7) View Routes        (list configured providers)")
        print("  📋 8) Show Recommendations (best model for each role)")
        print("  🚪 9) Exit")
        hr("─")
        try: c = input("  > ").strip()
        except: print("\n  bye"); break
        
        if   c == "1": do_auto()
        elif c == "2": do_quick()
        elif c == "3": do_add()
        elif c == "4": do_archive()
        elif c == "5": do_validate()
        elif c == "6": do_refresh()
        elif c == "7": do_view()
        elif c == "8": do_recommendations()
        elif c == "9": print("  bye"); break
        else: print("  1-9 only")

# ─── CLI ────────────────────────────────────────────────────────────────
def cli(args):
    ensure()
    if args.set_archive:
        p = Path(args.set_archive); p.mkdir(parents=True, exist_ok=True)
        print(f"Archive: {p}")
    if args.validate: sys.exit(0 if do_validate() else 1)
    if args.refresh_agents: sys.exit(0 if do_refresh() else 1)
    if args.quick: do_quick()
    if args.auto: do_auto()
    if args.recommendations: do_recommendations()

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="TBRN-OPENCODE Setup Wizard")
    ap.add_argument("--auto", action="store_true", help="Agent-driven auto-configure (recommended)")
    ap.add_argument("--quick", action="store_true", help="Quick 3-step setup")
    ap.add_argument("--validate", action="store_true", help="Validate config")
    ap.add_argument("--refresh-agents", action="store_true", help="Refresh agents")
    ap.add_argument("--set-archive", help="Set archive path")
    ap.add_argument("--recommendations", action="store_true", help="Show model recommendations per role")
    a = ap.parse_args()
    if a.auto or a.quick or a.validate or a.refresh_agents or a.set_archive or a.recommendations:
        cli(a)
    else:
        main_menu()
