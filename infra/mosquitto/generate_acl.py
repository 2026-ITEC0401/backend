#!/usr/bin/env python3
"""Print exact per-household Mosquitto ACL blocks to stdout."""

from __future__ import annotations

import argparse
import re


SAFE_ID = re.compile(r"^[A-Za-z0-9_-]+$")
DEVICES = ("rpi-001", "esp32_1", "esp32_2", "esp32_3")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("household_id")
    arguments = parser.parse_args()
    household = arguments.household_id
    if not SAFE_ID.fullmatch(household):
        raise SystemExit("household_id에는 영문자, 숫자, _, -만 사용할 수 있습니다.")

    for device in DEVICES:
        print(f"user {household}__{device}")
        print(f"topic read hearo/{household}/devices/{device}/command")
        print(f"topic write hearo/{household}/devices/{device}/ack")
        print(f"topic write hearo/{household}/devices/{device}/state")
        print(f"topic write hearo/{household}/devices/{device}/presence")
        if device == "rpi-001":
            print(f"topic write hearo/{household}/alerts")
            print("topic write hearo/alert")
            print("topic write hearo/log")
        else:
            print(f"topic read hearo/{household}/alerts")
        print()


if __name__ == "__main__":
    main()
