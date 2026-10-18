"""LC-15/16/17 public CLI scenarios from qa-lifecycle d5eaaf4."""
import json
import subprocess

from qa.test_damaged_lifecycle import LifecycleFixture

class MissingRecordFiles(LifecycleFixture):
    """docs/cli/agents.md "Damaged records": a record that cannot be read is shown and reported, and a running
    agent stays reachable (LC-15, LC-16, LC-17)."""

    def replace(self, name, file, how):
        agent = self.agents()[name]['id']
        path = self.root / 'state' / 'agents' / agent / file
        original = path.read_bytes()

        def restore():
            if path.is_dir():
                path.rmdir()
            path.write_bytes(original)
        self.addCleanup(restore)
        path.unlink()
        if how == 'directory':
            path.mkdir()
        return agent

    def tagged_panes(self, agent):
        panes = subprocess.run(['tmux', '-S', self.env['MV2_TMUX_SOCKET'], 'list-panes', '-a', '-F',
                                '#{@mv2_agent_id}'], capture_output=True, text=True).stdout.split()
        return panes.count(agent)

    def test_missing_properties_of_a_running_agent_is_reported(self):
        """LC-15"""
        self.mv2('spawn', '--name', 'a', '--harness', 'sleeper', '--wait')
        agent = self.replace('a', 'properties.json', 'missing')
        doctor = self.mv2('doctor', check=False)
        self.assertNotEqual(doctor.returncode, 0, doctor.stdout)
        self.assertIn(agent, doctor.stdout + doctor.stderr)
        self.assertIn(agent, self.mv2('list').stdout)

    def test_missing_properties_keeps_the_running_agents_name(self):
        """LC-15: names stay unique while the first agent still runs."""
        self.mv2('spawn', '--name', 'a', '--harness', 'sleeper', '--wait')
        self.replace('a', 'properties.json', 'missing')
        again = self.mv2('spawn', '--name', 'a', '--harness', 'sleeper', check=False)
        self.assertNotEqual(again.returncode, 0, again.stdout)

    def test_inspect_of_a_directory_properties_file_names_it(self):
        """LC-16"""
        self.mv2('spawn', '--name', 'a', '--harness', 'sleeper', '--wait')
        agent = self.replace('a', 'properties.json', 'directory')
        result = self.mv2('inspect', agent, check=False)
        self.assertNotIn('Traceback', result.stdout + result.stderr)
        self.assertIn('properties.json', result.stdout + result.stderr)

    def test_missing_runtime_never_allows_a_second_launch(self):
        """LC-17: resume refuses a duplicate launch while the agent's pane still runs."""
        self.mv2('spawn', '--name', 'a', '--harness', 'sleeper', '--wait')
        agent = self.replace('a', 'runtime.json', 'missing')
        self.mv2('resume', 'a', '--wait', check=False)
        self.assertLessEqual(self.tagged_panes(agent), 1)


