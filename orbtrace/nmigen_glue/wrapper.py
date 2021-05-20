import nmigen
from nmigen.hdl import ir
from nmigen.back import verilog

import migen

from pathlib import Path

class Wrapper(migen.Module):
    def __init__(self, platform, name = 'nmigen_wrapper'):
        self.platform = platform
        self.name = name

        self.m = nmigen.Module()

        self.connections = []

    def connect(self, migen_sig, nmigen_sig):
        self.connections.append((migen_sig, nmigen_sig))

    def connect_domain(self, name):
        n = 'sync' if name == 'sys' else name

        self.connect(migen.ClockSignal(name), nmigen.ClockSignal(n))
        self.connect(migen.ResetSignal(name), nmigen.ResetSignal(n))

    def from_nmigen(self, nmigen_sig):
        shape = nmigen_sig.shape()
        migen_sig = migen.Signal((shape.width, shape.signed), name = nmigen_sig.name)

        self.connect(migen_sig, nmigen_sig)

        return migen_sig

    def get_instance(self):
        connections = {}

        for m, n in self.connections:
            module, name, *_ = self.nmigen_name_map[n]
            direction = self.nmigen_dir_map[n]
            s = f'{direction}_{name}'

            assert s not in connections, f'Signal {s} connected multiple times.'

            connections[s] = m

        return migen.Instance(self.name, **connections)

    def generate_verilog(self):
        ports = [n for m, n in self.connections]

        fragment = ir.Fragment.get(self.m, None).prepare(ports = ports)

        v, m = verilog.convert_fragment(fragment, name = self.name)

        self.nmigen_dir_map = fragment.ports
        self.nmigen_name_map = m

        for name, domain in fragment.domains.items():
            if domain.clk in self.nmigen_name_map:
                self.nmigen_name_map[nmigen.ClockSignal(name)] = self.nmigen_name_map[domain.clk]
            if domain.clk in self.nmigen_dir_map:
                self.nmigen_dir_map[nmigen.ClockSignal(name)] = self.nmigen_dir_map[domain.clk]
            if domain.rst in self.nmigen_name_map:
                self.nmigen_name_map[nmigen.ResetSignal(name)] = self.nmigen_name_map[domain.rst]
            if domain.rst in self.nmigen_dir_map:
                self.nmigen_dir_map[nmigen.ResetSignal(name)] = self.nmigen_dir_map[domain.rst]

        return v

    def do_finalize(self):
        verilog_filename = str(Path(self.platform.output_dir) / 'gateware' / f'{self.name}.v')

        with open(verilog_filename, 'w') as f:
            f.write(self.generate_verilog())

        self.platform.add_source(verilog_filename)

        self.specials += self.get_instance()
