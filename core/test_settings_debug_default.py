"""DEBUG doit être faux par défaut (il active aussi CORS_ALLOW_ALL_ORIGINS) : le dev local l'active via `.env`."""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_debug_defaults_to_false_in_settings():
    source = (ROOT / 'savr_back' / 'settings.py').read_text()
    match = re.search(r"^DEBUG\s*=\s*config\('DEBUG',\s*default=(\w+)", source, re.MULTILINE)
    assert match, 'DEBUG doit être lu via config(...) avec un défaut explicite'
    assert match.group(1) == 'False'


def test_compose_never_defaults_debug_to_true_and_dev_env_opts_in():
    for name in ('docker-compose.yml', 'docker/docker-compose.yml'):
        path = ROOT / name
        if path.exists():
            assert 'DEBUG:-True' not in path.read_text(), name
    assert 'DEBUG=True' in (ROOT / 'ENV_EXAMPLE.txt').read_text()  # dev local : opt-in explicite
