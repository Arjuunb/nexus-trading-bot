import ast
import hashlib
import json
from pathlib import Path
from acceptance_freeze import proposal


def test_proposed_startup_delta_keeps_original_strategy_and_fill_methods_pinned():
    root = Path(__file__).parents[1]
    original = json.loads((root / 'data/pr6_real_paper_freeze.json').read_text())
    delta = proposal(root)
    count = 0
    for path, change in delta['files'].items():
        if path in original['sha256']:
            assert change['prior_sha256'] == original['sha256'][path]
        text = (root / path).read_text()
        assert hashlib.sha256(text.encode()).hexdigest() == change['sha256']
        actual = {}
        for parent in ast.parse(text).body:
            nodes = parent.body if isinstance(parent, ast.ClassDef) else [parent]
            for node in nodes:
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    name = parent.name+'.'+node.name if isinstance(parent, ast.ClassDef) else node.name
                    actual[name] = hashlib.sha256(ast.get_source_segment(text, node).encode()).hexdigest()
        expected = dict(change['unchanged_method_sha256'])
        expected.update({name: update['sha256'] for name, update in change['method_updates'].items()})
        assert actual == expected, path
        count += len(change['unchanged_method_sha256'])
    assert count > 150
    for path, expected in original['sha256'].items():
        if path not in delta['files']:
            assert hashlib.sha256((root / path).read_bytes()).hexdigest() == expected, path
