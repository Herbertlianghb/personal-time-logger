"""Change only this project's desktop gates; never prints secret config."""
import argparse
import json
import os
from pathlib import Path


def boolean(value):
    if value not in {'true', 'false'}:
        raise argparse.ArgumentTypeError('use true or false')
    return value == 'true'


parser = argparse.ArgumentParser()
parser.add_argument('--desktop-enabled', type=boolean)
parser.add_argument('--chat-mirror', type=boolean)
args = parser.parse_args()
path = Path('/var/lib/personal-time-logger/config.json')
if os.getuid() != 1000 or path.resolve() != path:
    raise RuntimeError('unexpected_configuration_target')
changes = {key: value for key, value in {'desktop_enabled': args.desktop_enabled,
           'desktop_chat_mirror': args.chat_mirror}.items() if value is not None}
if not changes:
    raise ValueError('no_desktop_option_requested')
config = json.loads(path.read_text(encoding='utf-8'))
config.update(changes)
temp = path.with_suffix('.desktop-options')
with temp.open('w', encoding='utf-8') as output:
    temp.chmod(0o600)
    output.write(json.dumps(config, ensure_ascii=False, indent=2))
    output.flush()
    os.fsync(output.fileno())
temp.replace(path)
print(json.dumps({'applied': changes, 'service_reload_required': True}))
