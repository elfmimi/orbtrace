`default_nettype none
/* verilator lint_off WIDTH */

// SWO (Manchester formatted)
// ==========================
// Collects sequences of 8 bits from the SWO pin and delivers them to the layer above.
// Format of a sequence of a packet is defined in ARM DDI 0314H Section 1.11.2.
//
// Deals with sync and clocking and delivers clean 8 bit bytes of data to layer above.

module swoManchIF (
		   input 	    rst,          // Reset synchronised to clock
		   input            clk,          // Module clock used for edge sampling
		   
		   // Downwards interface to the trace pins (1-n bits max, can be less)
		   input 	    SWOina,       // SWO data rising edge (LSB)
		   input 	    SWOinb,       // SWO data falling edge (MSB)

		   // DIAGNOSTIC
                   output 	    edgeOutput,

		   // Upwards interface to packet processor
		   output reg 	    byteAvail,    // Toggling indicator byte ready
		   output reg [7:0] completeByte  // The last constructed byte
		   );
   
   // Internals =======================================================================

   // Frame maintenance
   reg [7:0]  construct;                // Track of data being constructed
   reg [15:0] halfbitlen;               // Clock ticks for a half bit length
   reg [15:0] activeCount;              // Clock ticks through this bit
   reg [2:0]  bitcount;                 // Index through this byte
   reg bithistory;                      // Previous state of the SWO bit
   
   reg [1:0]  decodeState;              // Current state of decoder

   parameter DECODE_STATE_IDLE              = 0;
   parameter DECODE_STATE_GET_HBLEN         = 1;
   parameter DECODE_STATE_RXS_GETTING_BITS0 = 2;
   parameter DECODE_STATE_RXS_GETTING_BITS1 = 3;   

   // Calculations for bitlengths
   wire [15:0] quarterbitlen    = {1'b0, halfbitlen[15:1]};
   wire [15:0] threeightbitlen  = halfbitlen+quarterbitlen;
   wire [15:0] bitlen           = {halfbitlen[14:0],1'b0};

   // Bit construction slider
   wire [2:0] bitsnow = { bithistory, SWOinb, SWOina };

   // ...and edge detection
   wire       isEdge  = bitsnow[2]!=(bitsnow[1]) || (bitsnow[2]!=bitsnow[0]);
   
   always @(posedge clk, posedge rst)
     begin
        // Default status bits
	if (rst)
	  begin
	     decodeState      <= DECODE_STATE_IDLE;
	  end
	else
	  begin
	     bithistory <= SWOina;

	     // Guard to reset if we spend too long waiting for an edge
	     if (activeCount > bitlen)
	       begin
		  // No transition for the duration of a bit....must be at the end
		  decodeState <= DECODE_STATE_IDLE;
	       end
	     else
	       case (decodeState)
		 DECODE_STATE_IDLE: // --------------------------------------------------
		   begin
		      halfbitlen <= 0;
		      if (bitsnow[1:0]!={2'b00})
			begin
			   halfbitlen  <= bitsnow[1]+bitsnow[0];
			   decodeState <= DECODE_STATE_GET_HBLEN;
			end
		   end
		 
		 DECODE_STATE_GET_HBLEN: // --------------------------------------------
		   // If both halves are still high then extend count
		   if (bitsnow[1:0]=={2'b11})
		     halfbitlen          <= halfbitlen+2;
		   else
		     begin
			// Otherwise finesse the count and wait for the first falling edge
			halfbitlen        <= halfbitlen + bitsnow[1];
			
			// Now start accumulating time
			activeCount <= 0;
			bitcount <= 0;
			
			// Get the first half of the bit
			decodeState <= DECODE_STATE_RXS_GETTING_BITS0;
		     end
		 
		 // So there are four cases to consider;
		 // 0 followed by 0;  01 01
		 // 0 followed by 1;  01 10
		 // 1 followed by 0;  10 01
		 // 1 followed by 1;  10 10
		 
		 DECODE_STATE_RXS_GETTING_BITS0: // --------------------------------------------
		   // First part of a bit
		   if (!isEdge)
		     // No edge yet...keep accumulating
		     activeCount <= activeCount + 2;
		   else
		     begin
			// This is an edge change here
			if (activeCount+SWOinb < threeightbitlen)
			  begin
			     // This is a change at the start of a bit..so now wait for the mid-change
			     decodeState <= DECODE_STATE_RXS_GETTING_BITS1;
			     activeCount <= bitsnow[1]!=bitsnow[0]?1:0;
			  end
			else
			  begin
			     // This is a change in the middle of a bit..so here we need to record the value
			     // but we stay in the GETTING_BITS0 state because we're looking for another bitstart
			     construct[bitcount] <= bitsnow[2];
			     bitcount <= bitcount + 1;
			     
			     if (bitcount==7)
			       begin
				  completeByte <= {construct[6:0],bitsnow[2]};
				  byteAvail <= ~byteAvail;
			       end
			  end
		     end // else: !if(!isEdge)
		 
		 DECODE_STATE_RXS_GETTING_BITS1: // --------------------------------------------
		   if (!isEdge)
		     begin
			activeCount <= activeCount + 2;
		     end
		   else
		     begin
			// This is definately a change in the middle of a bit..so here we need to record the value
			construct[bitcount] <= bitsnow[2];
			bitcount <= bitcount + 1;
			activeCount <= bitsnow[1]!=bitsnow[0]?1:0;
			
			// The next bit we're looking for is the first half
			decodeState <= DECODE_STATE_RXS_GETTING_BITS0;
			
			if (bitcount==7)
			  begin
			     completeByte <= {construct[6:0],bitsnow[2]};
			     byteAvail <= ~byteAvail;
			  end
		     end // else: !if(!isEdge)
	       endcase // case (decodeState)
	  end // else: !if(rst)
     end // always @ (posedge traceClkin, posedge rst)
endmodule // swoManchIF
