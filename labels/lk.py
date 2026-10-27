#!/usr/bin/env python3
"""Точка входа: python3 labels/lk.py <команда>. Справка: python3 labels/lk.py -h"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from labelkit.cli import main  # noqa: E402

main()
