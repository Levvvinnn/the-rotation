"""Generate English action word families; irregular forms share the same base verb."""

from __future__ import annotations

import re

# Regular verbs use the spelling rules below; only irregular forms are listed.
IRREGULAR = {
    'drive': ('drove', 'driven'), 'do': ('did', 'done'), 'get': ('got', 'gotten'),
    'take': ('took', 'taken'), 'buy': ('bought',), 'make': ('made',),
    'pay': ('paid',), 'sweep': ('swept',), 'feed': ('fed',), 'run': ('ran',),
}
ROOTS = (
    'drive', 'host', 'pay', 'get', 'buy', 'grab', 'cover', 'take out', 'do',
    'pick up', 'order', 'take', 'uber', 'lyft', 'cook', 'make', 'prepare',
    'wash', 'wash up', 'clean', 'vacuum', 'mop', 'sweep', 'water', 'walk',
    'feed', 'finish', 'handle', 'empty', 'fold', 'run', 'book', 'call', 'tidy', 'hoover',
)
DIRECT_TASKS = {
    'drive': 'drive', 'host': 'host', 'cook': 'cooking', 'vacuum': 'vacuuming',
    'mop': 'mopping', 'sweep': 'mopping', 'uber': 'uber', 'lyft': 'uber',
}


def forms(phrase: str) -> tuple[set[str], str]:
    root, _, suffix = phrase.partition(' ')
    suffix = ' ' + suffix if suffix else ''
    doubles = len(root) <= 4 and root != 'uber' and bool(re.search(r'[^aeiou][aeiou][^aeiouwxy]$', root))
    if root.endswith('e') and not root.endswith('ee'):
        regular_past, active = root + 'd', root[:-1] + 'ing'
    elif len(root) > 1 and root.endswith('y') and root[-2] not in 'aeiou':
        regular_past, active = root[:-1] + 'ied', root + 'ing'
    elif doubles:
        regular_past, active = root + root[-1] + 'ed', root + root[-1] + 'ing'
    else:
        regular_past, active = root + 'ed', root + 'ing'
    past = {word + suffix for word in IRREGULAR.get(root, (regular_past,))}
    return past, active + suffix


COMPLETED_ROOTS: dict[str, str] = {}
ACTIVE_ROOTS: dict[str, str] = {}
WORD_ROOTS: dict[str, str] = {}
for root in ROOTS:
    completed, active = forms(root)
    for word in completed:
        COMPLETED_ROOTS[word] = root
    ACTIVE_ROOTS[active] = root
    for word in {root, active} | completed:
        if ' ' not in word:
            WORD_ROOTS[word] = root
COMPLETED_ROOTS['meal prepped'] = 'meal prep'
ACTIVE_ROOTS['meal prepping'] = 'meal prep'


def pattern(words) -> str:
    return '(?:' + '|'.join(re.escape(word) for word in sorted(words, key=lambda word: (-len(word), word))) + ')'


def normalized_phrase(text: str) -> str:
    words = re.findall(r"\w+(?:'\w+)?", text.lower())
    return ' '.join(WORD_ROOTS.get(word, word) for word in words if word not in {'the', 'a', 'an'})
