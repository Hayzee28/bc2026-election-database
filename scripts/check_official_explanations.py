#!/usr/bin/env python3
"""Find candidate-specific keyword context on configured official pages.

Matches are excerpts for review, never a claim that a page explains a revision.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from bs4 import BeautifulSoup
from watch_local_sources import fetch, KEYWORDS

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / 'official-explanations.json'


def scan(anomalies, configs, fetcher=fetch):
    result = {}
    by_jur = {c['jurisdiction']: c for c in configs}
    for jur, conf in by_jur.items():
        targets = {(a['candidateId'], a['candidate']) for a in anomalies if a['jurisdiction'] == jur}
        if not targets:
            continue
        try:
            payload, resolved = fetcher(conf['url'], conf['officialHosts'])
            if conf['type'].upper() == 'PDF':
                import io, pdfplumber
                with pdfplumber.open(io.BytesIO(payload)) as pdf:
                    content = '\n'.join(page.extract_text() or '' for page in pdf.pages)
                title = resolved.rsplit('/', 1)[-1]
                date = None
            else:
                soup = BeautifulSoup(payload, 'html.parser')
                title = soup.title.get_text(' ', strip=True) if soup.title else resolved
                date_tag = soup.find('meta', attrs={'property': 'article:published_time'})
                date = date_tag.get('content') if date_tag else None
                content = '\n'.join(tag.get_text(' ', strip=True) for tag in soup.select('p, li, tr, article')
                                    if len(tag.get_text(' ', strip=True)) < 700)
            for entity_id, name in targets:
                matches = []
                for line in content.splitlines():
                    if not re.search(re.escape(name), line, re.I):
                        continue
                    near = line[:360]
                    if KEYWORDS.search(near) and not re.search(r'Nomination Documents', near, re.I):
                        matches.append({'url': resolved, 'title': title, 'date': date,
                                        'snippet': near[:360],
                                        'retrievedAtUtc': datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')})
                if matches:
                    result[entity_id] = matches[:3]
        except Exception:
            # A broken local source must not block the updater or erase old findings.
            continue
    return result


def main():
    anomalies = json.loads((ROOT / 'candidate-anomalies.json').read_text(encoding='utf-8'))
    configs = json.loads((ROOT / 'config/local-sources.json').read_text(encoding='utf-8'))
    previous = json.loads(OUT.read_text(encoding='utf-8')) if OUT.exists() else {}
    previous.update(scan(anomalies, configs))
    OUT.write_text(json.dumps(previous, ensure_ascii=False, separators=(',', ':')) + '\n', encoding='utf-8')
    print(f'Official text matches retained for {len(previous)} candidate identities')


if __name__ == '__main__':
    main()
