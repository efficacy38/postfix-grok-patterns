#!/usr/bin/env python3
"""
Generate Vector unit tests from Logstash grok pattern test YAML files.

This script reads:
  1. postfix.grok    – to extract custom pattern aliases
  2. test/*.yaml     – to get test cases (pattern, data, expected results)

And generates:
  vector/vector_tests.yaml  – Vector unit test configuration

The generated file uses VRL parse_groks() with the custom aliases derived from
postfix.grok, so that the same grok patterns can be validated against both
Logstash (jls-grok) and Vector (Datadog grok implementation).

References:
  - Vector unit tests:   https://vector.dev/docs/reference/configuration/unit-tests/
  - VRL parse_groks:     https://vector.dev/docs/reference/vrl/functions/#parse_groks
  - Vector VRL overview: https://vector.dev/docs/reference/vrl/
"""

import os
import re
import sys

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.dirname(SCRIPT_DIR)
GROK_FILE = os.path.join(REPO_DIR, "postfix.grok")
TEST_DIR = os.path.join(REPO_DIR, "test")
OUTPUT_FILE = os.path.join(SCRIPT_DIR, "vector_tests.yaml")


# ---------------------------------------------------------------------------
# Parse postfix.grok → dict of pattern_name -> pattern_definition
# ---------------------------------------------------------------------------
def parse_postfix_grok(filepath):
    """Return an ordered list of (name, definition) pairs from the grok file."""
    patterns = []
    seen = set()
    with open(filepath, encoding="utf-8") as fh:
        for line in fh:
            line = line.rstrip("\n")
            stripped = line.strip()
            # skip blank lines and comment lines
            if not stripped or stripped.startswith("#"):
                continue
            # split on the first run of whitespace
            parts = stripped.split(None, 1)
            if len(parts) != 2:
                continue
            name, definition = parts
            if name not in seen:
                seen.add(name)
                patterns.append((name, definition))
    return patterns


# ---------------------------------------------------------------------------
# Escape a regex string for use inside a VRL double-quoted string literal.
# In VRL strings:  \"  and  \\  need escaping; everything else is literal.
# Also escape '$' as '$$' so that Vector's pre-parse environment variable
# substitution (which runs before YAML/VRL parsing) does not try to expand
# tokens like '$EXTENSION' as env vars.
# Reference: https://vector.dev/docs/reference/configuration/
# ---------------------------------------------------------------------------
def vrl_escape(s):
    return s.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")


# ---------------------------------------------------------------------------
# Read a YAML test file.  We use a minimal hand-rolled parser to avoid adding
# a PyYAML dependency inside the Vector Docker image.
# ---------------------------------------------------------------------------
def parse_test_yaml(filepath):
    """
    Return a dict with keys: pattern, data, results.
    results is a dict of field_name -> string_value.
    """
    with open(filepath, encoding="utf-8") as fh:
        content = fh.read()

    result = {"pattern": None, "data": None, "results": {}}

    # --- pattern ---
    m = re.search(r'^pattern:\s*(.+)$', content, re.MULTILINE)
    if not m:
        return None
    pattern_raw = m.group(1).strip().strip('"').strip("'")
    result["pattern"] = pattern_raw

    # --- data ---
    m = re.search(r'^data:\s*(.+)$', content, re.MULTILINE)
    if not m:
        return None
    data_raw = m.group(1).strip()
    # remove surrounding quotes if present (handles both " and ')
    if (data_raw.startswith('"') and data_raw.endswith('"')) or \
       (data_raw.startswith("'") and data_raw.endswith("'")):
        data_raw = data_raw[1:-1]
    result["data"] = data_raw

    # --- results block (YAML mapping under "results:") ---
    results_m = re.search(r'^results:\s*\n((?:[ \t]+.+\n?)*)', content, re.MULTILINE)
    if results_m:
        block = results_m.group(1)
        for line in block.splitlines():
            # match "  key: value" — value may be quoted
            km = re.match(r'^[ \t]+([\w\-]+):\s*(.+)$', line)
            if km:
                key = km.group(1)
                val = km.group(2).strip()
                # strip surrounding quotes
                if (val.startswith('"') and val.endswith('"')) or \
                   (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                result["results"][key] = val

    return result


# ---------------------------------------------------------------------------
# Normalise a grok pattern string to a transform-safe YAML key.
# e.g. "^%{POSTFIX_SMTPD}$" → "postfix_smtpd"
# ---------------------------------------------------------------------------
def pattern_to_transform_name(pattern_str):
    # strip anchors and whitespace
    name = pattern_str.strip().lstrip("^").rstrip("$")
    # extract the pattern name from %{PATTERN_NAME}
    m = re.match(r'%\{([A-Za-z0-9_]+)\}', name)
    if m:
        return m.group(1).lower()
    # fallback: sanitise the string
    return re.sub(r'[^a-z0-9_]', '_', name.lower()).strip("_")


def strip_anchors(pattern_str):
    """Strip leading ^ and trailing $ from a pattern string."""
    return pattern_str.strip().lstrip("^").rstrip("$")


# ---------------------------------------------------------------------------
# Generate the VRL aliases object literal (multi-line string)
# ---------------------------------------------------------------------------
def build_aliases_block(patterns, indent=10):
    pad = " " * indent
    lines = []
    for name, definition in patterns:
        escaped = vrl_escape(definition)
        lines.append(f'{pad}"{name}": "{escaped}"')
    return ",\n".join(lines)


# ---------------------------------------------------------------------------
# Make a valid YAML single-quoted scalar for the 'message' field.
# Single-quoted YAML scalars only need '' to escape a single quote.
# Also escape '$' as '$$' to prevent Vector from treating it as an
# environment variable reference (Vector expands $VAR and ${VAR} in configs).
# Reference: https://vector.dev/docs/reference/configuration/
# ---------------------------------------------------------------------------
def yaml_single_quote(s):
    s = s.replace("$", "$$")  # prevent Vector env-var expansion
    return "'" + s.replace("'", "''") + "'"


# ---------------------------------------------------------------------------
# Return the VRL field-access expression for a capture name.
# Names containing characters other than [a-zA-Z0-9_] must be quoted.
# ---------------------------------------------------------------------------
def vrl_field(name):
    if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        return f'.{name}'
    return f'"{name}"'  # quoted path segment – accessed as .<quoted>


def vrl_field_access(name):
    """Return e.g. `.foo` or `."foo-bar"` for use in assert_eq!."""
    if re.match(r'^[a-zA-Z_][a-zA-Z0-9_]*$', name):
        return f'.{name}'
    return f'."{ name }"'


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    # 1. Load patterns from postfix.grok
    if not os.path.isfile(GROK_FILE):
        print(f"ERROR: cannot find {GROK_FILE}", file=sys.stderr)
        sys.exit(1)

    all_patterns = parse_postfix_grok(GROK_FILE)
    aliases_block = build_aliases_block(all_patterns)
    aliases_oneliner = "{\n" + aliases_block + "\n        }"

    # 2. Load test YAML files
    yaml_files = sorted(
        f for f in os.listdir(TEST_DIR)
        if f.endswith(".yaml") and not os.path.isdir(os.path.join(TEST_DIR, f))
    )

    # group tests by the grok pattern they use
    # key: strip-anchored pattern string  e.g. "%{POSTFIX_SMTPD}"
    from collections import OrderedDict
    pattern_to_tests = OrderedDict()   # pattern_str -> list of (test_name, data, results)

    for yf in yaml_files:
        path = os.path.join(TEST_DIR, yf)
        info = parse_test_yaml(path)
        if info is None or info["data"] is None:
            print(f"WARN: skipping {yf} (could not parse)", file=sys.stderr)
            continue
        pat = strip_anchors(info["pattern"])
        test_name = os.path.splitext(yf)[0]
        if pat not in pattern_to_tests:
            pattern_to_tests[pat] = []
        pattern_to_tests[pat].append((test_name, info["data"], info["results"]))

    # 3. Generate vector_tests.yaml
    lines = []
    lines.append("# Generated by vector/generate_vector_tests.py — do not edit by hand.")
    lines.append("#")
    lines.append("# Vector unit tests for Postfix grok patterns.")
    lines.append("#")
    lines.append("# References:")
    lines.append("#   Vector unit tests:   https://vector.dev/docs/reference/configuration/unit-tests/")
    lines.append("#   VRL parse_groks:     https://vector.dev/docs/reference/vrl/functions/#parse_groks")
    lines.append("#   Vector VRL overview: https://vector.dev/docs/reference/vrl/")
    lines.append("")

    # --- transforms section ---
    lines.append("transforms:")
    transform_names = {}   # pattern_str -> yaml_key

    for pat in pattern_to_tests:
        tname = "parse_" + pattern_to_transform_name(pat)
        # ensure uniqueness
        base = tname
        suffix = 0
        while tname in transform_names.values():
            suffix += 1
            tname = f"{base}_{suffix}"
        transform_names[pat] = tname

        lines.append(f"  {tname}:")
        lines.append(f"    type: remap")
        lines.append(f"    inputs: []")
        lines.append(f"    source: |-")
        lines.append(f'      . |= parse_groks!(')
        lines.append(f'        .message,')
        lines.append(f'        patterns: ["{pat}"],')
        lines.append(f'        aliases: {{')
        lines.append(aliases_block)
        lines.append(f'        }}')
        lines.append(f'      )')
        lines.append("")

    # --- tests section ---
    lines.append("tests:")

    for pat, tests in pattern_to_tests.items():
        tname = transform_names[pat]
        for test_name, data, results in tests:
            lines.append(f"  - name: {test_name}")
            lines.append(f"    inputs:")
            lines.append(f"      - insert_at: {tname}")
            lines.append(f"        type: log")
            lines.append(f"        log_fields:")
            lines.append(f"          message: {yaml_single_quote(data)}")
            lines.append(f"    outputs:")
            lines.append(f"      - extract_from: {tname}")
            lines.append(f"        conditions:")
            lines.append(f"          - type: vrl")
            lines.append(f"            source: |-")
            if results:
                for field, expected in sorted(results.items()):
                    access = vrl_field_access(field)
                    escaped_expected = vrl_escape(str(expected))
                    lines.append(
                        f'              assert_eq!({access}, "{escaped_expected}", '
                        f'"{test_name}: {field} mismatch")'
                    )
            else:
                # No specific field checks — just ensure parse succeeded (event exists)
                lines.append(f'              true')
            lines.append("")

    output = "\n".join(lines)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as fh:
        fh.write(output)

    total_tests = sum(len(v) for v in pattern_to_tests.values())
    print(
        f"Generated {OUTPUT_FILE} with "
        f"{len(pattern_to_tests)} transforms and {total_tests} tests."
    )


if __name__ == "__main__":
    main()
