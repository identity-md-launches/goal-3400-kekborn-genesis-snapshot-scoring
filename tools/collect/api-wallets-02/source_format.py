"""Lossless JSON column dictionaries for repeated wallet history values."""
import argparse
import json
from pathlib import Path
import sys

DICTIONARY_COLUMNS = {
    "earnings.json": {"launch": "value", "token": "value"},
    "seats.json": {"jobsBeforeT": "items"},
}


def json_text(value):
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False) + "\n"


def encode_source(data, modes):
    """Replace values with zero-based indices, preserving nulls and row order."""
    if not modes:
        return data
    rows = [row.copy() for row in data["rows"]]
    dictionaries = {}
    for column, mode in modes.items():
        if column not in data["columns"]:
            continue
        index = data["columns"].index(column)
        values = []
        indices = {}

        def intern(value):
            if value is None:
                return None
            key = json_text(value)
            if key not in indices:
                indices[key] = len(values)
                values.append(value)
            return indices[key]

        for row in rows:
            value = row[index]
            if mode == "items" and value is not None:
                row[index] = [intern(item) for item in value]
            else:
                row[index] = intern(value)
        dictionaries[column] = {"mode": mode, "values": values}
    if not dictionaries:
        return data
    return {**data, "coverage": {**data["coverage"], "rowDictionaries": dictionaries}, "rows": rows}


def decode_source(data):
    """Expand a source document to its original literal rows."""
    coverage = data["coverage"].copy()
    dictionaries = coverage.pop("rowDictionaries", None)
    if dictionaries is None:
        return data
    rows = [row.copy() for row in data["rows"]]
    for column, dictionary in dictionaries.items():
        index = data["columns"].index(column)
        mode, values = dictionary["mode"], dictionary["values"]
        if mode not in {"value", "items"}:
            raise ValueError(f"{column}: unknown dictionary mode {mode!r}")

        def lookup(ref):
            if ref is None:
                return None
            if type(ref) is not int or not 0 <= ref < len(values):
                raise ValueError(f"{column}: invalid dictionary reference {ref!r}")
            return values[ref]

        for row in rows:
            ref = row[index]
            if mode == "items" and ref is not None:
                if not isinstance(ref, list):
                    raise ValueError(f"{column}: expected a list of dictionary references")
                row[index] = [lookup(item) for item in ref]
            else:
                row[index] = lookup(ref)
    return {**data, "coverage": coverage, "rows": rows}


def main():
    parser = argparse.ArgumentParser(description="Expand a wallet shard JSON source to literal rows.")
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    with args.source.open(encoding="utf-8") as source:
        sys.stdout.write(json_text(decode_source(json.load(source))))


if __name__ == "__main__":
    main()
