#!/usr/bin/env python3
"""sync-source.py · 占位：公开版不附带任何第三方看板的取数脚本。

把你自己的数据源适配器写在这里：产出 data/paperball/announcements.jsonl 与 meta.json，
字段见 adapters/sources/README.md 与 docs/contracts.md §4。host/daily-sync.sh 每天调用本文件，
退出码 0 视为成功，非 0 视为失败（已有数据不动）。
"""
import sys

print("公开版没有内置数据源：按 adapters/sources/README.md 写你自己的适配器；"
      "先试玩可以 cp -r data/sample data/paperball", file=sys.stderr)
sys.exit(1)
