#!/usr/bin/env python3
"""Read shard sources or expand dictionary-encoded earnings JSON to stdout."""
import argparse
import json
from pathlib import Path
import sys

ENCODED_COLUMNS = ("launch", "token")


def json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":")) + "\n").encode()


def encode_earnings(columns, rows, coverage):
    """Dictionary-encode metadata cells while preserving every row and field."""
    dictionaries = {}
    packed = [row.copy() for row in rows]
    for column in ENCODED_COLUMNS:
        if column not in columns:
            continue
        index = columns.index(column)
        values = []
        codes = {}
        for row in packed:
            value = row[index]
            if value is None:
                continue
            key = json_bytes(value)
            if key not in codes:
                codes[key] = len(values)
                values.append(value)
            row[index] = codes[key]
        dictionaries[column] = values
    return packed, {**coverage, "columnDictionaries": dictionaries}


def load_source(path):
    source = json.loads(path.read_bytes())
    dictionaries = source["coverage"].pop("columnDictionaries", {})
    for column, values in dictionaries.items():
        if column not in ENCODED_COLUMNS or not isinstance(values, list):
            raise ValueError(f"invalid dictionary for {column}")
        index = source["columns"].index(column)
        for row in source["rows"]:
            code = row[index]
            if code is None:
                continue
            if type(code) is not int or not 0 <= code < len(values):
                raise ValueError(f"invalid dictionary code for {column}: {code}")
            row[index] = values[code]
    return source


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    args = parser.parse_args()
    sys.stdout.buffer.write(json_bytes(load_source(args.source)))
