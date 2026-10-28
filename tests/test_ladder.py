#!/usr/bin/env python3
"""Tests for the failure ladder and the agent generator.

Runs against the real `chains.json`, but every subprocess writes to a temp
health registry and a temp rung log, so a test can never mark a live model
down or pollute the dispatch log. No network, no model calls, no money.

    python3 -m unittest discover -s tests -v
    python3 -m unittest tests.test_ladder -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
# Repo installs keep the config under config/live/; the live tree has it at ROOT.
CONFIG_ROOT = ROOT if (ROOT / "chains.json").exists() else ROOT / "config" / "live"
# Pin the ladder's config root to this install, whatever the caller's env says.
os.environ["STAGING"] = str(CONFIG_ROOT)

import ladder  # noqa: E402

CHAINS = json.loads((CONFIG_ROOT / "chains.json").read_text())
LADDER = ROOT / "scripts" / "ladder.py"
GENERATOR = ROOT / "scripts" / "gen-fallbacks.py"

_spec = importlib.util.spec_from_file_location("gen_fallbacks", GENERATOR)
gen_fallbacks = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen_fallbacks)


class SuffixTests(unittest.TestCase):
    def test_four_rungs(self):
        self.assertEqual(ladder.suffixes_for(4), ["", "-2", "-3", "-last"])

    def test_three_rungs_has_no_dash3(self):
        self.assertEqual(ladder.suffixes_for(3), ["", "-2", "-last"])

    def test_other_counts_rejected(self):
        for n in (0, 1, 2, 5, 6):
            with self.assertRaises(ValueError):
                ladder.suffixes_for(n)


class FamilyTests(unittest.TestCase):
    def test_both_nemotron_spellings_are_one_family(self):
        primary = ladder.family("nvidia/nvidia/nemotron-3-ultra-550b-a55b", CHAINS)
        secondary = ladder.family(
            "nvidia_secondary/nvidia/nemotron-3-ultra-550b-a55b", CHAINS)
        self.assertEqual(primary, "nemotron")
        self.assertEqual(primary, secondary)

    def test_unlisted_model_is_its_own_family(self):
        self.assertEqual(
            ladder.family("opencode/glm-5.3-flash-free", CHAINS),
            "opencode/glm-5.3-flash-free")

    def test_every_chain_model_resolves(self):
        for role, spec in CHAINS["roles"].items():
            for model in spec["rungs"]:
                self.assertTrue(ladder.family(model, CHAINS), role)


class ParseAuthorModelsTests(unittest.TestCase):
    def state(self, text: str) -> str:
        path = pathlib.Path(tempfile.mkdtemp()) / "STATE.md"
        path.write_text(text)
        return str(path)

    def test_inline_form(self):
        path = self.state("## TURN\n\nauthor_models: opencode/a-b, mistral/c-d\n")
        self.assertEqual(ladder.parse_author_models(path),
                         ["opencode/a-b", "mistral/c-d"])

    def test_bullet_form(self):
        path = self.state(
            "## TURN\n\n### author_models (this turn)\n"
            "- opencode/deepseek-v4.1-flash-free — orchestrator takeover\n"
            "- mistral/codestral-latest\n")
        self.assertEqual(
            ladder.parse_author_models(path),
            ["opencode/deepseek-v4.1-flash-free", "mistral/codestral-latest"])

    def test_only_latest_section_counts(self):
        path = self.state(
            "## TURN 1\n\n### author_models\n- google/gemini-3.7-flash\n\n"
            "## TURN 2\n\n### author_models\n- opencode/glm-5.3-flash-free\n")
        self.assertEqual(ladder.parse_author_models(path),
                         ["opencode/glm-5.3-flash-free"])

    def test_prose_on_a_bullet_is_not_a_model(self):
        path = self.state(
            "### author_models\n"
            "- opencode/x-y — see config/schema.py and docs/plans\n")
        self.assertEqual(ladder.parse_author_models(path), ["opencode/x-y"])

    def test_duplicates_collapse(self):
        path = self.state("author_models: opencode/x-y, opencode/x-y\n")
        self.assertEqual(ladder.parse_author_models(path), ["opencode/x-y"])

    def test_missing_section_is_empty(self):
        self.assertEqual(ladder.parse_author_models(self.state("## TURN\n")), [])

    def test_legacy_single_field(self):
        path = self.state("## TURN\n\nauthor_model: opencode/x-y\n")
        self.assertEqual(ladder.parse_author_models(path), ["opencode/x-y"])

    def test_real_state_file_parses(self):
        state = pathlib.Path.home() / ".orchestrator" / "STATE.md"
        if not state.exists():
            self.skipTest("no ~/.orchestrator/STATE.md")
        models = ladder.parse_author_models(str(state))
        for m in models:
            self.assertRegex(m, ladder.MODEL_TOKEN.pattern)


class PickCriticTests(unittest.TestCase):
    """The critic must differ BY FAMILY from every model that edited the diff."""

    def pick(self, text: str) -> subprocess.CompletedProcess:
        path = pathlib.Path(tempfile.mkdtemp()) / "STATE.md"
        path.write_text(text)
        return subprocess.run([sys.executable, str(LADDER), "--pick-critic",
                               str(path)], capture_output=True, text=True)

    def test_first_differing_rung_wins(self):
        got = self.pick("### author_models\n- opencode/deepseek-v4.1-flash-free\n")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("glm-5.3-flash-free", got.stdout)
        self.assertIn("[criticbase]", got.stdout)

    def test_skips_same_family_and_takes_the_next_rung(self):
        got = self.pick(
            "### author_models\n"
            "- opencode/glm-5.3-flash-free\n"
            "- nvidia_secondary/nvidia/nemotron-3-ultra-550b-a55b\n")
        self.assertEqual(got.returncode, 0, got.stderr)
        # glm (rung 1) and nemotron (rung 2, other spelling) both refused.
        self.assertIn("[critic-3]", got.stdout)
        self.assertIn("deepseek-v4.1-flash-free", got.stdout)
        self.assertIn("SAME FAMILY", got.stdout)

    def test_all_rungs_same_family_escalates(self):
        got = self.pick(
            "### author_models\n"
            "- opencode/glm-5.3-flash-free\n"
            "- nvidia/nvidia/nemotron-3-ultra-550b-a55b\n"
            "- opencode/deepseek-v4.1-flash-free\n"
            "- google/gemini-3.7-flash\n")
        self.assertEqual(got.returncode, 4)
        self.assertIn("escalate", got.stderr)
        self.assertEqual(got.stdout.count("refused"), 4)

    def test_no_authors_refuses(self):
        got = self.pick("## TURN\n\nnothing here\n")
        self.assertEqual(got.returncode, 4)
        self.assertIn("refusing", got.stderr)

    def test_older_turn_does_not_block_the_critic(self):
        got = self.pick(
            "### author_models\n- opencode/glm-5.3-flash-free\n"
            "- nvidia/nvidia/nemotron-3-ultra-550b-a55b\n"
            "- opencode/deepseek-v4.1-flash-free\n- google/gemini-3.7-flash\n\n"
            "## NEXT TURN\n\n### author_models\n- opencode/deepseek-v4.1-flash-free\n")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("glm-5.3-flash-free", got.stdout)


class LadderRunTests(unittest.TestCase):
    """Real subprocess runs, with health and logging redirected to temp files."""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.env = dict(os.environ,
                        MODEL_HEALTH_FILE=str(self.tmp / "health.json"),
                        RUNG_LOG_FILE=str(self.tmp / "rung-log.jsonl"),
                        STAGING=str(CONFIG_ROOT))

    def run_ladder(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(LADDER), *args],
                              capture_output=True, text=True, env=self.env)

    def test_dry_run_four_rung_role(self):
        got = self.run_ladder("--dry-run", "coder")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("[coder]", got.stdout)
        self.assertIn("FAIL", got.stdout)
        self.assertIn("[coder-2]", got.stdout)
        self.assertNotIn("-3", got.stdout.split("[coder-2]")[0])

    def test_dry_run_three_rung_role(self):
        got = self.run_ladder("--dry-run", "general")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("[general-2]", got.stdout)
        self.assertNotIn("[general-3]", got.stdout)

    def test_full_failure_exits_3_and_marks_every_model_down(self):
        got = self.run_ladder("general", "--", "false")
        self.assertEqual(got.returncode, 3, got.stdout + got.stderr)
        self.assertIn("all rungs + last_resort failed", got.stderr)

        rungs = CHAINS["roles"]["general"]["rungs"]
        # last_resort entries already present in the role chain are skipped, not
        # re-run, so only genuinely new models reach the health registry.
        extra = [m for m in CHAINS["last_resort"] if m not in rungs]
        health = json.loads((self.tmp / "health.json").read_text())
        self.assertEqual(set(health), set(rungs) | set(extra))
        self.assertTrue(all(v["reason"] == "empty" for v in health.values()))
        for model in set(CHAINS["last_resort"]) & set(rungs):
            self.assertIn(f"{model}: already in the role chain, skipping", got.stdout)

        # Two attempts per model actually tried.
        log = (self.tmp / "rung-log.jsonl").read_text().splitlines()
        self.assertEqual(len(log), 2 * (len(rungs) + len(extra)))

    def test_cooled_model_is_skipped_not_retried(self):
        model = CHAINS["roles"]["general"]["rungs"][0]
        subprocess.run([sys.executable, str(ROOT / "lib" / "health.py"),
                        "down", model, "429"], capture_output=True, env=self.env)
        got = self.run_ladder("--dry-run", "general")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("in cooldown, skipping", got.stdout)
        self.assertNotIn(f"[general] {model}: attempt", got.stdout)

    def test_no_command_is_a_usage_error(self):
        self.assertEqual(self.run_ladder("coder").returncode, 2)

    def test_write_handoff(self):
        path = self.tmp / "HANDOFF.md"
        got = subprocess.run([sys.executable, str(LADDER), "--write-handoff",
                              str(path)], capture_output=True, text=True)
        self.assertEqual(got.returncode, 0, got.stderr)
        text = path.read_text()
        for field in ("Task:", "Decisions:", "Files changed:", "Next step:"):
            self.assertIn(field, text)


class GeneratorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp()) / "agents"

    def gen(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, str(GENERATOR), *args],
                              capture_output=True, text=True)

    def test_live_agents_are_in_sync(self):
        got = self.gen("--check")
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertIn("in sync", got.stdout)

    def test_generated_set_matches_chains(self):
        expected = sum(len(v["rungs"]) for v in CHAINS["roles"].values())
        got = self.gen("--out", str(self.tmp))
        self.assertEqual(got.returncode, 0, got.stderr)
        self.assertEqual(len(list(self.tmp.glob("*.md"))), expected)
        self.assertEqual(self.gen("--check", "--out", str(self.tmp)).returncode, 0)

    def test_hidden_marking(self):
        self.gen("--out", str(self.tmp))
        hidden, visible = [], []
        for p in self.tmp.glob("*.md"):
            (hidden if "hidden: true" in p.read_text() else visible).append(p.stem)
        # Base rungs stay visible; every fallback rung of a subagent role hides.
        for role, spec in CHAINS["roles"].items():
            self.assertIn(role, visible)
            if role == "mastermind":
                continue
            for suffix in ladder.suffixes_for(len(spec["rungs"]))[1:]:
                self.assertIn(f"{role}{suffix}", hidden)

    def test_three_rung_role_has_no_dash3_file(self):
        self.gen("--out", str(self.tmp))
        for role in ("security-reviewer", "final-verifier", "ui-reviewer", "general"):
            self.assertFalse((self.tmp / f"{role}-3.md").exists(), role)
            self.assertTrue((self.tmp / f"{role}-last.md").exists(), role)

    def test_drift_is_detected(self):
        self.gen("--out", str(self.tmp))
        victim = self.tmp / "coder-2.md"
        victim.write_text(victim.read_text() + "\nhand-edited\n")
        got = self.gen("--check", "--out", str(self.tmp))
        self.assertEqual(got.returncode, 1)
        self.assertIn("drift", got.stderr)

    def test_stale_file_is_detected(self):
        self.gen("--out", str(self.tmp))
        (self.tmp / "retired-role.md").write_text("stale\n")
        got = self.gen("--check", "--out", str(self.tmp))
        self.assertEqual(got.returncode, 1)
        self.assertIn("stale    retired-role.md", got.stderr)

    def test_gsd_agents_are_spared(self):
        self.tmp.mkdir(parents=True, exist_ok=True)
        kept = self.tmp / "gsd-helper.md"
        kept.write_text("not ours\n")
        self.gen("--out", str(self.tmp))
        self.assertTrue(kept.exists())
        self.assertEqual(self.gen("--check", "--out", str(self.tmp)).returncode, 0)


class LiveConfigTests(unittest.TestCase):
    """TASK A: colors, mastermind subagent whitelist, v2-native config."""

    DISPATCHABLE = [
        "planner", "coder", "tester", "debugger", "critic", "designer",
        "copywriter", "explorer", "code-reviewer", "archiver",
        "security-reviewer", "final-verifier", "ui-reviewer", "general",
    ]

    @staticmethod
    def frontmatter(path: pathlib.Path) -> list[str]:
        lines = path.read_text().splitlines()
        self_check = lines[0] == "---"
        if not self_check:
            raise AssertionError(f"{path}: no frontmatter")
        out = []
        for line in lines[1:]:
            if line == "---":
                break
            out.append(line)
        return out

    def test_generated_agents_have_colors(self):
        colors = gen_fallbacks.COLORS
        agents = sorted((CONFIG_ROOT / "agents").glob("*.md"))
        self.assertEqual(len(agents), 56)
        for p in agents:
            role = p.stem
            for suffix in ("-2", "-3", "-last"):
                if role.endswith(suffix):
                    role = role[: -len(suffix)]
                    break
            self.assertIn(role, colors, f"{p.name}: role {role} has no color")
            want = f'color: "{colors[role]}"'
            found = [ln for ln in self.frontmatter(p) if ln.startswith("color:")]
            self.assertEqual(found, [want], p.name)

    def test_mastermind_subagent_whitelist(self):
        rules = gen_fallbacks.ROLES["mastermind"]["permission"]["subagent"]
        keys = list(rules)
        self.assertEqual(keys[0], "*")
        self.assertEqual(rules["*"], "deny")
        self.assertNotIn("mastermind", rules)
        self.assertNotIn("mastermind-2", rules)
        for role in self.DISPATCHABLE:
            self.assertEqual(rules.get(role), "allow", role)
        for pattern in ("*-2", "*-3", "*-last"):
            self.assertEqual(rules.get(pattern), "allow", pattern)
        expected = len(self.DISPATCHABLE) + 4  # *, *-2, *-3, *-last
        self.assertEqual(len(keys), expected, keys)
        # The generated frontmatter keeps "*" first.
        text = (CONFIG_ROOT / "agents" / "mastermind.md").read_text()
        self.assertIn("  subagent:", text)
        sub = text.split("  subagent:", 1)[1]
        first_rule = sub.split('"')[1] if '"' in sub else ""
        self.assertEqual(first_rule, "*")

    def test_config_is_v2_native(self):
        config = json.loads((CONFIG_ROOT / "opencode.json").read_text())
        self.assertNotIn("plugin", config)
        self.assertIn("plugins", config)
        self.assertIsInstance(config["plugins"], list)
        self.assertTrue(any("superpowers@" in s for s in config["plugins"]),
                        config["plugins"])
        self.assertIs(config["agents"]["explore"]["disabled"], True)


if __name__ == "__main__":
    unittest.main(verbosity=2)
