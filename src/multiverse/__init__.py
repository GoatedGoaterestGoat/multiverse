"""Small mechanics, local state, workflows expressed as prompts."""
import os
from pathlib import Path


def source():
    """The import root of this running package, in a checkout or an install."""
    return str(Path(__file__).resolve().parent.parent)


def source_environment(environment=None):
    """Pin an mv2 callback's imports without changing its parent's environment."""
    child = dict(os.environ if environment is None else environment)
    existing = child.get('PYTHONPATH')
    child['PYTHONPATH'] = source() + (os.pathsep + existing if existing else '')
    return child
