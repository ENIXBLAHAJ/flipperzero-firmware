#!/usr/bin/env python3
import typing
import subprocess
import logging
import time
import os
import select

from abc import ABC, abstractmethod
from dataclasses import dataclass
from flipper.app import App


class Programmer(ABC):
    @abstractmethod
    def flash(self, bin: str) -> bool:
        pass

    @abstractmethod
    def probe(self) -> bool:
        pass

    @abstractmethod
    def get_name(self) -> str:
        pass


@dataclass
class OpenOCDInterface:
    name: str
    file: str
    serial_cmd: str
    additional_args: typing.Optional[list[str]] = None


class OpenOCDProgrammer(Programmer):
    def __init__(self, interface: OpenOCDInterface):
        self.interface = interface
        self.logger = logging.getLogger("OpenOCD")

    def _add_file(self, params: list[str], file: str):
        params.append("-f")
        params.append(file)

    def _add_command(self, params: list[str], command: str):
        params.append("-c")
        params.append(command)

    def _add_serial(self, params: list[str], serial: str):
        self._add_command(params, f"{self.interface.serial_cmd} {serial}")

    def flash(self, bin: str) -> bool:
        i = self.interface

        openocd_launch_params = ["openocd"]
        self._add_file(openocd_launch_params, i.file)
        if i.additional_args:
            for a in i.additional_args:
                self._add_command(openocd_launch_params, a)
        self._add_file(openocd_launch_params, "target/stm32wbx.cfg")
        self._add_command(openocd_launch_params, "init")
        self._add_command(openocd_launch_params, f"program {bin} reset exit 0x8000000")

        self.logger.debug(f"Launching: {' '.join(openocd_launch_params)}")

        process = subprocess.Popen(
            openocd_launch_params,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        while process.poll() is None:
            time.sleep(0.25)
            print(".", end="", flush=True)
        print()

        success = process.returncode == 0

        if not success:
            self.logger.error("OpenOCD failed to flash")
            if process.stdout:
                self.logger.error(process.stdout.read().decode("utf-8").strip())

        return success

    def probe(self) -> bool:
        i = self.interface

        openocd_launch_params = ["openocd"]
        self._add_file(openocd_launch_params, i.file)
        if i.additional_args:
            for a in i.additional_args:
                self._add_command(openocd_launch_params, a)
        self._add_file(openocd_launch_params, "target/stm32wbx.cfg")
        self._add_command(openocd_launch_params, "init")
        self._add_command(openocd_launch_params, "exit")

        self.logger.debug(f"Launching: {' '.join(openocd_launch_params)}")

        process = subprocess.Popen(
            openocd_launch_params,
            stderr=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )

        # Wait for OpenOCD to end and get the return code
        process.wait()
        found = process.returncode == 0

        if not found:
            if process.stderr:
                self.logger.debug(process.stderr.read().decode("utf-8").strip())

        return found

    def get_name(self) -> str:
        return self.interface.name


class BlackmagicUSBProgrammer(Programmer):
    def __init__(self):
        self.logger = logging.getLogger("BlackmagicUSB")
        self.port: typing.Optional[str] = None

    def _add_command(self, params: list[str], command: str):
        params.append("-ex")
        params.append(command)

    def flash(self, bin: str) -> bool:
        if not self.port:
            if not self.probe():
                return False

        # We can convert .bin to .elf with objcopy:
        # arm-none-eabi-objcopy -I binary -O elf32-littlearm --change-section-address=.data=0x8000000 -B arm -S app.bin app.elf
        # But I choose to use the .elf file directly because we are flashing our own firmware and it always has an elf predecessor.
        elf = bin.replace(".bin", ".elf")
        if not os.path.exists(elf):
            self.logger.error(
                f"Sorry, but Blackmagic can't flash .bin file, and {elf} doesn't exist"
            )
            return False

        # arm-none-eabi-gdb build/f7-firmware-D/firmware.bin
        # -ex 'target extended-remote /dev/cu.usbmodem21201'
        # -ex "set confirm off"
        # -ex 'monitor swdp_scan'
        # -ex 'attach 1'
        # -ex 'set mem inaccessible-by-default off'
        # -ex 'load'
        # -ex 'compare-sections'
        # -ex 'quit'

        gdb_launch_params = ["arm-none-eabi-gdb", elf]
        self._add_command(gdb_launch_params, f"target extended-remote {self.port}")
        self._add_command(gdb_launch_params, "set confirm off")
        self._add_command(gdb_launch_params, "monitor swdp_scan")
        self._add_command(gdb_launch_params, "attach 1")
        self._add_command(gdb_launch_params, "set mem inaccessible-by-default off")
        self._add_command(gdb_launch_params, "load")
        self._add_command(gdb_launch_params, "compare-sections")
        self._add_command(gdb_launch_params, "quit")

        self.logger.debug(f"Launching: {' '.join(gdb_launch_params)}")

        process = subprocess.Popen(
            gdb_launch_params,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )

        while process.poll() is None:
            time.sleep(0.5)
            print(".", end="", flush=True)
        print()

        if not process.stdout:
            return False

        output = process.stdout.read().decode("utf-8").strip()
        flashed = "Loading section .text," in output

        # Check flash verification
        # Look for "MIS-MATCHED!" in the output
        if "MIS-MATCHED!" in output:
            flashed = False

        if not flashed:
            self.logger.error("Blackmagic failed to flash")
            self.logger.error(output)

        return flashed

    def _find_probe(self):
        import serial.tools.list_ports as list_ports

        ports = list(list_ports.grep("blackmagic"))
        if len(ports) == 0:
            # Blackmagic probe serial port not found, will be handled later
            pass
        elif len(ports) > 2:
            raise Exception("More than one Blackmagic probe found")
        else:
            # If you're getting any issues with auto lookup, uncomment this
            # print("\n".join([f"{p.device} {vars(p)}" for p in ports]))
            return sorted(ports, key=lambda p: f"{p.location}_{p.name}")[0]

    def probe(self) -> bool:
        if not (probe := self._find_probe()):
            return False

        if os.name == "nt":
            self.port = f"\\\\.\\{probe.device}"
        self.port = probe.device
        return True

    def get_name(self) -> str:
        return "blackmagic_usb"


programmers: list[Programmer] = [
    OpenOCDProgrammer(
        OpenOCDInterface(
            "jlink", "interface/jlink.cfg", "jlink_serial", ["transport select swd"]
        ),
    ),
    OpenOCDProgrammer(
        OpenOCDInterface(
            "cmsis-dap",
            "interface/cmsis-dap.cfg",
            "cmsis_dap_serial",
            ["transport select swd"],
        ),
    ),
    OpenOCDProgrammer(
        OpenOCDInterface(
            "stlink", "interface/stlink.cfg", "hla_serial", ["transport select hla_swd"]
        ),
    ),
    BlackmagicUSBProgrammer(),
]


class Main(App):
    def init(self):
        self.subparsers = self.parser.add_subparsers(help="sub-command help")
        self.parser_flash = self.subparsers.add_parser("flash", help="Flash a binary")
        self.parser_flash.add_argument(
            "--bin",
            type=str,
            help="Binary to flash",
            required=True,
        )
        self.parser_flash.add_argument(
            "--interface",
            choices=[i.get_name() for i in programmers],
            type=str,
            help="Interface to use",
        )
        self.parser_flash.set_defaults(func=self.flash)

    def _search_interface(self) -> list[Programmer]:
        found_programmers = []

        for p in programmers:
            name = p.get_name()
            self.logger.debug(f"Trying {name}")

            if p.probe():
                self.logger.debug(f"Found {name}")
                found_programmers += [p]
            else:
                self.logger.debug(f"Failed to probe {name}")

        return found_programmers

    def flash(self):
        strat_time = time.time()

        if not os.path.exists(self.args.bin):
            self.logger.error(f"Binary file not found: {self.args.bin}")
            return 1

        if self.args.interface:
            interfaces = [p for p in programmers if p.get_name() == self.args.interface]
        else:
            self.logger.info(f"Probing for interfaces...")
            interfaces = self._search_interface()

            if len(interfaces) == 0:
                self.logger.error("No interface found")
                return 1

            if len(interfaces) > 1:
                self.logger.error("Multiple interfaces found: ")
                self.logger.error(
                    f"Please specify '--interface={[i.get_name() for i in interfaces]}'"
                )
                return 1

        interface = interfaces[0]
        self.logger.info(f"Flashing {self.args.bin} using {interface.get_name()}")
        if not interface.flash(self.args.bin):
            self.logger.error("Failed to flash")
            return 1

        self.logger.info("Flashed successfully in %.2fs" % (time.time() - strat_time))
        return 0


if __name__ == "__main__":
    Main()()