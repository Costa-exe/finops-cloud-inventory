"""Refresh artifact hashes for already pinned versions using public PyPI metadata.

This command never upgrades packages. Review compatibility and pin every
transitive dependency in requirements.lock before deliberately changing versions.
"""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path
import re
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
PIN = re.compile(r'^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+-]+)', re.MULTILINE)


def release_hashes(pin):
    name, version = pin
    with urlopen(f'https://pypi.org/pypi/{name}/{version}/json', timeout=30) as response:
        release = json.load(response)
    artifacts = [item for item in release['urls']
                 if item['filename'].endswith('-none-any.whl') and not item.get('yanked')]
    if not artifacts:
        raise ValueError(f'No non-yanked universal wheel for {name}=={version}')
    hashes = set()
    for artifact in artifacts:
        url = artifact['url']
        if not url.startswith('https://files.pythonhosted.org/'):
            raise ValueError(f'Unexpected artifact host for {name}')
        digest = hashlib.sha256()
        with urlopen(url, timeout=60) as response:
            while chunk := response.read(1024 * 1024):
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != artifact['digests']['sha256']:
            raise ValueError(f'Artifact digest mismatch for {name}')
        hashes.add(actual)
    return f'{name}=={version} \\\n' + ' \\\n'.join('    --hash=sha256:' + value for value in sorted(hashes))


def main():
    path = ROOT / 'requirements.lock'
    pins = PIN.findall(path.read_text(encoding='utf-8'))
    if not pins or len({name.lower().replace('_', '-') for name, _ in pins}) != len(pins):
        raise ValueError('Lockfile must contain distinct exact version pins')
    with ThreadPoolExecutor(max_workers=4) as executor:
        entries = list(executor.map(release_hashes, pins))
    header = ('# Exact runtime dependency closure for Python 3.11+.\n'
              '# Universal wheels verified against PyPI release metadata.\n'
              '# Install: python -m pip install --require-hashes --only-binary=:all: -r requirements.lock\n'
              '# Refresh hashes without upgrades: python scripts/update_lock.py\n\n')
    temporary = path.with_suffix('.lock.tmp')
    try:
        temporary.write_text(header + '\n'.join(entries) + '\n', encoding='utf-8')
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)
    print(f'Verified universal wheel hashes for {len(pins)} pinned packages.')


if __name__ == '__main__':
    main()
