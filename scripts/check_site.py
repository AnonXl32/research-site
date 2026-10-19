"""Validate the generated site without network calls or third-party packages."""
import re
import hashlib
import json
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else '_site').resolve()
ORIGIN = 'https://research.elbaz.ch'

class Page(HTMLParser):
    def __init__(self, path):
        super().__init__(convert_charrefs=True)
        self.path, self.ids, self.links, self.images, self.headings = path, [], [], [], []
        self.canonicals, self.descriptions, self.og_images = [], [], []
        self.code_blocks, self.in_pre, self.code = [], False, []
        self.feed(path.read_text(encoding='utf-8'))

    def handle_starttag(self, tag, pairs):
        attrs = dict(pairs)
        if tag == 'pre':
            self.in_pre, self.code = True, []
        if attrs.get('id'):
            self.ids.append(attrs['id'])
        if tag in ['h1', 'h2', 'h3', 'h4', 'h5', 'h6']:
            self.headings.append(tag)
        if tag == 'a' and attrs.get('href'):
            self.links.append(attrs['href'])
        if tag in ['img', 'script'] and attrs.get('src'):
            self.links.append(attrs['src'])
            assert not attrs['src'].startswith('http:'), f'{self.path}: insecure asset'
        if tag == 'img':
            self.images.append(attrs)
        if tag == 'link' and attrs.get('rel') == 'stylesheet':
            self.links.append(attrs['href'])
        if tag == 'link' and attrs.get('rel') == 'canonical':
            self.canonicals.append(attrs.get('href'))
        if tag == 'meta' and attrs.get('name') == 'description':
            self.descriptions.append(attrs.get('content'))
        if tag == 'meta' and attrs.get('property') == 'og:image':
            self.og_images.append(attrs.get('content'))

    def handle_data(self, data):
        if self.in_pre:
            self.code.append(data)

    def handle_endtag(self, tag):
        if tag == 'pre':
            self.code_blocks.append(''.join(self.code))
            self.in_pre = False

def target(url, current):
    parsed = urlsplit(url)
    if parsed.scheme or parsed.netloc:
        if parsed.netloc != 'research.elbaz.ch':
            return None, None
    path = unquote(parsed.path)
    dest = ROOT / path.lstrip('/') if path.startswith('/') else current.parent / path
    if not path:
        dest = current
    if dest.is_dir():
        dest /= 'index.html'
    if not dest.suffix and not dest.exists():
        dest /= 'index.html'
    return dest.resolve(), unquote(parsed.fragment)

assert ROOT.is_dir(), 'Build the site first'
pages = {p.resolve(): Page(p) for p in ROOT.rglob('*.html')}
assert pages, 'No generated pages'
errors = []
for path, page in pages.items():
    if page.headings.count('h1') != 1:
        errors.append(f'{path.relative_to(ROOT)}: expected one h1')
    duplicates = [name for name, count in Counter(page.ids).items() if count > 1]
    if duplicates:
        errors.append(f'{path.relative_to(ROOT)}: duplicate IDs: {duplicates}')
    expected_path = '/' + path.relative_to(ROOT).as_posix()
    if expected_path.endswith('/index.html'):
        expected_path = expected_path[:-10]
    if page.canonicals != [ORIGIN + expected_path]:
        errors.append(f'{path.relative_to(ROOT)}: incorrect canonical {page.canonicals}')
    if not page.descriptions or not all(page.descriptions):
        errors.append(f'{path.relative_to(ROOT)}: missing description')
    for image in page.images:
        if not image.get('alt'):
            errors.append(f'{path.relative_to(ROOT)}: missing image description')
    for url in page.links + page.og_images:
        dest, fragment = target(url, path)
        if dest is None:
            continue
        if not dest.is_relative_to(ROOT) or not dest.is_file():
            errors.append(f'{path.relative_to(ROOT)}: broken local link {url}')
        elif fragment and dest in pages and fragment not in pages[dest].ids:
            errors.append(f'{path.relative_to(ROOT)}: broken fragment {url}')

for name in ['feed.xml', 'disclosures/feed.xml', 'sitemap.xml']:
    file = ROOT / name
    if not file.is_file():
        errors.append(f'Missing {name}')
    else:
        ElementTree.parse(file)
feed_ns = {'a': 'http://www.w3.org/2005/Atom'}
entries = ElementTree.parse(ROOT / 'feed.xml').findall('a:entry', feed_ns)
if len(entries) < 2:
    errors.append('Research feed must include both articles')
integrity_path = Path(__file__).resolve().parent / 'article-integrity.json'
integrity = json.loads(integrity_path.read_text(encoding='utf-8'))
rendered_examples = pages[ROOT / 'research/tickling-vmprotect-with-llvm/index.html'].code_blocks
if len(rendered_examples) != 38:
    errors.append('Rendered VMProtect code example count changed')
for index, (code, original) in enumerate(zip(rendered_examples, integrity['code_blocks']), 1):
    if hashlib.sha256((code.rstrip('\n') + '\n').encode()).hexdigest() != original['sha256']:
        errors.append(f'Rendered VMProtect example {index} changed during Markdown processing')
for forbidden in ['.git', '.cache', '.github', 'scripts', 'Gemfile', 'Gemfile.lock', 'README.md', 'DEPLOYMENT.md', '_drafts', 'private', 'output', 'tmp', '.env']:
    if (ROOT / forbidden).exists():
        errors.append(f'Non-public file deployed: {forbidden}')
for path in ROOT.rglob('*'):
    if path.is_file() and path.suffix in ['.html', '.xml', '.js', '.css', '.txt']:
        text = path.read_text(encoding='utf-8')
        if re.search(r'(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|-----BEGIN [A-Z ]*PRIVATE KEY-----|[A-Za-z0-9._%+-]+@elbaz\.ch)', text):
            errors.append(f'Credential or private contact pattern in {path.relative_to(ROOT)}')
        if re.search(r'\{\{\s*(?:site|page)\.', text):
            errors.append(f'Unrendered template in {path.relative_to(ROOT)}')
if errors:
    raise SystemExit('\n'.join(errors))
print(f'Site checks passed: {len(pages)} pages, local links and anchors, images, metadata, 2 feeds, sitemap, publishing boundaries.')
