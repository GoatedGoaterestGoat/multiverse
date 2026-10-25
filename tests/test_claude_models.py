"""Claude's models: mv2's table of the catalog built into Claude Code, and the check (harnesses/claude/models.py)."""
import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from isolation import isolate
from multiverse.harnesses.claude import models as m

ROOT = Path(__file__).resolve().parents[1]


def model(ident, efforts=(), default_effort=None, long=None, default=False, picker=('first_party', 'bedrock'), **ids):
    return {'id': ident, 'name': ident, 'family': 'sonnet', 'efforts': list(efforts), 'default_effort': default_effort,
            'inputs': ['text', 'image'], 'context_window': 200000, 'long_context_window': long,
            'max_output_tokens': 64000, 'default': default, 'picker': list(picker),
            'provider_ids': {'first_party': ident, **ids}}


TABLE = {'schema': 1, 'claude_code': '9.9.9', 'models': [
    model('claude-new', ['low', 'medium', 'high', 'xhigh', 'max'], 'medium', default=True, bedrock='us.claude-new'),
    model('claude-old', ['low', 'medium', 'high', 'max'], long=1000000, picker=('bedrock',), bedrock='us.claude-old'),
    model('claude-plain', bedrock='us.claude-plain'),
    model('claude-direct', ['low', 'medium', 'high']),  # not offered on Bedrock
], 'aliases': {'sonnet': {'default': 'claude-new', 'per_provider': {'bedrock': 'claude-plain'}},
               'direct': {'default': 'claude-direct', 'per_provider': {}}}}


def generator():
    spec = importlib.util.spec_from_file_location('gen_claude_models', ROOT / 'scripts' / 'gen_claude_models.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ClaudeModelsTest(unittest.TestCase):
    def setUp(self):
        isolate(self)
        patcher = patch.object(m, 'table', return_value=TABLE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_names_resolve_for_the_launchs_provider(self):
        """An alias means another model on each provider; a Bedrock launch checked as first-party would pass
        efforts its model ignores, and nothing would show it."""
        bedrock = {'CLAUDE_CODE_USE_BEDROCK': 'Yes', 'CLAUDE_CODE_USE_VERTEX': '1'}  # Claude's order: Bedrock first
        for name, effort, environment, expected in [
            ('first party', None, {}, (True, 'claude-new', 'medium')),  # no effort given: the model's default
            ('a variable set to 0 is not set', None, {'CLAUDE_CODE_USE_BEDROCK': '0'}, (True, 'claude-new', 'medium')),
            ('bedrock', 'medium', bedrock, (False, 'us.claude-plain', None)),  # its sonnet takes no effort
            ('bedrock, no effort', None, bedrock, (True, 'us.claude-plain', None)),
        ]:
            with self.subTest(name):
                found = m.check('sonnet', effort, environment)
                self.assertEqual((found.ok, found.model, found.effort), expected)
        with self.subTest('a full ID, as the provider names it'):
            self.assertEqual(m.check('us.claude-old', None, bedrock).model, 'us.claude-old')
            self.assertEqual(m.check('claude-old', None, bedrock).model, 'us.claude-old')
        with self.subTest('not offered on this provider'):
            found = m.check('direct', None, bedrock)
            self.assertEqual((found.ok, found.reason), (False, "claude-direct isn't offered on Amazon Bedrock"))
        catalog = m.catalog(bedrock)
        self.assertEqual((catalog.provider, catalog.source, catalog.version, catalog.free_form),
                         ('bedrock', 'table', '9.9.9', True))
        self.assertEqual([(item.id, item.aliases, item.default, item.hidden) for item in catalog.models],
                         [('us.claude-new', (), True, False), ('us.claude-old', (), False, False),
                          ('us.claude-plain', ('sonnet',), False, False)])
        # Hidden where that provider's picker doesn't list it.
        self.assertEqual([item.id for item in m.catalog({}).models if item.hidden], ['claude-old'])

    def test_the_check_refuses_what_claude_code_would_quietly_change(self):
        """Claude Code never refuses an effort or a suffix itself (it falls back), so a wrong one in a setting would
        silently run at another effort; a model mv2 doesn't know passes, marked unverified."""
        for name, args, expected in [
            ('an effort the model lacks', ('claude-old', 'xhigh'), (False, 'claude-old does not take effort xhigh')),
            ('a model that takes none', ('claude-plain', 'low'), (False, 'claude-plain does not take an effort')),
            ('an effort it takes', ('claude-old', 'max'), (True, None)),
            ('[1m] where the model takes it', ('claude-old[1m]', None), (True, None)),
            ('[1m] where it does not', ('sonnet[1m]', None), (False, 'claude-new does not take the [1m] suffix')),
        ]:
            with self.subTest(name):
                found = m.check(*args, {})
                self.assertEqual((found.ok, found.reason), expected)
                self.assertEqual(found.verified, found.ok)
        self.assertEqual(m.check('claude-old[1m]', None, {}).model, 'claude-old[1m]')
        unknown = m.check('claude-next', 'high', {})
        self.assertEqual((unknown.ok, unknown.verified, unknown.model, unknown.effort), (True, False, 'claude-next', 'high'))
        self.assertIn("isn't in mv2's table of Claude Code 9.9.9's models", unknown.reason)

    def test_the_generator_fails_loudly_on_a_catalog_it_does_not_know(self):
        """Claude Code's two built-in catalogs are internal and may change shape in any release; a partial table
        written quietly would refuse or pass the wrong models, or the wrong default effort, until someone noticed."""
        gen = generator()
        release = ('var py={$schema:"https://downloads.claude.ai/model-catalog/v1/schema.json",schema_version:1,'
                   'surfaces:{cc:{model_selector_state:[{id:"cc",model:"claude-a-1"}],model_selector_config:[{id:"cc",'
                   'models:[{id:"claude-b",runtime:{default_effort:"high"},offered_on:["first_party"]},'
                   '{id:"claude-a-1",runtime:{},offered_on:["bedrock"]}]}]}}};')
        good = ('var x={"//":"Hand-maintained baked-in model catalog","schema_version":1,models:[{id:"claude-a",'
                'family:"sonnet",display_name:"A",provider_ids:{first_party:"claude-a-1",bedrock:null},'
                'capabilities:["effort","max_effort"],default_effort:"high",context:{window:2e5,maximum_window:1e6,'
                'supports_1m_suffix:!0},max_output_tokens:{default:64000,upper:128000}}],'
                'aliases:{sonnet:{default:"claude-a",per_provider:{}}}};')
        good = good.replace('max_output_tokens:{default:64000,upper:128000}}]', 'max_output_tokens:{default:64000,'
                            'upper:128000}},{id:"claude-b",family:"sonnet",display_name:"B",provider_ids:{first_party:'
                            '"claude-b"},capabilities:["effort"]}]')

        def build(text):
            data = text.encode()
            return gen.table(gen.parse(gen.literal(data)), '1.0.0',
                             gen.parse(gen.literal(data, gen.RELEASE, 'release model catalog')))

        table = build(good + release)
        first, second = table['models']
        self.assertEqual({key: first[key] for key in ('efforts', 'long_context_window', 'context_window', 'default', 'picker')},
                         {'efforts': ['low', 'medium', 'high', 'max'], 'long_context_window': 1000000,
                          'context_window': 200000, 'default': True, 'picker': ['bedrock']})  # named by its dated ID
        self.assertEqual((second['default_effort'], second['picker']), ('high', ['first_party']))  # the picker's default
        for name, old, new in [
            ('a new schema', '"schema_version":1', '"schema_version":2'),
            ('a model without an id', 'id:"claude-a",', ''),
            ('an unknown provider', 'bedrock:null', 'moon:null'),
            ('capabilities that are not a list', 'capabilities:["effort","max_effort"]', 'capabilities:"effort"'),
            ('an alias to an unknown model', 'default:"claude-a"', 'default:"claude-zz"'),
            ('a default effort the model lacks', 'default_effort:"high"', 'default_effort:"xhigh"'),
            ('no catalog at all', 'Hand-maintained baked-in model catalog', 'something else'),
            ('no release catalog', 'downloads.claude.ai/model-catalog/v1/schema.json', 'elsewhere'),
            ('a release model the hand-kept catalog lacks', 'id:"claude-b",runtime', 'id:"claude-c",runtime'),
            ('a default model it lacks', 'model:"claude-a-1"', 'model:"claude-z"'),
            ('an unknown provider in the picker', 'offered_on:["bedrock"]', 'offered_on:["moon"]'),
            ("the picker's default effort the model lacks", 'default_effort:"high"', 'default_effort:"xhigh"'),
            ('no Claude Code surface', 'surfaces:{cc:', 'surfaces:{chat:'),
        ]:
            with self.subTest(name), self.assertRaises(SystemExit):
                self.assertIn(old, good + release)
                build((good + release).replace(old, new))

    def test_the_table_is_from_the_tested_claude_code(self):
        """A version check that bumps the tested version but forgets to regenerate the table would go unnoticed."""
        defaults = json.loads((ROOT / 'src/multiverse/harnesses/claude/defaults.json').read_text())
        shipped = json.loads((ROOT / 'src/multiverse/harnesses/claude/models.json').read_text())
        self.assertEqual(shipped['claude_code'], defaults['checked_cli_version'])
