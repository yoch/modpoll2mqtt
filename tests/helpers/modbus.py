"""Shared Modbus test doubles for unit tests."""

from modpoll.modbus_task import ModbusHandler


class FakeModbusResult:
    def __init__(self, *, bits=None, registers=None, error=False):
        self.bits = bits
        self.registers = registers
        self._error = error

    def isError(self):
        return self._error


class FakeModbusMaster:
    def __init__(self, *, coils=None, bits=None, registers=None, fail_addresses=None):
        self.coils = list(coils if coils is not None else bits or [])
        self.registers = list(registers or [])
        self.fail_addresses = set(fail_addresses or [])
        self.writes = []

    def read_coils(self, address, *, count=1, device_id=1):
        return FakeModbusResult(bits=self.coils[address : address + count])

    def read_discrete_inputs(self, address, *, count=1, device_id=1):
        return FakeModbusResult(bits=self.coils[address : address + count])

    def read_holding_registers(self, address, *, count=1, device_id=1):
        if address in self.fail_addresses:
            return FakeModbusResult(error=True)
        return FakeModbusResult(registers=self.registers[address : address + count])

    def read_input_registers(self, address, *, count=1, device_id=1):
        if address in self.fail_addresses:
            return FakeModbusResult(error=True)
        return FakeModbusResult(registers=self.registers[address : address + count])

    def write_coil(self, address, value, device_id=1):
        self.writes.append(("coil", address, value))
        self.coils[address] = value
        return FakeModbusResult()

    def write_coils(self, address, values, device_id=1):
        self.writes.append(("coils", address, list(values)))
        for i, val in enumerate(values):
            self.coils[address + i] = val
        return FakeModbusResult()

    def write_register(self, address, value, device_id=1):
        self.writes.append(("register", address, value))
        self.registers[address] = value
        return FakeModbusResult()

    def write_registers(self, address, values, device_id=1):
        self.writes.append(("registers", address, list(values)))
        for i, val in enumerate(values):
            self.registers[address + i] = val
        return FakeModbusResult()


def handler_with_device(device, master):
    handler = ModbusHandler(master, "dummy.csv")
    handler.deviceList = [device]
    return handler


def log_messages(caplog, level: str) -> list[str]:
    return [r.message for r in caplog.records if r.levelname == level]
