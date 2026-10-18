"""LC-15/16 public CLI scenarios from qa-lifecycle d5eaaf4."""
from qa.test_damaged_lifecycle import LifecycleFixture

class MissingRecordFiles(LifecycleFixture):
    """docs/cli/agents.md "Damaged records": a record that cannot be read is shown and reported, and a running
    agent stays reachable (LC-15, LC-16)."""

    def test_missing_properties_keeps_the_running_agents_name(self):
        """Through the real CLI, a running agent whose properties.json is gone keeps its name, so a second spawn with
        it is refused, and it stays listed and reported; fragile because the name must come from tmux tags or launch
        evidence, and a break allows two agents with one name (LC-15, LC-16, f7cf2ce)."""
        self.mv2('spawn', '--name', 'a', '--harness', 'sleeper', '--wait')
        agent = self.agents()['a']['id']
        path = self.root / 'state' / 'agents' / agent / 'properties.json'
        original = path.read_bytes()

        def restore():
            if path.is_dir():
                path.rmdir()
            path.write_bytes(original)
        self.addCleanup(restore)
        path.unlink()
        again = self.mv2('spawn', '--name', 'a', '--harness', 'sleeper', check=False)
        self.assertNotEqual(again.returncode, 0, again.stdout)
        doctor = self.mv2('doctor', check=False)
        self.assertNotEqual(doctor.returncode, 0, doctor.stdout)
        self.assertIn(agent, doctor.stdout + doctor.stderr)
        self.assertIn(agent, self.mv2('list').stdout)
        path.mkdir()  # LC-16: a directory in its place is named, not a crash
        result = self.mv2('inspect', agent, check=False)
        self.assertNotIn('Traceback', result.stdout + result.stderr)
        self.assertIn('properties.json', result.stdout + result.stderr)
