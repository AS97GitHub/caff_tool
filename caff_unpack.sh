#!/bin/bash

ROOT_DIR="${1:-.}"
TOOL_PATH="$(dirname "$0")/caff_tool.py"
PY="$(command -v python3 || command -v python)"

if [ -z "$PY" ]; then
    echo "Error: python3 not found in PATH."
    exit 1
fi

echo "Recursively unpacking 'default.bin' -> 'default.toml' in '$ROOT_DIR'..."
find "$ROOT_DIR" -type f -name "default.bin" -print0 | while IFS= read -r -d '' bin_file; do
    dir=$(dirname "$bin_file")
    toml_file="$dir/default.toml"
    echo "Unpacking: $bin_file -> $toml_file"
    "$PY" "$TOOL_PATH" unpack "$bin_file" "$toml_file"
done

echo ""
read -n 1 -s -r -p "Press any key to continue..."
echo ""
