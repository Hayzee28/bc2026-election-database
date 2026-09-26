#!/usr/bin/env python3
"""Conservative official local candidate-source watcher.

Configured sources require a verified official hostname and an explicit extraction
recipe. An individual source error retains its previous known-good candidate list.
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from bs4 import BeautifulSoup
import pdfplumber

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'config/local-sources.json'
STATE = ROOT / 'local-source-state.json'
HISTORY = ROOT / 'local-source-history.json'
DISCOVERY = ROOT / 'reports/local-source-discovery.json'
KEYWORDS = re.compile(r'withdrawn|withdrawal|acclaimed|disqualified|candidate list corrected|nomination|replacement|amended', re.I)


def utc_now():
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')


def official(url, hosts):
    parsed = urlparse(url)
    return parsed.scheme == 'https' and parsed.hostname in hosts and not parsed.username and not parsed.password


def fetch(url, hosts):
    if not official(url, hosts):
        raise ValueError('Source is outside configured official HTTPS hostnames')
    req = Request(url, headers={'User-Agent': 'BC2026-official-source-monitor/1.0'})
    with urlopen(req, timeout=25) as response:
        resolved = response.geturl()
        if not official(resolved, hosts):
            raise ValueError('Source redirected outside configured official hostnames')
        payload = response.read(8_000_001)
        if len(payload) > 8_000_000:
            raise ValueError('Source exceeds 8 MB limit')
    return payload, resolved


def names_from_html(payload, recipe):
    soup = BeautifulSoup(payload, 'html.parser')
    names = []
    for section in recipe.get('sections', []):
        heading = soup.find(string=lambda s: s and section['heading'].casefold() in str(s).casefold())
        if heading is None:
            raise ValueError('Expected candidate section missing: ' + section['heading'])
        table = heading.parent.find_next('table')
        if table is None:
            raise ValueError('Candidate table missing: ' + section['heading'])
        for tr in table.find_all('tr'):
            td = tr.find('td')
            if td:
                name = ' '.join(td.stripped_strings).strip()
                if name and name.casefold() != 'candidate name':
                    names.append(name)
    if recipe.get('selector'):
        names += [el.get_text(' ', strip=True) for el in soup.select(recipe['selector'])]
    return sorted(set(names))


def names_from_pdf(payload, recipe):
    if not recipe.get('linePattern'):
        raise ValueError('PDF source requires a configured linePattern with a name group')
    pattern = re.compile(recipe['linePattern'])
    names = []
    with pdfplumber.open(io.BytesIO(payload)) as pdf:
        for page in pdf.pages:
            for line in (page.extract_text() or '').splitlines():
                match = pattern.search(line)
                if match:
                    names.append(match.group('name').strip())
    return sorted(set(names))


def check_one(config, old, timestamp, fetcher=fetch):
    url = config['url']; hosts = config['officialHosts']
    previous = old or {}
    try:
        payload, resolved = fetcher(url, hosts)
        typ = config['type'].upper()
        names = names_from_html(payload, config) if typ == 'HTML' else names_from_pdf(payload, config)
        if len(names) < config.get('minimumNames', 1):
            raise ValueError('Too few names; extraction recipe may be stale')
        digest = hashlib.sha256(payload).hexdigest()
        return {'checkedAtUtc': timestamp, 'sourceUrl': resolved, 'sourceHash': digest,
                'sourceType': typ, 'sourceChanged': digest != previous.get('sourceHash'),
                'candidateNames': names, 'status': 'OK', 'error': None}
    except Exception as exc:
        return {**previous, 'checkedAtUtc': timestamp, 'sourceUrl': url,
                'sourceType': config['type'].upper(), 'sourceChanged': None,
                'status': 'ERROR', 'error': str(exc),
                'lastGoodCheckedAtUtc': previous.get('checkedAtUtc') if previous.get('status') == 'OK'
                                       else previous.get('lastGoodCheckedAtUtc')}


def discover(config, fetcher=fetch):
    """Suggest candidate links on official landing pages; never auto-activate them."""
    website = config.get('officialWebsite')
    if not website:
        return []
    hosts = config['officialHosts']
    payload, resolved = fetcher(website, hosts)
    soup = BeautifulSoup(payload, 'html.parser')
    results = []
    for a in soup.select('a[href]'):
        label = a.get_text(' ', strip=True)
        url = urljoin(resolved, a['href'])
        if re.search(r'candidate|nomination', label + ' ' + url, re.I) and official(url, hosts):
            results.append({'title': label[:160], 'url': url, 'verifiedHostOnly': True,
                            'requiresRecipeReview': True})
    return list({item['url']: item for item in results}.values())


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--discover', action='store_true')
    args = p.parse_args()
    configs = json.loads(CONFIG.read_text(encoding='utf-8'))
    old = json.loads(STATE.read_text(encoding='utf-8')) if STATE.exists() else {}
    history = json.loads(HISTORY.read_text(encoding='utf-8')) if HISTORY.exists() else []
    timestamp = utc_now()
    new = old.copy()
    suggestions = {}
    for conf in configs:
        jur = conf['jurisdiction']
        if jur in suggestions:
            raise ValueError('Duplicate local jurisdiction configuration')
        suggestions[jur] = []
        new[jur] = check_one(conf, old.get(jur), timestamp)
        history.append({'jurisdiction': jur, **new[jur]})
        if args.discover:
            try:
                suggestions[jur] = discover(conf)
            except Exception as exc:
                suggestions[jur] = [{'error': str(exc)}]
        print(f"LOCAL {jur}: {new[jur]['status']}, {len(new[jur].get('candidateNames', []))} names")
    STATE.write_text(json.dumps(new, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    HISTORY.write_text(json.dumps(history, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    if args.discover:
        DISCOVERY.parent.mkdir(exist_ok=True)
        DISCOVERY.write_text(json.dumps(suggestions, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')


if __name__ == '__main__':
    main()
