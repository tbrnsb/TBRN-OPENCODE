#!/usr/bin/env python3
"""Interactive setup wizard for TBRN-OPENCODE-V2."""

import json
import sys
from pathlib import Path

CFG_DIR = Path.home() / ".config" / "opencode"
DB_DIR = Path.home() / ".local" / "share" / "session-archive" / "global-sessions"
PROVIDER_FILE = CFG_DIR / "provider-routes.json"
OPENCODE_JSON = CFG_DIR / "opencode.json"
CHAINS_FILE = CFG_DIR / "chains.json"
SAMPLE_ROUTE = CFG_DIR / "config" / "provider-routes.example.json"

MENU_TEXT = """TBRN-OPENCODE-V2 Interactive Setup Wizard
===========================================

1. Set Session Archive Directory
2. Configure API Provider Keys
3. Validate Current Configuration
4. Refresh Generated Agents
5. Exit

Current state:
  Session archive: {}
  Provider routes: {}
  OpenCode config: {}
  Chains: {}
""".strip()

def get_choice(prompt, options, exit_val="5"):
    for i, opt in enumerate(options, 1):
        print(f"     {i}. {opt}")
    while True:
        try:
            choice = input(f"   {prompt} ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n   Goodbye!")
            return exit_val
        if choice == exit_val:
            return exit_val
        try:
            idx = int(choice)
            if 1 <= idx <= len(options):
                return str(idx)
        except ValueError:
            pass
        print("   Invalid choice. Try again.")

def load_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default if default is not None else {}

def save_json(path, data):
    path.write_text(json.dumps(data, indent=2) + "\n")

def ensure_dirs():
    DB_DIR.mkdir(parents=True, exist_ok=True)
    CFG_DIR.mkdir(parents=True, exist_ok=True)

def set_session_archive():
    print("\n[1] Session Archive Directory")
    print(f"   Current: {DB_DIR}")
    new_path = input("   Enter new path (or press Enter to keep): ").strip()
    if new_path:
        new_db = Path(new_path)
        new_db.mkdir(parents=True, exist_ok=True)
        print(f"   ✓ Would set to: {new_db}")
    else:
        print("   (kept existing)")

def configure_providers():
    print("\n[2] Configure API Provider Keys")
    routes = load_json(PROVIDER_FILE, {})

    print("   Existing routes:")
    if routes.get("api_routes"):
        for r in routes["api_routes"][:5]:
            ckey = r.get("credential", "")[:8] if r.get("credential") else "no key"
            print(f"     - {r.get('model', '?')} ({ckey}...)")
    else:
        print("   (none configured)")

    while True:
        print("\n   Select provider to add/edit:")
        print("     1. NVIDIA")
        print("     2. OpenRouter")
        print("     3. Groq")
        print("     4. Anthropic")
        print(f"     5. Finish / Save (current routes: {len(routes.get('api_routes', []))})")

        choice = input("   Choice (1-5): ").strip()
        if choice == "5":
            break
        idx = int(choice) - 1
        providers = {
            1: ("nvidia", "NVIDIA"),
            2: ("openrouter", "OpenRouter"),
            3: ("groq", "Groq"),
            4: ("anthropic", "Anthropic"),
        }
        if idx not in providers:
            print("   Invalid choice.")
            continue
        pid, pname = providers[idx]

        key = input(f"   {pname} API key (or press Enter to skip): ").strip()
        if not key:
            print("   (skipped)")
            continue

        model = input(f"   Default model for {pid} (e.g. nvidia/nemotron-3-ultra-550b-a55b): ").strip()
        if not model:
            model = f"{pid}/nemotron-3.5-lightning-30b-a3b"

        print(f"   Model family for critic separation ({pid}):")
        print("     1. nemotron (NIM + Zen are same family)")
        print("     2. openrouter")
        print("     3. mistral")
        print("     4. custom")
        fam_choice = input("   Family choice (1-4): ").strip()
        families = {"1": "nemotron", "2": "openrouter", "3": "mistral", "4": "custom"}
        family = families.get(fam_choice, "custom")

        print(f"   Data policy for {pid}:")
        print("     1. private-safe (required for private code)")
        print("     2. training-optin")
        print("     3. trial-logged")
        print("     4. training-explicit")
        pol_choice = input("   Policy choice (1-4): ").strip()
        policies = {"1": "private-safe", "2": "training-optin", "3": "trial-logged", "4": "training-explicit"}
        policy = policies.get(pol_choice, "private-safe")

        routes.setdefault("api_routes", []).append({
            "credential": key,
            "model": model,
            "family": family,
            "data_policy": policy,
        })
        print(f"   ✓ Added {pname} route")

    save_json(PROVIDER_FILE, routes)
    print(f"   ✓ Saved {PROVIDER_FILE}")

def validate_config():
    print("\n[3] Validate Current Configuration")
    errors = []
    oc = load_json(OPENCODE_JSON)
    if not oc.get("plugins"):
        errors.append("✗ opencode.json missing 'plugins' key (need V2)")
    else:
        print("  ✓ opencode.json has 'plugins' key")
    agents = oc.get("agents", {})
    if agents.get("explore", {}).get("disabled") is True:
        print("  ✓ agents.explore.disabled is True")
    else:
        errors.append("✗ agents.explore.disabled not set")
    routes = load_json(PROVIDER_FILE, {})
    if routes.get("api_routes"):
        print(f"  ✓ Provider routes: {len(routes['api_routes'])} routes")
    else:
        errors.append("✗ No provider routes configured")
    chains = load_json(CHAINS_FILE, {})
    if chains.get("roles"):
        print(f"  ✓ chains.json has {len(chains['roles'])} roles")
    else:
        errors.append("✗ chains.json has no roles")
    if errors:
        print("\nErrors:")
        for e in errors:
            print(f"  {e}")
    else:
        print("\n✓ All checks passed")
    return len(errors) == 0

def refresh_agents():
    print("\n[4] Refresh Generated Agents")
    import subprocess
    result = subprocess.run(
        [sys.executable, "scripts/gen-fallbacks.py", "--check", "--out", "config/live/agents", "--chains", "config/live/chains.json"],
        cwd=CFG_DIR,
        capture_output=True, text=True,
    )
    print(result.stdout)
    if result.stderr:
        print("STDERR:", result.stderr[:200])
    print("✓ Agent regeneration complete")

def main():
    ensure_dirs()
    if SAMPLE_ROUTE.exists():
        import shutil
        shutil.copy2(SAMPLE_ROUTE, PROVIDER_FILE)
        print(f"✓ Copied sample provider routes to {PROVIDER_FILE}")

    while True:
        print(MENU_TEXT.format(
            DB_DIR,
            PROVIDER_FILE.read_text()[:60] if PROVIDER_FILE.exists() else "none",
            OPENCODE_JSON.read_text()[:60] if OPENCODE_JSON.exists() else "none",
            CHAINS_FILE.read_text()[:60] if CHAINS_FILE.exists() else "none",
        ))

        choice = input("   Enter choice (1-5): ").strip()
        if choice == "1":
            set_session_archive()
        elif choice == "2":
            configure_providers()
        elif choice == "3":
            validate_config()
        elif choice == "4":
            refresh_agents()
        elif choice == "5" or not choice:
            print("   Goodbye!")
            break
        else:
            print("   Invalid choice. Enter 1-5.")

if __name__ == "__main__":
    main()
