import io
import json
import os
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

from bliss import cli, config as cfg, names
from bliss.agents import BUILTIN, agent_command, get_agent, read_credentials
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

    def test_list_json(self):
        self.add("sunny-otter")
        out = io.StringIO()
        with mock.patch.object(cli, "get_target") as target, redirect_stdout(out), \
                mock.patch.object(Path, "cwd", return_value=Path("/here")):
            target.return_value.status.return_value = "running"
            self.assertEqual(cli.main(["list", "--json"]), 0)
        row = json.loads(out.getvalue())[0]
        self.assertEqual((row["name"], row["state"], row["here"]), ("sunny-otter", "running", True))

    def test_empty_list_json_is_still_json(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["list", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue()), [])

    def test_targets_json(self):
        cli.main(["--target", "b"])
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(cli.main(["targets", "--json"]), 0)
        self.assertEqual(json.loads(out.getvalue()),
                         [{"name": "a", "type": "podman", "default": False},
                          {"name": "b", "type": "ssh", "default": True},
                          {"name": "here", "type": "here", "default": False}])

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

    def _status_json(self, state):
        if not cfg.list_workloads():
            self.add("sunny-otter")
        out = io.StringIO()
        with mock.patch.object(cli, "get_target") as target, redirect_stdout(out):
            target.return_value.status.return_value = state
            self.assertEqual(cli.main(["status", "--json", "ott"]), 0)
        return json.loads(out.getvalue())

    def test_status_json(self):
        self.assertEqual(self._status_json("running"),
                         {"name": "sunny-otter", "target": "a", "state": "running",
                          "finished": False, "exit": None})

    def test_status_json_counts_a_kept_shell_as_finished(self):
        status = self._status_json("exited(3)")
        self.assertEqual((status["finished"], status["exit"]), (True, 3))
        status = self._status_json("Succeeded")
        self.assertEqual((status["finished"], status["exit"]), (True, 0))

    def test_status_without_json_is_unchanged(self):
        self.add("sunny-otter")
        out = io.StringIO()
        with mock.patch.object(cli, "get_target") as target, redirect_stdout(out):
            target.return_value.status.return_value = "running"
            self.assertEqual(cli.main(["status", "ott"]), 0)
        self.assertEqual(out.getvalue(), "sunny-otter: running\n")

    def test_short_aliases(self):
        self.add("sunny-otter")
        with mock.patch.object(cli, "get_target") as target:
            target.return_value.status.return_value = "running"
            target.return_value.attach.return_value = 0
            self.assertEqual(cli.main(["a", "ott"]), 0)
            target.return_value.attach.assert_called_once()
            self.assertEqual(cli.main(["ls"]), 0)


class HereTest(Base):
    def test_exists_without_settings(self):
        self.conf.write_text("")
        self.assertEqual(cli.main(["--target", "here"]), 0)
        self.assertEqual(cfg.get_default_target(), "here")

    def test_settings_can_choose_screen(self):
        self.conf.write_text('[targets.here]\ntype = "here"\nsession = "screen"\n')
        self.assertEqual(cfg.load_config().target("here")["session"], "screen")

    def test_plan_needs_no_package_manager(self):
        out = io.StringIO()
        with mock.patch.object(cli, "detect_system", side_effect=AssertionError), redirect_stdout(out):
            self.assertEqual(cli.main(["-t", "here", "run", "-n", "--", "x"]), 0)
        self.assertIn("in place", out.getvalue())

    def test_agent_must_be_installed_and_nothing_is_fetched(self):
        with mock.patch.object(cli, "resolve", side_effect=AssertionError), \
                mock.patch.object(cli, "has_tool", return_value=False):
            self.assertEqual(cli.main(["-t", "here", "ai", "-n", "--agent", "pi", "x"]), 1)

    def test_agent_runs_from_path_without_sending_credentials(self):
        self.conf.write_text('[agents.claude]\ncredentials = ["/nonexistent"]\n')
        out = io.StringIO()
        with mock.patch.object(cli, "resolve", side_effect=AssertionError), \
                mock.patch.object(cli, "has_tool", return_value=True), redirect_stdout(out):
            self.assertEqual(cli.main(["-t", "here", "ai", "-n", "x"]), 0)
        self.assertNotIn("login:", out.getvalue())

    def test_launch_json_prints_the_workload(self):
        out = io.StringIO()
        with mock.patch.object(cli, "get_target") as target, \
                mock.patch.object(cli, "has_tool", return_value=True), redirect_stdout(out):
            target.return_value.launch.return_value = {"session": "s", "dir": "/d"}
            self.assertEqual(cli.main(["-t", "here", "ai", "--json", "--", "do it"]), 0)
        started = json.loads(out.getvalue())
        self.assertEqual(started["target"], "here")
        self.assertEqual(cfg.find_workload(None, started["name"], "/")["id"], started["name"])

    def test_pull_says_there_is_nothing_to_copy(self):
        cfg.record_workload({"id": "sunny-otter", "target": "here", "cwd": "/x", "time": 0,
                             "session": "s", "dir": "/d"})
        with mock.patch.object(cli, "get_target") as target:
            self.assertEqual(cli.main(["pull", "ott"]), 0)
            target.return_value.pull.assert_not_called()


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

    def test_credentials_are_opt_in(self):
        self.assertEqual(get_agent(cfg.load_config(), "claude").credentials, ())

    def test_credentials_are_read_by_their_own_name(self):
        login = self.dir / ".credentials.json"
        login.write_text("token")
        self.conf.write_text(f'[agents.claude]\ncredentials = ["{login}"]\n')
        agent = get_agent(cfg.load_config(), "claude")
        self.assertEqual(read_credentials(agent), {".credentials.json": "token"})

    def test_missing_credentials_file_is_an_error_not_a_silent_skip(self):
        self.conf.write_text('[agents.claude]\ncredentials = ["/nonexistent/login.json"]\n')
        with self.assertRaisesRegex(BlissError, "cannot read /nonexistent/login.json"):
            read_credentials(get_agent(cfg.load_config(), "claude"))

    def test_agent_without_a_config_directory_refuses_credentials(self):
        self.conf.write_text('[agents.hermes]\ncredentials = ["~/x"]\n')
        with self.assertRaisesRegex(BlissError, "cannot send them safely"):
            get_agent(cfg.load_config(), "hermes")

    def test_unknown_agent(self):
        with self.assertRaises(BlissError):
            get_agent(cfg.load_config(), "nope")


if __name__ == "__main__":
    unittest.main()
