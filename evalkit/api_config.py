"""Local API setup. Never print or persist a resolved credential."""
import json
import os
import re
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent / 'api.local.json'


def validate_config(config):
    if not isinstance(config, dict):
        raise ValueError('API config must be a JSON object')
    if config.get('protocol') != 'responses':
        raise ValueError('Only the Responses protocol is currently supported')
    endpoint = config.get('endpoint', '')
    try:
        parts = urlsplit(endpoint)
        valid = (isinstance(endpoint, str) and endpoint.isascii()
                 and not any(c.isspace() for c in endpoint) and parts.scheme == 'https'
                 and bool(parts.hostname) and not parts.username and not parts.password
                 and not parts.query and not parts.fragment and parts.path.endswith('/responses'))
        _ = parts.port
    except (ValueError, TypeError, AttributeError):
        valid = False
    if not valid:
        raise ValueError('endpoint must be a complete HTTPS /responses URL without credentials or query parameters')
    if not isinstance(config.get('model'), str) or not config['model'].strip():
        raise ValueError('model is required')
    name = config.get('key_env', '')
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]{0,79}', name):
        raise ValueError('key_env must be an environment variable NAME, not the API key; put the key in .env')
    for field, default in (('max_output_tokens', 12000), ('timeout_seconds', 180)):
        value = config.get(field, default)
        if type(value) is not int or value <= 0:
            raise ValueError(field + ' must be a positive integer')


def load_api_config(path=DEFAULT_CONFIG):
    path = Path(path).resolve()
    try:
        config = json.loads(path.read_text(encoding='utf-8-sig'))
    except (OSError, ValueError, UnicodeError):
        raise ValueError('Cannot read API config; create a valid api.local.json from config/api.example.json') from None
    validate_config(config)
    key_file = config.get('key_file')
    if key_file is not None:
        if not isinstance(key_file, str) or not key_file.strip():
            raise ValueError('key_file must be a local file path')
        config['_key_file_path'] = str((path.parent / key_file).resolve())
    return config


def resolve_key(config):
    validate_config(config)
    name = config['key_env']
    secret = os.environ.get(name, '')
    source = 'environment'
    if not secret.strip():
        source = 'local_file'
        key_path = config.get('_key_file_path')
        if not key_path:
            raise ValueError('API key is missing; fill the configured environment variable or .env file')
        try:
            lines = Path(key_path).read_text(encoding='utf-8-sig').splitlines()
        except (OSError, UnicodeError):
            raise ValueError('Cannot read the local .env key file') from None
        matches = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if line.startswith('export '):
                line = line[7:].lstrip()
            key, separator, value = line.partition('=')
            if separator and key.strip() == name:
                value = value.strip()
                if len(value) >= 2 and value[0] == value[-1] and value[0] in ('"', "'"):
                    value = value[1:-1]
                matches.append(value)
        if len(matches) != 1:
            raise ValueError('The .env file must contain exactly one configured API-key entry')
        secret = matches[0]
    if not secret or secret.lower() in {'your_api_key_here', 'paste_your_api_key_here', 'replace_me'}:
        raise ValueError('API key is empty or still a placeholder; fill the value after = in .env')
    if not secret.isascii() or any(c.isspace() or ord(c) < 33 or ord(c) == 127 for c in secret):
        raise ValueError('API key contains invalid whitespace or characters; paste only the key')
    return secret, source


def check_setup(path=DEFAULT_CONFIG):
    """Offline readiness only, never a claim that the provider accepted the key."""
    try:
        config = load_api_config(path)
        _, source = resolve_key(config)
        return {'status': 'ready_for_connection_test', 'key_present': True, 'key_source': source,
                'network_tested': False, 'note': 'Local setup only; provider, model access and quota are unverified'}
    except ValueError as exc:
        return {'status': 'not_ready', 'network_tested': False, 'reason': str(exc)}
