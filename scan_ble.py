#!/usr/bin/env python3

import os
import sys

sys.path.append(os.path.dirname(__file__))

from common.util import ArgumentParser, BluetoothApp, get_connector


class App(BluetoothApp):

    def bt_evt_system_boot(self, evt):
        """EFR32 đã boot xong, bắt đầu quét BLE."""

        self.lib.bt.scanner.start(
            self.lib.bt.scanner.SCAN_PHY_SCAN_PHY_1M,
            self.lib.bt.scanner.DISCOVER_MODE_DISCOVER_GENERIC
        )

        self.log.info("Scanning started. Press Ctrl+C to stop.")

    def bt_evt_scanner_legacy_advertisement_report(self, evt):
        """EFR32 nhận được một gói Legacy Advertising."""

        print(
            f"Address={evt.address} | "
            f"RSSI={evt.rssi:4} dBm | "
            f"Address type={evt.address_type} | "
            f"Data={bytes(evt.data).hex()}"
        )


if __name__ == "__main__":
    parser = ArgumentParser(description="Simple BLE scanner using PyBGAPI")
    args = parser.parse_args()

    connector = get_connector(args)
    app = App(connector)
    app.run()
