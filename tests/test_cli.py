import io
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from bliss import cli, config as cfg, names
from bliss.agents import BUILTIN, agent_command, get_agent
from bliss.util import BlissError

TOML = """
[targets.a]
type = "podman"
[targets.b]
type = "ssh"
host = "me@box"
"""


class Base(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.conf = self.dir / "config.toml"
        self.conf.write_text(TOML)
        patcher = mock.patch.dict(os.environ, {"BLISS_CONFIG": str(self.conf),
                                               "XDG_STATE_HOME": str(self.dir / "state")})
        patcher.start()
        self.addCleanup(patcher.stop)


class DefaultTargetTest(Base):
    def test_target_alone_sets_default(self):
        self.assertEqual(cli.main(["--target", "b"]), 0)
        self.assertEqual(cfg.get_default_target(), "b")
        self.assertEqual(cli.main(["--target", "a"]), 0)
        self.assertEqual(cfg.get_default_target(), "a")

    def test_unknown_target_is_rejected(self):
        self.assertEqual(cli.main(["--target", "nope"]), 1)
        self.assertIsNone(cfg.get_default_target())

    def test_action_needs_some_target(self):
        self.assertEqual(cli.main(["run", "-n", "--", "x"]), 1)

    def test_explicit_target_does_not_change_default(self):
        cli.main(["--target", "a"])
        with mock.patch.object(cli, "_launch", return_value=0) as launch:
            cli.main(["--target", "b", "run", "--", "x"])
        self.assertEqual(launch.call_args.args[0].target, "b")
        self.assertEqual(cfg.get_default_target(), "a")


class RunArgsTest(Base):
    def launched(self, argv):
        with mock.patch.object(cli, "_launch", return_value=0) as launch:
            cli.main(argv)
        return launch.call_args

    def test_command_flags_pass_through(self):
        call = self.launched(["-t", "a", "run", "--", "ls", "-la", "--foo"])
        self.assertEqual(call.args[2], ["ls", "-la", "--foo"])
        self.assertFalse(call.args[4])

    def test_no_command_is_an_attached_shell(self):
        call = self.launched(["-t", "a", "run"])
        self.assertEqual(call.args[2], ["bash", "-i"])
        self.assertTrue(call.args[4])


class ConfigTest(Base):
    def test_bad_type(self):
        self.conf.write_text('[targets.x]\ntype = "vm"\n')
        with self.assertRaises(BlissError):
            cfg.load_config()

    def test_ssh_needs_host(self):
        self.conf.write_text('[targets.x]\ntype = "ssh"\n')
        with self.assertRaises(BlissError):
            cfg.load_config()

    def test_bad_toml_is_reported(self):
        self.conf.write_text("[[[")
        self.assertEqual(cli.main(["targets"]), 1)


class WorkloadNameTest(Base):
    def add(self, wid, cwd="/here", target="a"):
        cfg.record_workload({"id": wid, "target": target, "cwd": cwd, "time": 0, "container": "c"})

    def test_new_name_avoids_taken_names(self):
        taken = {f"{a}-{b}" for a in names._ADJECTIVES for b in names._ANIMALS}
        self.assertNotIn(names.new_name(taken), taken)
        self.assertRegex(names.new_name(set()), r"^[a-z]+-[a-z]+$")

    def test_find_by_name_and_by_start_of_either_word(self):
        self.add("sunny-otter")
        self.add("calm-robin")
        for query in ("sunny-otter", "sun", "ott", "sunny-o"):
            self.assertEqual(cfg.find_workload(None, query, "/x")["id"], "sunny-otter")

    def test_ambiguous_name_lists_the_choices(self):
        self.add("sunny-otter")
        self.add("sunny-robin")
        with self.assertRaisesRegex(BlissError, "sunny-otter, sunny-robin"):
            cfg.find_workload(None, "sun", "/x")

    def test_exact_name_beats_a_longer_match(self):
        self.add("sunny-otter")
        self.add("sunny-otter-7")
        self.assertEqual(cfg.find_workload(None, "sunny-otter", "/x")["id"], "sunny-otter")

    def test_dot_and_nothing_mean_newest_here(self):
        self.add("calm-robin")
        self.add("sunny-otter")
        self.add("merry-yak", cwd="/elsewhere")
        for query in (".", None):
            self.assertEqual(cfg.find_workload(None, query, "/here")["id"], "sunny-otter")

    def test_only_workload_is_found_from_anywhere_except_with_dot(self):
        self.add("sunny-otter")
        self.assertEqual(cfg.find_workload(None, None, "/x")["id"], "sunny-otter")
        with self.assertRaises(BlissError):
            cfg.find_workload(None, ".", "/x")

    def test_target_narrows_the_search(self):
        self.add("sunny-otter", target="a")
        self.add("sunny-robin", target="b")
        self.assertEqual(cfg.find_workload("b", "sun", "/x")["id"], "sunny-robin")

    def _list(self, status):
        out = io.StringIO()
        with mock.patch.object(cli, "get_target") as target, redirect_stdout(out), \
                mock.patch.object(Path, "cwd", return_value=Path("/here")):
            target.return_value.status.side_effect = status
            self.assertEqual(cli.main(["list"]), 0)
        return out.getvalue()

    def test_list_shows_state_and_marks_newest_here(self):
        self.add("sunny-otter")
        output = self._list(lambda record: "running")
        self.assertRegex(output, r"\* +sunny-otter +a +running")
        self.assertIn("bare 'bliss attach' here means sunny-otter", output)

    def test_list_survives_an_unreachable_target(self):
        self.add("sunny-otter")
        def fail(record):
            raise BlissError("no route")
        self.assertRegex(self._list(fail), r"sunny-otter +a +unknown")

    def test_attach_explains_a_finished_workload(self):
        self.add("sunny-otter")
        with mock.patch.object(cli, "get_target") as target:
            target.return_value.status.return_value = "exited"
            self.assertEqual(cli.main(["attach", "ott"]), 1)
            target.return_value.attach.assert_not_called()

    def test_short_aliases(self):
        self.add("sunny-otter")
        with mock.patch.object(cli, "get_target") as target:
            target.return_value.status.return_value = "running"
            target.return_value.attach.return_value = 0
            self.assertEqual(cli.main(["a", "ott"]), 0)
            target.return_value.attach.assert_called_once()
            self.assertEqual(cli.main(["ls"]), 0)


class AgentTest(Base):
    def test_supported_agents(self):
        self.assertEqual(set(BUILTIN), {"claude", "autolith", "hermes", "pi"})

    def test_prompt_is_one_argument(self):
        prompt = 'a "b"; rm -rf /'
        self.assertEqual(agent_command(BUILTIN["claude"], prompt, False), ["claude", "-p", prompt])

    def test_prompt_is_only_replaced_as_a_whole_argument(self):
        # Splicing it into a shell string would be a shell injection.
        agent = BUILTIN["autolith"]
        command = agent_command(agent, "$(boom)", False)
        self.assertEqual(command[-1], "$(boom)")
        self.assertNotIn("boom", command[2])

    def test_pi_prompt(self):
        self.assertEqual(agent_command(BUILTIN["pi"], "do it", True), ["pi", "--print", "do it"])

    def test_interactive_without_prompt(self):
        self.assertEqual(agent_command(BUILTIN["hermes"], None, False), ["hermes"])

    def test_unattended_is_opt_in(self):
        self.assertEqual(agent_command(BUILTIN["claude"], None, True),
                         ["claude", "--dangerously-skip-permissions"])

    def test_config_overrides(self):
        self.conf.write_text('[agents.claude]\ninteractive = ["claude", "--x"]\nversion = "1.2.3"\n')
        agent = get_agent(cfg.load_config(), "claude")
        self.assertEqual(agent.interactive, ("claude", "--x"))
        self.assertEqual(agent.version, "1.2.3")

    def test_default_from_config(self):
        self.conf.write_text('[ai]\nagent = "hermes"\n')
        self.assertEqual(get_agent(cfg.load_config(), None).name, "hermes")

    def test_unknown_agent(self):
        with self.assertRaises(BlissError):
            get_agent(cfg.load_config(), "nope")


if __name__ == "__main__":
    unittest.main()
