from migen import *
from migen.genlib.cdc import MultiReg

from litex.soc.interconnect.stream import Endpoint
from litex.build.io import DDRInput

class SWOManchPHY(Module):
    def __init__(self, pads):
        self.source = source = Endpoint([('data', 8)])

        swo_a = Signal()
        swo_b = Signal()

        self.specials += DDRInput(
            clk = ClockSignal(),
            i = pads.swo,
            o1 = swo_a,
            o2 = swo_b,
        )

        edgeOutput = Signal()

        byteavail = Signal()
        byteavail_last = Signal()
        byte = Signal(8)

        self.sync += byteavail_last.eq(byteavail)

        self.comb += [
            source.data.eq(byte),
            source.valid.eq(byteavail != byteavail_last),
            #source.first.eq(1),
            #source.last.eq(1),
        ]

        swomanchif = Instance('swoManchIF',
            i_rst = ResetSignal(),
            i_SWOina = swo_a,
            i_SWOinb = swo_b,
            i_clk = ClockSignal(),
            o_edgeOutput = edgeOutput,

            o_byteAvail = byteavail,
            o_completeByte = byte,
        )

        self.specials += swomanchif
