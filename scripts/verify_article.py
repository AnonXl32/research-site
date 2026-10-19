"""Check that all 38 original VMProtect examples remain intact (stdlib only)."""
import hashlib
import json
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
manifest = json.loads((ROOT / 'scripts/article-integrity.json').read_text(encoding='utf-8'))
article = (ROOT / '_research/tickling-vmprotect-with-llvm.md').read_text(encoding='utf-8')
blocks = re.findall(r'^```([^\n]*)\n(.*?)^```\s*$', article, re.M | re.S)
assert len(blocks) == len(manifest['code_blocks']) == 38, 'Original code example count changed'
for index, ((language, text), original) in enumerate(zip(blocks, manifest['code_blocks']), 1):
    normalized = text.rstrip('\n') + '\n'
    digest = hashlib.sha256(normalized.encode()).hexdigest()
    assert digest == original['sha256'], f'Original example {index} changed'
    assert language == original['language'], f'Example {index} language changed'
for image in manifest['images']:
    assert (ROOT / image['local'].lstrip('/')).is_file(), f'Missing diagram: {image["local"]}'
for stale in ['What’s next?', 'What\ufffds next?', 'SITECODEBLOCK', 'SITEHEADING']:
    assert stale not in article, f'Import artifact remains: {stale}'
hypervisor = (ROOT / '_research/rust-based-hypervisors.md').read_text(encoding='utf-8')
hypervisor_manifest = json.loads((ROOT / 'scripts/hypervisor-integrity.json').read_text(encoding='utf-8'))
hypervisor_body = hypervisor.split('## Abstract', 1)[1]
for original, repaired in hypervisor_manifest.get('reference_repairs', {}).items():
    assert repaired in hypervisor_body, f'Missing repaired reference: {repaired}'
    hypervisor_body = hypervisor_body.replace(repaired, original)
assert hashlib.sha256(('## Abstract' + hypervisor_body).encode()).hexdigest() == hypervisor_manifest['body_sha256'], 'Supplied hypervisor article body changed'
hypervisor_blocks = re.findall(r'^```[^\n]*\n(.*?)^```\s*$', hypervisor, re.M | re.S)
assert [hashlib.sha256(b.encode()).hexdigest() for b in hypervisor_blocks] == hypervisor_manifest['code_sha256'], 'Supplied hypervisor examples changed'
print('Article integrity passed: 38 original VMProtect examples and 3 diagrams; complete hypervisor article and 6 examples preserved.')
