from nmigen                  import *
from .dbgIF                   import DBGIF

# Principle of operation
# ======================
#
# This module takes frames from the USB handler, parses them and sends them to the dbgif below for
# processing. In general this layer avoids doing any manipulation of the line, that is all handled
# below, with the intention of being able to replace cmsis-dap with another dap controller if
# needed.
#
# Communication with the dbgif is via a register and flag mechanism. Registers are filled with the
# appropriate information, then 'go' is set. When the dbgif accepts the command it drops 'done'
# and this layer can then release 'go'. When the command finishes 'done' is set true again. This
# interface can work over a cdc and, indeed, dbgif is generally not in the same domain as cmsis_dap.
#
# Default configuration information
# =================================

DAP_CONNECT_DEFAULT      = 1                # Default connect is SWD
DAP_VERSION_STRING       = Cat(C(0x31,8),C(0x2e,8),C(0x30,8),C(0x30,8),C(0,8))
DAP_CAPABILITIES         = 0x01             # SWD Debug only (for now)
DAP_TD_TIMER_FREQ        = 0x3B9ACA00       # 1uS resolution timer
DAP_TB_SIZE              = 500              # 500 bytes in trace buffer
DAP_MAX_PACKET_COUNT     = 1                # 1 max packet count
DAP_V1_MAX_PACKET_SIZE   = 64
DAP_V2_MAX_PACKET_SIZE   = 500

# CMSIS-DAP Protocol Messages
# ===========================

DAP_Info                 = 0x00
DAP_HostStatus           = 0x01
DAP_Connect              = 0x02
DAP_Disconnect           = 0x03
DAP_TransferConfigure    = 0x04
DAP_Transfer             = 0x05
DAP_TransferBlock        = 0x06
DAP_TransferAbort        = 0x07
DAP_WriteABORT           = 0x08
DAP_Delay                = 0x09
DAP_ResetTarget          = 0x0a
DAP_SWJ_Pins             = 0x10
DAP_SWJ_Clock            = 0x11
DAP_SWJ_Sequence         = 0x12
DAP_SWD_Configure        = 0x13
DAP_JTAG_Sequence        = 0x14
DAP_JTAG_Configure       = 0x15
DAP_JTAG_IDCODE          = 0x16
DAP_SWO_Transport        = 0x17
DAP_SWO_Mode             = 0x18
DAP_SWO_Baudrate         = 0x19
DAP_SWO_Control          = 0x1a
DAP_SWO_Status           = 0x1b
DAP_SWO_Data             = 0x1c
DAP_SWD_Sequence         = 0x1d
DAP_SWO_ExtendedStatus   = 0x1e
DAP_ExecuteCommands      = 0x7f

DAP_QueueCommands        = 0x7e
DAP_Invalid              = 0xff

# Commands to the dbgIF
# =====================

CMD_RESET                = 0
CMD_PINS_WRITE           = 1
CMD_TRANSACT             = 2
CMD_SET_SWD              = 3
CMD_SET_JTAG             = 4
CMD_SET_SWJ              = 5
CMD_SET_PWRDOWN          = 6
CMD_SET_CLK              = 7
CMD_SET_CFG              = 8
CMD_WAIT                 = 9
CMD_CLR_ERR              = 10
CMD_SET_RST_TMR          = 11
CMD_SET_TFR_CFG          = 12

# TODO/Done
# =========

# DAP_Info               : Done
# DAP_Hoststatus         : Done (But not tied to h/w)
# DAP_Connect            : Done + Tested for SWD, Not for JTAG
# DAP_Disconnect         : Done
# DAP_WriteABORT         : Done
# DAP_Delay              : Done
# DAP_ResetTarget        : Done
# DAP_SWJ_Pins           : Done
# DAP_SWJ_Clock          : Done
# DAP_SWJ_Sequence       : Done
# DAP_SWD_Configure      : Done
# DAP_SWD_Sequence       :
# DAP_SWO_Transport      :
# DAP_SWO_Mode           :
# DAP_SWO_Baudrate       :
# DAP_SWO_Control        :
# DAP_SWO_Status         :
# DAP_SWO_ExtendedStatus :
# DAP_SWO_Data           :
# DAP_JTAG_Sequence      :
# DAP_JTAG_Configure     :
# DAP_JTAG_IDCODE        :
# DAP_Transfer_Configure : Done
# DAP_Transfer           : Done (Masking done, not tested)
# DAP_TransferBlock      : Done
# DAP_TransferAbort      : Done
# DAP_ExecuteCommands    :
# DAP_QueueCommands      :

# This is the RAM used to store responses before they are sent back to the host
# =============================================================================

class WideRam(Elaboratable):
    def __init__(self):
        self.adr   = Signal(12)
        self.dat_r = Signal(32)
        self.dat_w = Signal(32)
        self.we    = Signal()
        self.mem   = Memory(width=32, depth=DAP_V2_MAX_PACKET_SIZE, init=[0xdeadbeef])

    def elaborate(self, platform):
        m = Module()
        m.submodules.rdport = rdport = self.mem.read_port()
        m.submodules.wrport = wrport = self.mem.write_port()
        m.d.comb += [
            rdport.addr.eq(self.adr),
            wrport.addr.eq(self.adr),
            self.dat_r.eq(rdport.data),
            wrport.data.eq(self.dat_w),
            wrport.en.eq(self.we),
        ]
        return m

# This is the CMSIS-DAP handler itself
# ====================================

class CMSIS_DAP(Elaboratable):
    def __init__(self, streamIn, streamOut, dbgpins, v2Indication):
        # External interface
        self.running      = Signal()       # Flag for if target is running
        self.connected    = Signal()       # Flag for if target is connected

        self.isV2         = v2Indication
        self.streamIn     = streamIn
        self.streamOut    = streamOut
        self.isV1         = Signal()
        self.rxBlock      = Signal( 7*8 )  # Longest message we pickup is 6 bytes + command
        self.rxLen        = Signal(3)      # Rxlen to pick up
        self.rxedLen      = Signal(3)      # Rxlen picked up so far
        self.swjbits      = Signal(8)      # Number of bits of SWJ remaining outstanding

        self.txBlock      = Signal( 14*8 ) # Response to be returned
        self.txLen        = Signal(16)     # Length of response to be returned
        self.txedLen      = Signal(16)     # Length of response that has been returned so far
        self.busy         = Signal()       # Indicator that we can't receive USB traffic at the moment

        self.maxSWObytes  = Signal(16)     # Maximum number of receivable SWO bytes from protocol packet
        self.txb          = Signal(4)      # State of various orthogonal state machines

        # Support for SWJ_Sequence
        self.bitcount     = Signal(3)      # Bitcount in transmission sequence
        # Support for JTAG_Sequence
        self.tmsValue     = Signal()       # TMS value while performing JTAG sequence
        self.tdoCapture   = Signal()       # Are we capturing TDO when performing JTAG sequence
        self.tdiData      = Signal(8)      # TDI being sent out
        self.tdoCount     = Signal(7)      # Count of tdi bits being sent (in txBlock)
        self.seqCount     = Signal(8)      # Number of sequences that follow
        self.tckCycles    = Signal(6)      # Number of tckCycles in this sequence
        self.bytebits     = Signal(4)      # Number of bits in this byte that have been transmitted

        # Support for DAP_Transfer
        self.dapIndex     = Signal(8)      # Index of selected JTAG device
        self.transferCount= Signal(16)     # Number of transfers 1..65535

        self.mask         = Signal(32)     # Match mask register

        self.retries      = Signal(16)     # Retry counter for WAIT
        self.matchretries = Signal(16)     # Retry counter for Value Matching

        self.tfrReq       = Signal(8)      # Transfer request from controller
        self.tfrData      = Signal(32)     # Transfer data from controller

        # CMSIS-DAP Configuration info
        self.ndev         = Signal(8)      # Number of devices in signal chain
        self.irlength     = Signal(8)      # JTAG IR register length for each device

        self.waitRetry    = Signal(16)     # Number of transfer retries after WAIT response
        self.matchRetry   = Signal(16)     # Number of retries on reads with Value Match in DAP_Transfer

        self.dbgpins      = dbgpins
    # -------------------------------------------------------------------------------------
    def RESP_Invalid(self, m):
        # Simply transmit an 'invalid' packet back
        m.d.usb += [ self.txBlock.word_select(0,8).eq(C(DAP_Invalid,8)), self.txLen.eq(1), self.busy.eq(1) ]
        m.next = 'RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_Info(self, m):
        # <b:0x00> <b:requestId>
        # Transmit requested information packet back
        m.next = 'RESPOND'

        with m.Switch(self.rxBlock.word_select(1,8)):
            # These cases are not implemented in this firmware
            # Get the Vendor ID, Product ID, Serial Number, Target Device Vendor, Target Device Name
            with m.Case(0x01, 0x02, 0x03, 0x05, 0x06):
                m.d.usb += [ self.txLen.eq(2), self.txBlock[8:16].eq(Cat(C(0,8))) ]

            with m.Case(0x04): # Get the CMSIS-DAP Firmware Version (string)
                m.d.usb += [ self.txLen.eq(7), self.txBlock[8:56].eq(Cat(C(5,8),DAP_VERSION_STRING))]
            with m.Case(0xF0): # Get information about the Capabilities (BYTE) of the Debug Unit
                m.d.usb+=[self.txLen.eq(3), self.txBlock[8:24].eq(Cat(C(1,8),C(DAP_CAPABILITIES,8)))]
            with m.Case(0xF1): # Get the Test Domain Timer parameter information
                m.d.usb+=[self.txLen.eq(6), self.txBlock[8:56].eq(Cat(C(8,8),C(DAP_TD_TIMER_FREQ,32)))]
            with m.Case(0xFD): # Get the SWO Trace Buffer Size (WORD)
                m.d.usb+=[self.txLen.eq(6), self.txBlock[8:48].eq(Cat(C(4,8),C(DAP_TB_SIZE,32)))]
            with m.Case(0xFE): # Get the maximum Packet Count (BYTE)
                m.d.usb+=[self.txLen.eq(6), self.txBlock[8:24].eq(Cat(C(1,8),C(DAP_MAX_PACKET_COUNT,8)))]
            with m.Case(0xFF): # Get the maximum Packet Size (SHORT).
                with m.If(self.isV2):
                    m.d.usb+=[self.txLen.eq(6), self.txBlock[8:32].eq(Cat(C(2,8),C(DAP_V2_MAX_PACKET_SIZE,16)))]
                with m.Else():
                    m.d.usb+=[self.txLen.eq(6), self.txBlock[8:32].eq(Cat(C(2,8),C(DAP_V1_MAX_PACKET_SIZE,16)))]
            with m.Default():
                self.RESP_Invalid(m)
    # -------------------------------------------------------------------------------------
    def RESP_HostStatus(self, m):
        # <b:0x01> <b:type> <b:status>
        # Set LEDs for condition of debugger
        m.next = 'RESPOND'

        with m.Switch(self.rxBlock.word_select(1,8)):
            with m.Case(0x00): # Connect LED
                m.d.usb+=self.connected.eq(self.rxBlock.word_select(2,8)==C(1,8))
            with m.Case(0x01): # Running LED
                m.d.usb+=self.running.eq(self.rxBlock.word_select(2,8)==C(1,8))
            with m.Default():
                self.RESP_Invalid(m)
    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_Connect_Setup(self, m):
        # <b:0x02> <b:Port>
        # Perform connect operation
        self.RESP_Invalid(m)

        if (DAP_CAPABILITIES&(1<<0)):
            # SWD mode is permitted
            with m.If ((((self.rxBlock.word_select(1,8))==0) & (DAP_CONNECT_DEFAULT==1)) |
                       ((self.rxBlock.word_select(1,8))==1)):
                m.d.usb += [
                    self.txBlock.word_select(0,16).eq(Cat(self.rxBlock.word_select(0,8),C(1,8))),
                    self.dbgif.command.eq(CMD_SET_SWD),
                    self.txLen.eq(2),
                    self.dbgif.go.eq(1)
                    ]
                m.next = 'DAP_Wait_Connect_Done'

        if (DAP_CAPABILITIES&(1<<1)):
            with m.If ((((self.rxBlock.word_select(1,8))==0) & (DAP_CONNECT_DEFAULT==2)) |
                       ((self.rxBlock.word_select(1,8))==2)):
                m.d.usb += [
                    self.txBlock.word_select(0,16).eq(Cat(self.rxBlock.word_select(0,8),C(2,8))),
                    self.dbgif.command.eq(CMD_SET_JTAG),
                    self.txLen.eq(2),
                    self.dbgif.go.eq(1)
                    ]
                m.next = 'DAP_Wait_Connect_Done'

    def RESP_Wait_Connect_Done(self, m):
        # Generic wait for inferior to process command
        with m.If((self.dbgif.go==1) & (self.dbg_done==0)):
            m.d.usb+=self.dbgif.go.eq(0)
        with m.If((self.dbgif.go==0) & (self.dbg_done==1)):
            m.next='RESPOND'
    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_Wait_Done(self, m):
        # Generic wait for inferior to process command
        with m.If((self.dbgif.go==1) & (self.dbg_done==0)):
            m.d.usb+=self.dbgif.go.eq(0)
        with m.If((self.dbgif.go==0) & (self.dbg_done==1)):
            m.d.usb += self.txBlock.bit_select(8,1).eq(self.dbgif.perr)
            m.next='RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_Disconnect(self, m):
        # <b:0x03>
        # Perform disconnect
        m.d.usb += [
            self.running.eq(0),
            self.connected.eq(0)
        ]
        m.next = 'RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_WriteABORT(self, m):
        # <b:0x08> <b:DapIndex> <w:AbortCode>
        # Post abort code to register
        # TODO: Add ABORT for JTAG
        m.d.usb += [
            self.dbgif.command.eq(CMD_TRANSACT),
            self.dbgif.apndp.eq(0),
            self.dbgif.rnw.eq(0),
            self.dbgif.addr32.eq(0),
            self.dbgif.dwrite.eq(self.rxBlock.bit_select(16,32)),
            self.dbgif.go.eq(1)
        ]

        m.next = 'DAP_Wait_Done'
    # -------------------------------------------------------------------------------------
    def RESP_Delay(self, m):
        # <b:0x09> <s:Delay>
        # Delay for programmed number of uS
        m.d.usb += [
            self.dbgif.dwrite.eq( Cat(self.rxBlock.bit_select(16,8),self.rxBlock.bit_select(8,8))),
            self.dbgif.command.eq( CMD_WAIT ),
            self.dbgif.go.eq(1)
        ]
        m.next = 'DAP_Wait_Done'
    # -------------------------------------------------------------------------------------
    def RESP_ResetTarget(self, m):
        # <b:0x0A>
        # Reset the target
        m.d.usb += [
            self.txBlock.bit_select(8,16).eq(Cat(C(0,8),C(0,7),C(1,1))),
            self.txLen.eq(3),
            self.dbgif.command.eq( CMD_RESET ),
            self.dbgif.go.eq(1)
        ]
        m.next = 'DAP_Wait_Done'
    # -------------------------------------------------------------------------------------
    def RESP_SWJ_Pins_Setup(self, m):
        # <b:0x10> <b:PinOutput> <b:PinSelect> <w:PinWait>
        # Control and monitor SWJ/JTAG pins
        m.d.usb += [
            self.dbgif.dwrite.eq( Cat(C(0,16),self.rxBlock.bit_select(8,16)) ),
            self.dbgif.countdown.eq( self.txBlock.bit_select(24,32) )
            ]
        m.next = 'DAP_SWJ_Pins_PROCESS';

    def RESP_SWJ_Pins_Process(self, m):
        # Spin waiting for debug interface to do its thing
        with m.If (self.dbg_done):
            m.d.usb += [
                self.txBlock.word_select(1,8).eq(self.dbgif.dread[0:8]),
                self.txLen.eq(2)
            ]
        m.next = 'RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_SWJ_Clock(self, m):
        # <0x11> <w:newclock>
        # Set clock frequency for JTAG and SWD comms
        m.d.usb += [
            self.dbgif.dwrite.eq( self.rxBlock.bit_select(8,32) ),
            self.dbgif.command.eq( CMD_SET_CLK ),
            self.dbgif.go.eq(1)
            ]
        m.next = 'DAP_Wait_Done'
    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_SWJ_Sequence_Setup(self, m):
        # <b:0x12> <b:Count> [n x <bSeqDat>.....]
        # Generate SWJ Sequence data
        m.d.usb += [
            # Number of bits to be transferred
            self.transferCount.eq(Mux(self.rxBlock.bit_select(8,8),Cat(self.rxBlock.bit_select(8,8),C(0,8)),C(256,16))),
            self.txb.eq(0),

            # Setup to have control over swdo, swclk and swwr (set for output), with clocks of 1 clock cycle
            self.dbgif.dwrite.eq(0),
            self.dbgif.pinsin.eq(0xFF10),
            self.bitcount.eq(0),
            self.dbgif.command.eq(CMD_PINS_WRITE)
            ]
        m.next = 'DAP_SWJ_Sequence_PROCESS'

    def RESP_SWJ_Sequence_Process(self, m):
        with m.Switch(self.txb):
            with m.Case(0): # Grab next octet(s) from usb --------------------------------------------------------------
                with m.If(self.streamOut.ready):
                    m.d.usb += [
                        self.tfrData.eq(self.streamOut.payload),
                        self.txb.eq(1),
                        self.busy.eq(1)
                    ]
                with m.Else():
                    m.d.usb += self.busy.eq(0)

            with m.Case(1): # Write the data bit -----------------------------------------------------------------------
                m.d.usb += [
                    self.dbgif.pinsin[0:2].eq(Cat(C(0,1),self.tfrData.bit_select(0,1))),
                    self.tfrData.eq(Cat(C(1,0),self.tfrData[1:8])),
                    self.dbgif.go.eq(1),
                    self.transferCount.eq(self.transferCount-1),
                    self.bitcount.eq(self.bitcount+1),
                    self.txb.eq(2)
                ]

            with m.Case(2): # Wait for bit to be accepted, then we can drop clk ----------------------------------------
                with m.If(self.dbg_done==0):
                    m.d.usb += self.dbgif.go.eq(0)
                with m.If ((self.dbgif.go==0) & (self.dbg_done==1)):
                    m.d.usb += [
                        self.dbgif.pinsin[0].eq(1),
                        self.dbgif.go.eq(1),
                        self.txb.eq(3)
                        ]

            with m.Case(3): # Now wait for clock to be complete, and move to next bit ----------------------------------
                with m.If(self.dbg_done==0):
                    m.d.usb += self.dbgif.go.eq(0)
                with m.If ((self.dbgif.go==0) & (self.dbg_done==1)):
                    with m.If(self.transferCount!=0):
                        m.d.usb += self.txb.eq(Mux(self.bitcount,1,0))
                    with m.Else():
                        m.next = 'DAP_Wait_Done'

    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_SWD_Configure(self, m):
        # <0x13> <ConfigByte>
        # Setup configuration for SWD
        m.d.usb += [
            self.dbgif.dwrite.eq( self.rxBlock.bit_select(8,8) ),
            self.dbgif.command.eq( CMD_SET_CFG ),
            self.dbgif.go.eq(1)
            ]
        m.next = 'DAP_Wait_Done'
    # -------------------------------------------------------------------------------------
    def RESP_SWO_Transport(self, m):
        # <b:0x17> <b:Transport>
        # Set SWO transport
        with m.If(self.rxBlock.word_select(1,8)>2):
            m.d.usb += self.txBlock.word_select(1,8).eq(C(0xff,8))
        m.next = 'RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_SWO_Mode(self, m):
        # <b:0x18> <b:Mode>
        # Set SWO mode (Manchester or UART)
        with m.If(self.rxBlock.word_select(1,8)>2):
            m.d.usb += self.txBlock.word_select(1,8).eq(C(0xff,8))
        m.next = 'RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_SWO_Baudrate(self, m):
        # <b:19> <w:Baudrate>
        # Set SWO baudrate
        m.d.usb += self.txBlock.eq(self.rxBlock)
        m.d.usb += self.txLen.eq(5)
        m.next = 'RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_SWO_Control(self, m):
        # <b:0x1A> <b:Control>
        # Start or Stop SWO transport
        with m.If(self.rxBlock.word_select(1,8)>1):
            m.d.usb += self.txBlock.word_select(1,8).eq(C(0xff,8))
        m.next = 'RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_SWO_Status(self, m):
        # <b:0x1B>
        # SWO Status Request
        m.d.usb += self.txBlock.bit_select(8,40).eq( Cat(C(0,8),C(0x11223344,32) ))
        m.d.usb += self.txLen.eq(6)
        m.next = 'RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_SWO_ExtendedStatus(self, m):
        # <b:0x1E> <b:Control>
        # Return Extended Status information about SWO
        with m.If((self.rxBlock.word_select(1,8)&0xf8)!=0):
            self.RESP_Invalid(m)
        with m.Else():
            m.d.usb += self.txBlock.bit_select(8,104).eq( Cat(C(0,8),C(0x11223344,32),C(0x55667788,32),C(0x99aabbcc,32) ))
            m.d.usb += self.txLen.eq(14)
            m.next = 'RESPOND'
    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------

    def RESP_SWO_Data_Setup(self, m):
        # <b:0x1C> <s:TraceCount>
        # Triggered at the start of a RESP SWO Data Sequence
        # No more data to receive, but a variable amount to send back...
        m.d.comb += self.maxSWObytes.eq(self.rxBlock.bit_select(8,16))

        m.d.usb += [
            self.txLen.eq(Mux(self.maxSWObytes<100,self.maxSWObytes,100)),
            self.txb.eq(0)
            ]
        m.next = 'DAP_SWO_Data_PROCESS'

    def RESP_SWO_Data_Process(self, m):
        with m.If(self.streamIn.ready):
            # Triggered for each octet of a data stream
            m.d.usb += self.streamIn.last.eq(0)

            with m.Switch(self.txb):
                with m.Case(0): # Response header
                    m.d.usb += self.streamIn.payload.eq(DAP_SWO_Data)
                    m.d.usb += self.txb.eq(1)

                with m.Case(1): # Trace status
                    m.d.usb += self.streamIn.payload.eq(0)
                    m.d.usb += self.txb.eq(2)

                with m.Case(2): # Count H
                    m.d.usb += self.streamIn.payload.eq(self.txLen.word_select(0,8))
                    m.d.usb += self.txb.eq(3)

                with m.Case(3): # Count L
                    m.d.usb += self.streamIn.payload.eq(self.txLen.word_select(1,8))
                    m.d.usb += self.txb.eq(Mux(self.txLen,4,5))
                    with m.If(self.txLen==0):
                        m.d.usb += self.streamIn.last.eq(1)

                with m.Case(4): # Data to be sent
                    m.d.usb += self.streamIn.payload.eq(42)
                    m.d.usb += self.txLen.eq(self.txLen-1)
                    with m.If(self.txLen==1):
                        m.d.usb += [
                            self.streamIn.last.eq(1),
                            self.txb.eq(5)
                        ]

                with m.Case(5):
                    m.next = 'IDLE'

            with m.If(self.txb!=5):
                m.d.usb += self.streamIn.valid.eq(1)
    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_JTAG_Configure(self, m):
        # <b:0x15> <b:Count> n x [ <b:IRLength> ]
        # Set IR Length for Chain
        m.d.usb += self.irlength.eq(self.rxBlock.word_select(2,8))
        m.d.usb += self.ndev.eq(self.rxBlock.word_select(1,8))
        m.next = 'RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_JTAG_IDCODE(self, m):
        # <b:0x16> <b:JTAGIndex>
        # Request ID code for specified device
        m.d.usb += self.txBlock.bit_select(16,32).eq(0x44332211)
        m.d.usb += self.txLen.eq(6)
        m.next = 'RESPOND'
    # -------------------------------------------------------------------------------------
    def RESP_TransferConfigure(self, m):
        # <b:0x04> <b:IdleCycles> <s:WaitRetry> <s:MatchRetry>
        # Configure transfer parameters
        m.d.usb += [
            self.waitRetry.eq(self.rxBlock.bit_select(16,16)),
            self.matchRetry.eq(self.rxBlock.bit_select(32,16)),

            # Send idleCycles to layers below
            self.dbgif.dwrite.eq(self.rxBlock.bit_select(8,8)),
            self.dbgif.command.eq(CMD_SET_TFR_CFG),
            self.dbgif.go.eq(1)
        ]
        m.next = 'DAP_Wait_Done'
    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_Transfer_Setup(self, m):
        # <0x05> <b:DapIndex> <b:TfrCount] n x [ <b:TfrReq> <w:TfrData>]
        # Triggered at start of a Transfer data sequence
        # We have the command, index and transfer count, need to set up to get the transfers

        m.d.usb += [
            self.dapIndex.eq(self.rxBlock.bit_select(8,8)),
            self.transferCount.eq(self.rxBlock.bit_select(16,8)),
            self.tfrram.adr.eq(0),
            self.busy.eq(1),
            self.txb.eq(0)
        ]

        # Filter for case someone tries to send us no transfers to perform
        # in which case we send back a good ack!
        with m.If(self.rxBlock.bit_select(16,8)!=0):
            m.next = 'DAP_Transfer_PROCESS'
        with m.Else():
            m.d.usb += [
                self.txBlock.word_select(2,8).eq(C(1,8)),
                self.busy.eq(0),
                self.txLen.eq(3)
                ]
            m.next = 'RESPOND'


    def RESP_Transfer_Process(self, m):
        m.d.comb += self.tfrram.dat_w.eq(self.dbgif.dread)
        #m.d.usb += self.can.eq(~self.can)
        m.d.comb += self.can.eq(self.dbgif.dbgpins.swdwr)

        # By default we don't want to receive any more stream data, we're not writing to the ram, and
        # this isn't the end of the packet
        m.d.usb += [
            self.busy.eq(1),
            self.streamIn.last.eq(0),
        ]

        with m.Switch(self.txb):
            with m.Case(0): # Get transfer request from stream, or the previous one if the post is finishing ----------
                with m.If(~self.streamOut.ready):
                    m.d.usb += self.busy.eq(0)
                with m.Else():
                    m.d.usb += [
                        self.tfrReq.eq(self.streamOut.payload),
                        self.retries.eq(0)
                    ]

                    # This is a good transaction from the stream, so record the fact it's in flow
                    m.d.usb += self.txBlock.word_select(1,8).eq(self.txBlock.word_select(1,8)+1)

                    # So now go do the read or write as appropriate
                    with m.If ((~self.streamOut.payload.bit_select(1,1)) |
                               self.streamOut.payload.bit_select(4,1) |
                               self.streamOut.payload.bit_select(5,1) ):

                        # Need to collect the value
                        m.d.usb += self.txb.eq(1)
                    with m.Else():
                        # It's a read, no value to collect
                        m.d.usb += [
                            self.txb.eq(5),
                            self.busy.eq(1)
                        ]

            with m.Case(1,2,3,4): # Collect the 32 bit transfer Data to go with the command ----------------------------
                with m.If(self.streamOut.ready):
                    m.d.usb+=[
                        self.tfrData.word_select(self.txb-1,8).eq(self.streamOut.payload),
                        self.txb.eq(self.txb+1)
                    ]

                    with m.If(self.tfrReq.bit_select(5,1) & (self.txb==5)):
                        # This is a match register write
                        m.d.usb += [
                            self.mask.eq(Cat(self.streamOut.payload,self.tfrData.bit_select(0,24))),
                            self.txb.eq(0)
                        ]
                with m.Else():
                    m.d.usb +=self.busy.eq(0)

            with m.Case(5): # We have the command and any needed data, action it ---------------------------------------
                m.d.usb += [
                    self.dbgif.command.eq(CMD_TRANSACT),
                    self.dbgif.apndp.eq(self.tfrReq.bit_select(0,1)),
                    self.dbgif.rnw.eq(self.tfrReq.bit_select(1,1)),
                    self.dbgif.addr32.eq(self.tfrReq.bit_select(2,2)),
                    self.dbgif.dwrite.eq(self.tfrData),
                    self.dbgif.go.eq(1),
                    self.txb.eq(self.txb+1),
                ]

            with m.Case(6): # We sent a command, wait for it to start being executed -----------------------------------
                with m.If(self.dbg_done==0):
                    m.d.usb+=[
                        self.dbgif.go.eq(0),
                        self.txb.eq(7)
                    ]

            with m.Case(7): # Wait for command to complete -------------------------------------------------------------
                with m.If(self.dbg_done==1):
                    # Write return value from this command into return frame
                    m.d.usb += self.txBlock.word_select(2,8).eq(Cat(self.dbgif.ack,self.dbgif.perr)),

                    # Now lets figure out how to handle this response....

                    # If we're to retry, then lets do it
                    with m.If(self.dbgif.ack==0b010):
                        m.d.usb += self.retries.eq(self.retries+1)
                        m.d.usb += self.txb.eq(Mux((self.retries<self.waitRetry),5,8))

                    with m.Elif(self.tfrReq.bit_select(4,1)):

                        # This is a transfer match request
                        with m.If(((self.dbgif.dread & self.mask) !=self.tfrData) & (self.matchretries<self.matchRetry)):
                            # Not a match and we've run out of attempts, so set bit 4
                            m.d.usb += self.txBlock.bit_select(21,1).eq(1)
                            m.d.usb += self.txb.eq(8)
                        with m.Else():
                            m.d.usb += self.txb.eq(5)
                    with m.Else():
                        # Check to see if this is a new post (i.e. data to be ignored), or data
                        with m.If(self.dbgif.again | ((~self.dbgif.ignoreData) & self.dbgif.rnw)):
                            m.d.usb += [
                                # We're instructed to write this
                                self.tfrram.adr.eq(self.tfrram.adr+1),
                            ]
                        # It it was a good transfer, then keep going if appropriate
                        with m.If(self.dbgif.again):
                            # Just repeat this send
                            m.d.usb += self.txb.eq(5)
                        with m.Else():
                            m.d.usb += [
                                # This transaction is something we want to record
                                self.transferCount.eq(self.transferCount-1)
                            ]

                            with m.If((self.dbgif.ack==1) & (self.dbgif.perr==0) & (self.transferCount>1)):
                                m.d.usb += self.txb.eq(0)
                            with m.Else():
                                with m.If(self.dbgif.postedMode):
                                    # Debug interface is in posting mode, better do one final read to collect the data
                                    m.d.usb += [
                                        self.tfrReq.eq(0x0E), # Read RDBUFF
                                        self.retries.eq(0),
                                        self.txb.eq(5)
                                    ]
                                with m.Else():
                                    # Otherwise let's wrap up
                                    # All data have been processed, now lets send them back
                                    m.d.usb += [
                                        # This is the number of reads we need to send back
                                        # Include the read from this clock tick if appropriate (i.e. if it's a read)
                                        self.transferCount.eq(self.tfrram.adr+self.dbgif.rnw),
                                        self.txb.eq(8)
                                    ]

            with m.Case(8,9,10): # Transfer completed, start sending data back -----------------------------------------
                m.d.usb += self.tfrram.adr.eq(0),
                with m.If(self.streamIn.ready):
                    m.d.usb += [
                        self.streamIn.payload.eq(self.txBlock.word_select(self.txb-8,8)),
                        self.streamIn.valid.eq(1),
                        self.txb.eq(self.txb+1)
                    ]
                    with m.If((self.txb==10) & (self.transferCount==0)):
                        m.d.usb += [
                            self.streamIn.last.eq(1),
                            self.busy.eq(0)
                        ]
                        m.next = 'IDLE'

            with m.Case(11): # Collect transfer value from RAM store ---------------------------------------------------
                m.d.usb += [
                    self.transferCount.eq(self.transferCount-1),
                    self.txb.eq(self.txb+1)
                ]

            with m.Case(12,13,14,15): # Send 32 bit value to usb -------------------------------------------------------
                with m.If(self.streamIn.ready):
                    m.d.usb += [
                        self.streamIn.payload.eq(self.tfrram.dat_r.word_select(self.txb-12,8)),
                        self.streamIn.valid.eq(1),
                        self.txb.eq(self.txb+1)
                        ]
                    with m.If(self.txb==15):
                        with m.If(self.transferCount==0):
                            m.d.usb += [
                                self.streamIn.last.eq(1),
                                self.busy.eq(0)
                            ]
                            m.next = 'IDLE'

                        with m.Else():
                            m.d.usb += [
                                self.tfrram.adr.eq(self.tfrram.adr+1),
                                self.txb.eq(11)
                            ]

    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_TransferBlock_Setup(self, m):
        # <B:0x06> <B:DapIndex> <S:TransferCount> <B:TransferReq> n x [ <W:TransferData> ])
        # Triggered at start of a TransferBlock data sequence
        # We have the command, index and transfer count, need to set up to get the transfers

        m.d.usb += [
            self.tfrram.adr.eq(0),
            self.dbgif.command.eq(CMD_TRANSACT),
            self.retries.eq(0),

            # DAP Index is 1 byte in
            self.dapIndex.eq(self.rxBlock.bit_select(8,8)),

            # Transfer count is 2 bytes in
            self.transferCount.eq(self.rxBlock.bit_select(16,16)),

            # Transfer Req is 4 bytes in
            self.dbgif.apndp.eq(self.rxBlock.bit_select(32,1)),
            self.dbgif.rnw.eq(self.rxBlock.bit_select(33,1)),
            self.dbgif.addr32.eq(self.rxBlock.bit_select(34,2)),

            # Zero the number of responses sent back
            self.txBlock.bit_select(8,16).eq(C(0,16)),

            # Decide which state to jump to depending on if we have data
            self.txb.eq(Mux(self.tfrReq.bit_select(33,1),4,0)),

            # ...and start the retries counter for this first entry
            self.retries.eq(0)
        ]

        # Filter for case someone tries to send us no transfers to perform
        # in which case we send back a good ack!
        with m.If(self.rxBlock.bit_select(16,16)):
            m.next = 'DAP_TransferBlock_PROCESS'
        with m.Else():
            m.d.usb += [
                self.txBlock.bit_select(8,24).eq(C(1,24)),
                self.txLen.eq(4)
            ]
            m.next = 'RESPOND'



    def RESP_TransferBlock_Process(self, m):
        m.d.comb += self.tfrram.dat_w.eq(self.dbgif.dread)

        # By default we don't want to receive any more stream data, we're not writing to the ram
        # and it's not the end of a USB packet
        m.d.usb += [
            self.busy.eq(1),
            self.streamIn.last.eq(0),
        ]

        with m.Switch(self.txb):
            with m.Case(0,1,2,3): # Collect the 32 bit transfer Data to go with the command ----------------------------
                with m.If(self.streamOut.ready):
                    m.d.usb+=[
                        self.tfrData.word_select(self.txb,8).eq(self.streamOut.payload),
                        self.txb.eq(self.txb+1),
                    ]
                with m.Else():
                    m.d.usb +=self.busy.eq(0)

            with m.Case(4): # We have the command and any needed data, action it ---------------------------------------
                m.d.usb += [
                    self.txBlock.bit_select(8,16).eq(self.txBlock.bit_select(8,16)+1),
                    self.dbgif.dwrite.eq(self.tfrData),
                    self.dbgif.go.eq(1),
                    self.retries.eq(self.retries+1),
                    self.txb.eq(5)
                ]

            with m.Case(5): # Wait for command to be accepted ----------------------------------------------------------
                with m.If(self.dbg_done==0):
                    m.d.usb += self.dbgif.go.eq(0)
                    m.d.usb += self.txb.eq(6)

            with m.Case(6): # We sent a command, wait for it to start being executed -----------------------------------
                with m.If(self.dbg_done==1):
                    # Write return value from this command into return frame
                    m.d.usb += self.txBlock.bit_select(24,8).eq(Cat(self.dbgif.ack, self.dbgif.perr))

                    # Now lets figure out how to handle this response

                    # If we're to retry, then let's do it
                    with m.If(self.dbgif.ack==0b010):
                        m.d.usb += self.txb.eq(Mux((self.retries<self.waitRetry),4,7))

                    with m.Else():
                        with m.If((~self.dbgif.ignoreData) & (self.dbgif.rnw)):
                            # If this is something that resulted in data, then store the data
                            m.d.usb += [
                                self.tfrram.adr.eq(self.tfrram.adr+1),
                            ]

                        with m.If(self.dbgif.again):
                            # We need to repeat this request with the same parameters
                            m.d.usb += [
                                self.retries.eq(0),
                                self.dbgif.go.eq(1),
                                self.txb.eq(5)
                            ]

                        with m.Else():
                            # Keep going if appropriate
                            m.d.usb += self.transferCount.eq(self.transferCount-1)
                            with m.If((self.dbgif.ack==1) & (self.dbgif.perr==0) & (self.transferCount>1)):
                                m.d.usb += self.txb.eq(Mux(self.dbgif.rnw,4,0))

                            with m.Else():
                                with m.If(self.dbgif.postedMode):
                                    # Debug interface is in posting mode, better do one more read to collect the data
                                    m.d.usb += [
                                        self.txBlock.bit_select(8,16).eq(self.txBlock.bit_select(8,16)-1),
                                        self.dbgif.rnw.eq(1),     # Read RDBUFF
                                        self.retries.eq(0),
                                        self.dbgif.apndp.eq(0),
                                        self.dbgif.addr32.eq(3),
                                        self.txb.eq(4)
                                    ]
                                with m.Else():
                                    # Otherwise lets wrap up
                                    m.d.usb += [
                                        # Only need to increment transfer count ram position if this was a read
                                        self.transferCount.eq(self.tfrram.adr+self.dbgif.rnw),
                                        self.tfrram.adr.eq(0),
                                        self.txb.eq(7)
                                    ]

            with m.Case(7,8,9,10): # Transfer completed, start sending data back ---------------------------------------
                with m.If(self.streamIn.ready):
                    m.d.usb += [
                        self.streamIn.payload.eq(self.txBlock.word_select(self.txb-7,8)),
                        self.streamIn.valid.eq(1),
                        self.txb.eq(self.txb+1),
                    ]

                # For the case that this was a write there are no data to send back, so we're done
                with m.If((self.txb==10) & (self.dbgif.rnw==0)):
                    m.d.usb += self.streamIn.last.eq(1)
                    m.next = 'IDLE'

            with m.Case(11): # Collect transfer value from RAM store ---------------------------------------------------
                m.d.usb += [
                    self.transferCount.eq(self.transferCount-1),
                    self.txb.eq(12)
                ]

            with m.Case(12,13,14,15): # Send 32 bit value to usb -------------------------------------------------------
                with m.If(self.streamIn.ready):
                    m.d.usb += [
                        self.streamIn.payload.eq(self.tfrram.dat_r.word_select(self.txb-12,8)),
                        self.streamIn.valid.eq(1),
                        self.txb.eq(self.txb+1)
                    ]
                    with m.If(self.txb==15):
                        with m.If(self.transferCount==0):
                            m.d.usb+=self.streamIn.last.eq(1)
                            m.next = 'IDLE'
                        with m.Else():
                            m.d.usb += [
                                self.txb.eq(11),
                                self.tfrram.adr.eq(self.tfrram.adr+1)
                            ]

    # -------------------------------------------------------------------------------------
    # -------------------------------------------------------------------------------------
    def RESP_JTAG_Sequence_Setup(self,m):
        # Triggered at the start of a RESP JTAG Sequence
        # There are data to receive at this point, and potentially bytes to transmit

        # Collect how many sequences we'll be processing, then move to get the first one
        m.d.usb += [
            self.seqCount.eq(self.rxBlock.word_select(1,8)),
            self.tdoCount.eq(16),  # TDI Data goes after packet ID and status
            self.txBlock[16:].eq(0), # Blank out any TDI storage
            self.txb.eq(0)
            ]
        m.next = 'DAP_JTAG_Sequence_PROCESS'


    def RESP_JTAG_Sequence_PROCESS(self,m):
        m.d.usb += self.busy.eq(1)

        # If we're done then go home
        with m.If(self.seqCount == 0):
            # Packet response has been built in this state machine, so send it
            m.d.usb += [
                self.txLen.eq((self.tdoCount+7)>>3),
                self.busy.eq(0)
                ]
            m.next = 'RESPOND'
        with m.Else():
            # Otherwise...
            with m.Switch(self.txb):
                # --------------
                with m.Case(0): # Get info for this sequence
                    m.d.usb += self.busy.eq(0)
                    with m.If(self.streamOut.ready):
                        m.d.usb += [
                            self.tckCycles.eq(self.streamOut.payload[0:6]),
                            self.tmsValue.eq(self.streamOut.payload[6]),
                            self.tdoCapture.eq(self.streamOut.payload[7]),
                            self.txb.eq(1)
                        ]
                    # Just in case a previous sequence had tdoCapture on, round up the bits
                    m.d.usb += self.tdoCount.eq((self.tdoCount+7)&0x78)

                # --------------
                with m.Case(1): # Waiting for TDI byte to arrive
                    m.d.usb += self.busy.eq(0)
                    with m.If(self.streamOut.ready):
                        m.d.usb += [
                            self.tdiData.eq(self.streamOut.payload),
                            self.txb.eq(2),
                            self.busy.eq(1),
                            self.bytebits.eq(Mux(((self.tckCycles==0) | (self.tckCycles>7)),8,self.tckCycles))
                            ]
                # --------------
                with m.Case(2): # Clocking out TDI and, perhaps, receiving TDO
                    # We still need to deal with this bit...
                    m.d.usb += self.tckCycles.eq(self.tckCycles-1)
                    m.d.usb += self.bytebits.eq(self.bytebits-1)

                    # If this is the last bit of this byte, best go get another one
                    with m.If(self.bytebits==1):
                        m.d.usb += self.txb.eq(1)

                    # ...and if we've got anything to record
                    with m.If(self.tdoCapture):
                        m.d.usb += [
                            self.tdoCount.eq(self.tdoCount+1),
                            self.txBlock.bit_select(self.tdoCount,1).eq(self.bytebits==1)
                        ]
                        # Add code here for I/O
                    m.d.usb += self.tdiData.eq(self.tdiData>>1)

                    with m.If(self.tckCycles==1):
                        # This sequence is done, so move to next transmission
                        m.d.usb += self.seqCount.eq(self.seqCount-1)
                        m.d.usb += self.txb.eq(0)
                # --------------

    # -------------------------------------------------------------------------------------

    def elaborate(self,platform):
        self.can      = platform.request("canary")
        done_cdc      = Signal(2)
        self.dbg_done = Signal()

        m = Module()
        # Reset everything before we start

        m.d.usb += [  self.streamIn.valid.eq(0) ]

        m.d.comb += [
            self.streamOut.ready.eq(~self.busy),
        ]

        m.submodules.tfrram = self.tfrram = DomainRenamer('usb')(WideRam())
        m.submodules.dbgif = self.dbgif = DBGIF(self.dbgpins)

        m.d.usb += done_cdc.eq(Cat(done_cdc[1],self.dbgif.done))
        m.d.comb += self.dbg_done.eq(done_cdc==0b11)

        # Latch the read data at the rising edge of done signal
        m.d.comb += self.tfrram.we.eq(done_cdc==0b10)

        with m.FSM(domain="usb") as decoder:
            with m.State('IDLE'):
                m.d.usb += [ self.txedLen.eq(0), self.busy.eq(0) ]

                # Only process if this is the start of a packet (i.e. it's not overrrun or similar)
                with m.If((self.streamOut.valid) & (self.streamOut.first==1)):
                    m.next = 'ProtocolError'
                    m.d.usb += self.rxedLen.eq(1)
                    m.d.usb += self.rxBlock.word_select(0,8).eq(self.streamOut.payload)

                    # Default return is packet name followed by 0 (no error)
                    m.d.usb += self.txBlock.word_select(0,16).eq(Cat(self.streamOut.payload,C(0,8)))
                    m.d.usb += self.txLen.eq(2)

                    with m.Switch(self.streamOut.payload):
                        with m.Case(DAP_Disconnect, DAP_ResetTarget, DAP_SWO_Status, DAP_TransferAbort):
                            m.d.usb+=self.rxLen.eq(1)
                            m.next='RxParams'

                        with m.Case(DAP_Info, DAP_Connect, DAP_SWD_Configure, DAP_SWO_Transport, DAP_SWJ_Sequence,
                                    DAP_SWO_Mode, DAP_SWO_Control, DAP_SWO_ExtendedStatus, DAP_JTAG_IDCODE, DAP_JTAG_Sequence):
                            m.d.usb+=self.rxLen.eq(2)
                            with m.If(~self.streamOut.last):
                                m.next = 'RxParams'

                        with m.Case(DAP_HostStatus, DAP_SWO_Data, DAP_Delay, DAP_JTAG_Configure, DAP_Transfer):
                            m.d.usb+=self.rxLen.eq(3)
                            with m.If(~self.streamOut.last):
                                m.next = 'RxParams'

                        with m.Case(DAP_SWO_Baudrate, DAP_SWJ_Clock, DAP_TransferBlock):
                            m.d.usb+=self.rxLen.eq(5)
                            with m.If(~self.streamOut.last):
                                m.next = 'RxParams'

                        with m.Case(DAP_WriteABORT, DAP_TransferConfigure):
                            m.d.usb+=self.rxLen.eq(6)
                            with m.If(~self.streamOut.last):
                                m.next = 'RxParams'

                        with m.Case(DAP_SWJ_Pins):
                            m.d.usb+=self.rxLen.eq(7)
                            with m.If(~self.streamOut.last):
                                m.next = 'RxParams'

                        with m.Case(DAP_SWD_Sequence):
                            with m.If(~self.streamOut.last):
                                m.next = 'DAP_SWD_Sequence_GetCount'

                        with m.Case(DAP_ExecuteCommands):
                            with m.If(~self.streamOut.last):
                                m.next = 'DAP_ExecuteCommands_GetNum'

                        with m.Case(DAP_QueueCommands):
                            with m.If(~self.streamOut.last):
                                m.next = 'DAP_QueueCommands_GetNum'

                        with m.Default():
                            self.RESP_Invalid(m)

    #########################################################################################

            with m.State('RESPOND'):
                with m.If(self.txedLen<self.txLen):
                    with m.If(self.streamIn.ready):
                        m.d.usb += [
                            self.streamIn.payload.eq(self.txBlock.word_select(self.txedLen,8)),
                            self.txedLen.eq(self.txedLen+1),
                            self.streamIn.last.eq(self.txedLen+1==self.txLen),
                            self.streamIn.valid.eq(1)
                        ]
                with m.Else():
                    # Everything is transmitted, return to idle condition
                    m.d.usb += [
                        self.streamIn.valid.eq(0),
                        self.streamIn.last.eq(0),
                        self.rxedLen.eq(0),
                        self.busy.eq(0)
                    ]
                    m.next = 'IDLE'

    #########################################################################################

            with m.State('RxParams'):
                # ---- Action dispatcher --------------------------------------
                # If we've got everything for this packet then let's process it
                with m.If(self.rxedLen==self.rxLen):
                    with m.Switch(self.rxBlock.word_select(0,8)):

                        # General Commands
                        # ================
                        with m.Case(DAP_Info):
                            self.RESP_Info(m)

                        with m.Case(DAP_HostStatus):
                            self.RESP_HostStatus(m)

                        with m.Case(DAP_Connect):
                            self.RESP_Connect_Setup(m)

                        with m.Case(DAP_Disconnect):
                            self.RESP_Disconnect(m)

                        with m.Case(DAP_WriteABORT):
                            self.RESP_WriteABORT(m)

                        with m.Case(DAP_Delay):
                            self.RESP_Delay(m)

                        with m.Case(DAP_ResetTarget):
                            self.RESP_ResetTarget(m)

                        # Common SWD/JTAG Commands
                        # ========================
                        with m.Case(DAP_SWJ_Pins):
                            self.RESP_SWJ_Pins_Setup(m)

                        with m.Case(DAP_SWJ_Clock):
                            self.RESP_SWJ_Clock(m)

                        with m.Case(DAP_SWJ_Sequence):
                            self.RESP_SWJ_Sequence_Setup(m)

                        # SWD Commands
                        # ============
                        with m.Case(DAP_SWD_Configure):
                            self.RESP_SWD_Configure(m)

                        # SWO Commands
                        # ============
                        with m.Case(DAP_SWO_Transport):
                            self.RESP_SWO_Transport(m)

                        with m.Case(DAP_SWO_Mode):
                            self.RESP_SWO_Mode(m)

                        with m.Case(DAP_SWO_Baudrate):
                            self.RESP_SWO_Baudrate(m)

                        with m.Case(DAP_SWO_Control):
                            self.RESP_SWO_Control(m)

                        with m.Case(DAP_SWO_Status):
                            self.RESP_SWO_Status(m)

                        with m.Case(DAP_SWO_ExtendedStatus):
                            self.RESP_SWO_ExtendedStatus(m)

                        with m.Case(DAP_SWO_Data):
                            self.RESP_SWO_Data_Setup(m)

                        # JTAG Commands
                        # =============
                        with m.Case(DAP_JTAG_Sequence):
                            self.RESP_JTAG_Sequence_Setup(m)

                        with m.Case(DAP_JTAG_Configure):
                            self.RESP_JTAG_Configure(m)

                        with m.Case(DAP_JTAG_IDCODE):
                            self.RESP_JTAG_IDCODE(m)

                        # Transfer Commands
                        # =================
                        with m.Case(DAP_TransferConfigure):
                            self.RESP_TransferConfigure(m)

                        with m.Case(DAP_Transfer):
                            self.RESP_Transfer_Setup(m)

                        with m.Case(DAP_TransferBlock):
                            self.RESP_TransferBlock_Setup(m)

                        with m.Default():
                            self.RESP_Invalid(m)

                # Grab next byte in this packet
                with m.Elif(self.streamOut.valid):
                    m.d.usb += [
                        self.rxBlock.word_select(self.rxedLen,8).eq(self.streamOut.payload),
                        self.rxedLen.eq(self.rxedLen+1)
                    ]
                    # Don't grab more data if we've got what we were commanded for
                    with m.If(self.rxedLen+1==self.rxLen):
                        m.d.usb += self.busy.eq(1)

                    # Check to make sure this packet isn't foreshortened
                    with m.If(self.streamOut.last):
                        with m.If(self.rxedLen+1!=self.rxLen):
                            self.RESP_Invalid(m)


    #########################################################################################

            with m.State('DAP_SWJ_Pins_PROCESS'):
              self.RESP_SWJ_Pins_Process(m)

            with m.State('DAP_SWO_Data_PROCESS'):
              self.RESP_SWO_Data_Process(m)

            with m.State('DAP_SWJ_Sequence_PROCESS'):
                self.RESP_SWJ_Sequence_Process(m)

            with m.State('DAP_JTAG_Sequence_PROCESS'):
              self.RESP_JTAG_Sequence_PROCESS(m)

            with m.State('DAP_Transfer_PROCESS'):
              self.RESP_Transfer_Process(m)

            with m.State('DAP_TransferBlock_PROCESS'):
              self.RESP_TransferBlock_Process(m)

            with m.State('DAP_SWD_Sequence_GetCount'):
                self.RESP_Invalid(m)

            with m.State('DAP_TransferBlock'):
                self.RESP_Invalid(m)

            with m.State('DAP_ExecuteCommands_GetNum'):
                self.RESP_Invalid(m)

            with m.State('DAP_QueueCommands_GetNum'):
                self.RESP_Invalid(m)

            with m.State('DAP_Wait_Done'):
                self.RESP_Wait_Done(m)

            with m.State('DAP_Wait_Connect_Done'):
                self.RESP_Wait_Connect_Done(m)

            with m.State('Error'):
                self.RESP_Invalid(m)

            with m.State('ProtocolError'):
                self.RESP_Invalid(m)

        return m