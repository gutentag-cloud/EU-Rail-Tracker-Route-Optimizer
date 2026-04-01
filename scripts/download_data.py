#!/usr/bin/env python3
"""
Download the trainline-eu/stations open dataset.
Source: https://github.com/trainline-eu/stations
"""

import os, urllib.request

URL = (
    "https://raw.githubusercontent.com/"
    "trainline-eu/stations/master/stations.csv"
)
DEST = os.path.join(os.path.dirname(__file__), "..", "data", "stations.csv")


def main():
    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    if os.path.exists(DEST):
        size = os.path.getsize(DEST)
        print(f"  stations.csv already exists ({size:,} bytes)")
        resp = input("  Re-download? [y/N] ").strip().lower()
        if resp != "y":
            return

    print(f"⬇  Downloading stations.csv ...")
    urllib.request.urlretrieve(URL, DEST)
    size = os.path.getsize(DEST)
    print(f"✅ Saved to {DEST} ({size:,} bytes)")


if __name__ == "__main__":
    main()
