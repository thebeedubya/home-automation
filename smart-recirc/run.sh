#!/bin/bash
echo "$(date): Starting smart-recirc" >&2
cd /Users/bradwood/Projects/Home/smart-recirc || { echo "cd failed" >&2; exit 1; }
echo "$(date): In directory $(pwd)" >&2
echo "$(date): Python is $(/usr/bin/python3 --version 2>&1)" >&2
exec /usr/bin/python3 -u controller.py observe 2>&1
