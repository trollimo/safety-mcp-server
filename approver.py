#!/usr/bin/env python3
"""Исполнитель операций записи.

  python approver.py serve [--bind 0.0.0.0] [--port 8765]
  python approver.py enroll [--account имя] [--reset]
"""
import sys

from safety_mcp.approver import main

if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
