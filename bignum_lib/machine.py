# Copyright lowRISC contributors.
# Licensed under the Apache License, Version 2.0, see LICENSE for details.
# SPDX-License-Identifier: Apache-2.0

import math
from collections import Counter


class CallStackUnderrun(OverflowError):
    pass


class Machine(object):
    NUM_REGS = 32
    XLEN = 256
    LIMBS = 8
    DMEM_DEPTH = 128
    IMEM_DEPTH = 1024
    DEFAULT_DUMP_FILENAME = 'dmem_dump.hex'
    LOOP_STACK_SIZE = 16
    CALL_STACK_SIZE = 16

    # breakpoints is dictionary with break addresses being keys and
    # values are tuples of number of passes required and the pass counter
    breakpoints = {}

    # force break in later instruction, e.g. when single stepping
    # Can consider the loop or callstack to allow finishing calls, loops, or step over
    # Format (Forcebreak active, consider call stack, call stack, consider loop stack, loop stack)
    force_break = (False, False, 0, False, 0)

    def get_func_addr_for_pc(self, pc):
        """ Get the function base address for an arbitrary program counter address """
        func_addr_found = False
        for pc in range(pc, 0, -1):
            if pc in self.ctx.functions:
                break
        return pc

    def stat_record_instr(self, instr):
        ins_str = (instr.get_asm_str()[1]).split(' ', 1)[0].strip() # There seems to be no nicer way?
        if 'instruction_histo' not in self.stats:
            self.stats['instruction_histo'] = Counter()
        self.stats['instruction_histo'][ins_str] += 1

    def stat_record_func_call(self, call_site, callee_func):
        caller_func = self.get_func_addr_for_pc(call_site)

        if 'func_calls' not in self.stats:
            self.stats['func_calls'] = []

        self.stats['func_calls'].append({
            'call_site': call_site,
            'caller_func': caller_func,
            'callee_func': callee_func,
        })

    def stat_record_loop(self, loop_addr, loop_len, new_loop_stack_depth, iterations):
        if 'loops' not in self.stats:
            self.stats['loops'] = []
        self.stats['loops'].append({
            'loop_addr': loop_addr,
            'loop_len': loop_len,
            'new_loop_stack_depth': new_loop_stack_depth,
            'iterations': iterations,
        })

    def stat_record_movi(self, imm_size):
        if 'movi' not in self.stats:
            self.stats['movi'] = Counter()
        self.stats['movi'][imm_size] += 1

    def stat_record_wide_mem_op(self, op, inc_src, inc_dst):
        if 'wide_mem_ops' not in self.stats:
            self.stats['wide_mem_ops'] = []
        self.stats['wide_mem_ops'].append({
            'op': op,
            'inc_src': inc_src,
            'inc_dst': inc_dst,
        })

    def stat_record_flag_access(self, flag_group, op):
        if 'flag_access' not in self.stats:
            self.stats['flag_access'] = []
        self.stats['flag_access'].append({
            'flag_group': flag_group,
            'op': op,
        })

    def __init__(self, dmem, imem, s_addr=0, stop_addr=None, ctx=None):
        self.finishFlag = False
        if self.XLEN % (self.LIMBS * 2):
            raise Exception('XLEN must be divisible by LIMBS*2')
        self.limb_width = int(self.XLEN / self.LIMBS)
        self.limb_mask = 2 ** self.limb_width - 1
        self.half_limb_width = int(self.XLEN / self.LIMBS / 2)
        self.half_limb_mask = 2 ** self.half_limb_width - 1
        self.xlen_mask = 2 ** self.XLEN - 1
        self.half_xlen_mask = 2 ** int(self.XLEN / 2) - 1
        self.reg_idx_width = int(math.ceil(math.log2(self.NUM_REGS)))
        self.reg_idx_mask = 2 ** self.reg_idx_width - 1
        self.dmem_idx_width = int(math.ceil(math.log2(self.DMEM_DEPTH)))
        self.dmem_idx_mask = 2 ** self.dmem_idx_width - 1
        self.ctx = ctx
        self.reset(dmem, imem, s_addr, stop_addr, clear_regs=True)

        self.stats = {}

    def reset(self, dmem, imem, s_addr=0, stop_addr=None, clear_regs=False):
        self.M = False
        self.L = False
        self.Z = False
        self.C = False
        self.XM = False
        self.XL = False
        self.XC = False
        self.XZ = False
        if (clear_regs):
            self.clear_regs()
        self.r_valid_half_limbs = [[False]*self.LIMBS*2 for i in range(self.NUM_REGS)]
        self.dmem = []
        self.imem = []
        self.init_dmem = []
        self.loop_stack = []
        self.call_stack = []
        self.dmem.clear()
        self.init_dmem.clear()
        for item in dmem:
            self.dmem.append(item)
            self.init_dmem.append(True)
        for i in range(len(dmem), self.DMEM_DEPTH):
            self.dmem.append(0)
            self.init_dmem.append(False)
        self.imem = imem
        self.pc = s_addr
        if not stop_addr:
            self.stop_addr=(len(self.imem) - 1)
        else:
            self.stop_addr = stop_addr

    def clear_regs(self):
        self.dmp = 0
        self.rfp = 0
        self.lc = 0
        self.rnd = 1
        self.pc = 0
        self.mod = 0
        self.r = []
        for i in range(self.NUM_REGS):
            self.r.append(0)

    def __check_reg_idx(self, idx):
        """Check if register index is within bound"""
        if idx < 0 or idx > self.NUM_REGS - 1:
            raise IndexError

    def __check_reg_val(self, value):
        """Check if register sized value is within bounds"""
        if value < 0 or value > self.xlen_mask:
            raise OverflowError

    def __check_limb_val(self, value):
        """Check if limb value is within bounds"""
        if value < 0 or value > self.limb_mask:
            raise OverflowError

    def __check_half_limb_val(self, value):
        """Check if half-limb value is within bounds"""
        if value < 0 or value > self.half_limb_mask:
            raise OverflowError

    def __check_limb_idx(self, idx):
        """Check if limb index is within bounds"""
        if idx < 0 or idx >= self.LIMBS:
            raise IndexError

    def __check_dmem_addr(self, addr):
        """Check if Dmem address is within bounds"""
        if addr < 0 or addr >= self.DMEM_DEPTH:
            raise IndexError

    def __check_imem_addr(self, addr):
        """Check if Imem address is within bounds"""
        if addr < 0 or addr >= len(self.imem):
            raise IndexError

    def __get_limb_from_reg_val(self, lidx, regval):
        """Extract a specific limb from a register"""
        self.__check_limb_idx(lidx)
        self.__check_reg_val(regval)
        return (regval >> lidx * self.limb_width) & self.limb_mask

    def __mod_limb_in_reg_val(self, lidx, regval, limbval):
        """Modify a specific limb in an register"""
        self.__check_limb_idx(lidx)
        self.__check_reg_val(regval)
        self.__check_limb_val(limbval)
        mask = self.half_limb_mask << (lidx * self.limb_width)
        masked_reg = regval | mask
        masked_reg2 = masked_reg ^ mask
        reg = masked_reg2 | (limbval << (lidx * self.limb_width))
        return reg

    def __mod_half_limb_in_reg_val(self, lidx, regval, halflimbval, upper):
        """Modify a specific half-limb in a register"""
        self.__check_limb_idx(lidx)
        self.__check_reg_val(regval)
        self.__check_half_limb_val(halflimbval)
        mask = self.half_limb_mask << ((lidx * 2 + bool(upper)) * self.half_limb_width)
        masked_reg = regval | mask
        masked_reg2 = masked_reg ^ mask
        reg = masked_reg2 | (halflimbval << ((lidx * 2 + bool(upper)) * self.half_limb_width))
        return reg

    @staticmethod
    def __test_bit(testval, pos):
        """Test for the value of a bit at a given position"""
        mask = 1 << pos
        return bool(testval & mask)

    @staticmethod
    def __set_bit(testval, pos):
        """Set a bit at a specific position and return the new value"""
        mask = 1 << pos
        return testval | mask

    def get_reg(self, ridx):
        """Get register value for register index"""
        if isinstance(ridx, int):
            self.__check_reg_idx(ridx)
            return self.r[ridx]
        if isinstance(ridx, str):
            if ridx == 'mod':
                return self.mod
            elif ridx == 'dmp':
                return self.dmp
            elif ridx == 'rfp':
                return self.rfp
            elif ridx == 'lc':
                return self.lc
            elif ridx == 'rnd':
                return self.rnd
            else:
                raise Exception('Invalid special register')

    def get_reg_valid_half_limbs(self, ridx):
        return self.r_valid_half_limbs[ridx]

    def set_reg(self, ridx, value, valid_limb=None, valid_half_limb=None):
        """Set register value at register index"""
        self.__check_reg_val(value)
        if isinstance(ridx, int):
            if valid_limb:
                self.r_valid_half_limbs[ridx][valid_limb*2] = True
                self.r_valid_half_limbs[ridx][valid_limb*2+1] = True
            elif valid_half_limb:
                self.r_valid_half_limbs[ridx][valid_half_limb] = True
            else:
                self.r_valid_half_limbs[ridx] = [True]*self.LIMBS*2
            self.__check_reg_idx(ridx)
            self.r[ridx] = value
        if isinstance(ridx, str):
            if ridx == 'mod':
                self.mod = value
            elif ridx == 'dmp':
                self.dmp = value
            elif ridx == 'rfp':
                self.rfp = value
            elif ridx == 'lc':
                self.lc = value
            elif ridx == 'rnd':
                self.rnd = value
            else:
                raise Exception('Invalid special register')

    def get_reg_limb(self, ridx, lidx):
        """Get a single limb from a register"""
        return self.__get_limb_from_reg_val(lidx, self.get_reg(ridx))

    def set_reg_limb(self, ridx, lidx, value):
        """Set a single limb in a register"""
        self.set_reg(ridx, self.__mod_limb_in_reg_val(lidx, self.get_reg(ridx), value), valid_limb=lidx)

    def set_reg_half_limb(self, ridx, lidx, value, upper):
        """Set a single half limb of a register"""
        self.set_reg(ridx, self.__mod_half_limb_in_reg_val(lidx, self.get_reg(ridx), value, upper))

    def set_pc(self, pc):
        """Set the program counter"""
        self.__check_imem_addr(pc)
        self.pc = pc

    def get_pc(self):
        """Get the program counter"""
        return self.pc

    def inc_pc(self):
        """Increment the program counter"""
        self.set_pc(self.get_pc() + 1)

    def get_dmem(self, address):
        """Get value for a dmem address"""
        self.__check_dmem_addr(address)
        if not self.init_dmem[address]:
            print('Warning: reading from uninitialized dmem memory address: ' + hex(address))
        return self.dmem[address]

    def set_dmem(self, address, value):
        """Set value at a dmem address"""
        self.__check_dmem_addr(address)
        self.__check_reg_val(value)
        self.dmem[address] = value
        self.init_dmem[address] = True

    def push_loop_stack(self, cnt, end_addr, start_addr):
        """Push tuple of loop count, loop end address and loop start address to loop stack"""
        self.__check_imem_addr(start_addr)
        self.__check_imem_addr(end_addr)
        if len(self.loop_stack) == self.LOOP_STACK_SIZE:
            raise OverflowError('Loop stack overflow')
        self.loop_stack.append((cnt, end_addr, start_addr))

    def dec_top_loop_cnt(self):
        """Decrement loop counter on top of stack"""
        if not len(self.loop_stack):
            raise Exception('Nothing on loop stack to decrement')
        if self.loop_stack[-1][0]:
            self.loop_stack[-1] = (self.loop_stack[-1][0] - 1, self.loop_stack[-1][1], self.loop_stack[-1][2])
            return True
        else:
            return False

    def get_top_loop_end_addr(self):
        """return the end address of the top loop on the stack"""
        if not len(self.loop_stack):
            raise Exception('Nothing on loop stack')
        return self.loop_stack[-1][1]

    def get_top_loop_start_addr(self):
        """return the end address of the top loop on the stack"""
        if not len(self.loop_stack):
            raise Exception('Nothing on loop stack')
        return self.loop_stack[-1][2]

    def pop_loop_stack(self):
        """Remove the top element of the loop stack and return its start address"""
        if len(self.loop_stack):
            return self.loop_stack.pop()[2]
        else:
            raise OverflowError('Loop stack underrun')

    def push_call_stack(self, address):
        """Push a return address to the call stack"""
        self.__check_imem_addr(address)
        if len(self.call_stack) == self.CALL_STACK_SIZE:
            raise OverflowError('Call stack overflow')
        self.call_stack.append(address)

    def pop_call_stack(self):
        """Remove the top return address from the call stack"""
        if len(self.call_stack):
            return self.call_stack.pop()
        else:
            raise CallStackUnderrun('Call stack underrun')

    def get_flag(self, flag):
        """Get a flag"""
        if flag == 'M':
            return self.M
        elif flag == 'L':
            return self.L
        elif flag == 'Z':
            return self.Z
        elif flag == 'C':
            return self.C
        elif flag == 'XM':
            return self.XM
        elif flag == 'XL':
            return self.XL
        elif flag == 'XZ':
            return self.XZ
        elif flag == 'XC':
            return self.XC
        else:
            raise Exception('Invalid flag identifier')

    def set_flag(self, flag, val):
        """Set/unset a flag"""
        if flag == 'M':
            self.M = val
        elif flag == 'L':
            self.L = val
        elif flag == 'Z':
            self.Z = val
        elif flag == 'C':
            self.C = val
        elif flag == 'XM':
            self.XM = val
        elif flag == 'XL':
            self.XL = val
        elif flag == 'XZ':
            self.XZ = val
        elif flag == 'XC':
            self.XC = val
        else:
            raise Exception('Invalid flag identifier')

    def set_c_z_m_l(self, val):
        """Set/Unset C, Z, M and L flags by examining the given value"""
        self.set_z_m_l(val)
        self.set_flag('C', self.__test_bit(val, self.XLEN))

    def setx_c_z_m_l(self, val):
        """Set/Unset XC, XZ, XM and XL flags by examining the given value"""
        self.setx_z_m_l(val)
        self.set_flag('XC', self.__test_bit(val, self.XLEN))

    def set_z_m_l(self, val):
        """Set/Unset Z, M and L flags by examining the given value"""
        self.set_flag('Z', not bool(val & self.xlen_mask))
        self.set_flag('M', self.__test_bit(val, self.XLEN - 1))
        self.set_flag('L', self.__test_bit(val, 0))

    def setx_z_m_l(self, val):
        """Set/Unset XZ, XM and XL flags by examining the given value"""
        self.set_flag('XZ', not bool(val & self.xlen_mask))
        self.set_flag('XM', self.__test_bit(val, self.XLEN - 1))
        self.set_flag('XL', self.__test_bit(val, 0))

    def get_instruction(self, address):
        """Get instruction binary at an imem address"""
        self.__check_imem_addr(address)
        return self.imem[address]

    def get_limb_hex_str(self, val, idx):
        """Extract a limb from a value and return a hex string"""
        limb = self.__get_limb_from_reg_val(idx, val)
        return '0x' + hex(limb)[2:].zfill(8)

    def get_xlen_hex_str(self, val):
        """Get a hex string for an XLEN sized value """
        res_str = ''
        for i in range(self.LIMBS - 1, -1, -1):
            res_str += self.get_limb_hex_str(val, i)[2:]
            if i > 0:
                res_str += ' '
        return res_str

    @staticmethod
    def get_limb_header():
        """Get a header for pretty printing XLEN sized values"""
        res_str  = '    |       7|       6|       5|       4|       3|       2|       1|       0|\n'
        res_str += '----|--------|--------|--------|--------|--------|--------|--------|--------|\n'
        return res_str

    def get_reg_table(self, header):
        """Get a table with hex strings of all regs"""
        res_str = ''
        if header:
            res_str += self.get_limb_header()
        for i in range(0, self.NUM_REGS):
            if (i % 4) == 0:
                res_str += '\n'
            res_str += ('r' + str(i)).rjust(3) + ': ' + self.get_xlen_hex_str(self.get_reg(i))
            if i != self.NUM_REGS:
                res_str += '\n'
        return res_str

    def get_s_reg_table(self, header):
        """Get a table with hexstrings of all special registers"""
        res_str = ''
        if header:
            res_str += self.get_limb_header()
        res_str += 'mod: ' + self.get_xlen_hex_str(self.get_reg('mod')) + '\n'
        res_str += 'rfp: ' + self.get_xlen_hex_str(self.get_reg('rfp')) + '\n'
        res_str += 'dmp: ' + self.get_xlen_hex_str(self.get_reg('dmp')) + '\n'
        res_str += ' lc: ' + self.get_xlen_hex_str(self.get_reg('lc')) + '\n'
        res_str += 'rnd: ' + self.get_xlen_hex_str(self.get_reg('rnd'))
        return res_str

    def get_all_reg_table(self, header):
        """Get a table with hex strings of all registers (general purpose and special)"""
        res_str = ''
        if header:
            res_str += self.get_limb_header()
        res_str += self.get_s_reg_table(False) + '\n' + self.get_reg_table(False)
        return res_str

    def get_all_flags_table(self):
        """Ger a table with the state of all flags (extended and standard)"""
        res_str = ''
        res_str += '|C|Z|M|L|  X|C|Z|M|L|\n'
        res_str += '|' + str(int(self.get_flag('C'))) + '|' + str(int(self.get_flag('Z'))) + '|' \
                   + str(int(self.get_flag('M'))) + '|' + str(int(self.get_flag('L'))) + '|'
        res_str += '   |' + str(int(self.get_flag('XC'))) + '|' + str(int(self.get_flag('XZ'))) + '|' \
                   + str(int(self.get_flag('XM'))) + '|' + str(int(self.get_flag('XL'))) + '|'
        return res_str

    def get_dmem_table(self, low, high):
        """Get a table of hex strings for a given dmem range"""
        s = ''
        for i in range(low, min(high + 1, self.DMEM_DEPTH)):
            if (i % 4) == 0 and i > 0:
                s += '\n'
            s += ('' + str(i)).rjust(4) + ': ' + self.get_xlen_hex_str(self.dmem[i])
            s += '\n'
        return s

    def get_breakpoints(self):
        """Get list of all breakpoints"""
        ret_str = ''
        for key in self.breakpoints:
            ret_str += 'Address: ' + str(key) + ', stop at pass: ' + str(self.breakpoints[key][0]) \
                       + ', passed: ' + str(self.breakpoints[key][1] - 1) + '\n'
        return ret_str

    def toggle_breakpoint(self, bp, passes=1, msg=False):
        """Toggle a breakpoint"""
        # breakpoints is a dictionary with the address as key and the values
        # are tuples of number of passes required to break and the pass counter
        if isinstance(bp, int):
            addr = int(bp)
        else:
            if bp.isdigit():
                addr = int(bp)
            elif bp.lower().startswith('0x'):
                addr = int(bp[2:], 16)
            else:
                if not self.ctx:
                    print('\nError: Label/function breakpoints only possible when assembly context is available\n')
                    return
                else:
                    rev_functions = {v: k for k, v in self.ctx.functions.items()}
                    rev_labels = {v: k for k, v in self.ctx.labels.items()}
                    if bp in rev_functions:
                        addr = rev_functions[bp]
                    elif bp in rev_labels:
                        addr = rev_labels[bp]
                    else:
                        print('\nError: function or label \'' + bp + '\' not found.\n')
                        return

        if addr in self.breakpoints:
            del self.breakpoints[addr]
            if msg:
                print('\nBreakpoint deleted at address ' + str(addr) + '\n')
        else:
            if addr in range(0, self.IMEM_DEPTH):
                self.breakpoints.update({addr: (passes, 1)})
                if msg:
                    print('\nBreakpoint set at address ' + str(addr) + '\n')
            else:
                print('\nError: breakpoint address out of range\n')

    def __check_break(self):
        """check if current PC is in list of Breakpoints, if so and the number of required passes are reached, break,
         otherwise increment the pass counter for the address."""
        if self.force_break[0]:
            force_break, consider_callstack, callstack, consider_loopstack, loopstack = self.force_break
            if consider_loopstack and len(self.loop_stack) == loopstack:
                self.__clear_force_break()
                return True, 0
            if consider_callstack and len(self.call_stack) == callstack:
                self.__clear_force_break()
                return True, 0
            if not consider_callstack and not consider_loopstack:
                self.__clear_force_break()
                return True, 0
        if self.breakpoints:
            # check if address is breakpoint
            if self.get_pc() in self.breakpoints:
                # break address found, check for number passes
                passes, cnt = self.breakpoints[self.get_pc()]
                if cnt == passes:
                    self.breakpoints[self.get_pc()] = (passes, 1)
                    return True, passes
                else:
                    self.breakpoints[self.get_pc()] = (passes, cnt + 1)
                    return False, 0
        return False, 0

    def __loop_depth(self, address):
        """Get loop depth for an address"""
        if not self.ctx:
            return 0
        i = 0
        for r in self.ctx.loopranges:
            if address in r:
                i += 1
        return i

    def print_asm(self, address, before=5, after=5):
        """Print range of assembly instructions before and after current program counter"""
        if address - before - 1 < 0:
            s_addr = 0
        else:
            s_addr = address - before - 1
        if address + after + 1 > len(self.imem) - 1:
            e_addr = len(self.imem) - 1 + 1
        else:
            e_addr = address + after + 1
        for i in range(s_addr, e_addr):
            asm_str = ''
            if address == i:
                asm_str += ' ->'
            else:
                asm_str += '   '
            if i in self.breakpoints:
                if self.breakpoints[i][0] != self.breakpoints[i][1]:
                    asm_str += ' ? '
                else:
                    asm_str += ' x '
            else:
                asm_str += '   '
            asm_str += str(i).zfill(4) + ': '
            for k in range(0, self.__loop_depth(i)):
                asm_str += '    '
            asm_str += self.get_instruction(i).get_asm_str()[1]
            if self.ctx:
                if i in self.ctx.functions:
                    print('\nfunction ' + self.ctx.functions[i] + ':')
            if self.ctx:
                if i in self.ctx.labels:
                    print(self.ctx.labels[i] + ':')
            print(asm_str)

    def get_full_dmem(self):
        """Get full dmem content"""
        return self.dmem

    def dump_dmem(self, length, filename):
        """Dump dmem contents to file"""
        f = open(filename, 'w')
        for i in range(0, min(length, self.DMEM_DEPTH)):
            f.write(str(i).zfill(4) + ': ' + self.get_xlen_hex_str(self.dmem[i]) + '\n')
        f.close()

    @staticmethod
    def __print_break_help():
        print('h  - show this help message')
        print('c  - continue')
        print('s  - step into')
        print('n  - step over')
        print('o  - step out')
        print('ol - step out of loop')
        print('r  - print register file')
        print('rs - print special registers')
        print('ra - print all registers')
        print('d [len] [start] - print dmem words')
        print('f  - print flags')
        print('ls - print loop stack')
        print('cs - print call stack')
        print('a  - print assembly around current instruction')
        print('b <addr> [pass] - toggle breakpoint')
        print('lp - list breakpoints')
        print('dump <length> [filename] - dump dmem content to hex file')
        print('q  - quit')

    def __set_force_break(self, consider_callstack=False, callstack=0, consider_loopstack=False, loopstack=0):
        self.force_break = (True, consider_callstack, callstack, consider_loopstack, loopstack)

    def __clear_force_break(self):
        self.force_break = (False, False, 0, False, 0)

    def __handle_break_command(self, passes):
        if passes:
            print('Breakpoint hit at address ' + str(self.get_pc()) + ' at pass ' + str(passes) + '.')
        else:
            print('Breakpoint hit at address ' + str(self.get_pc()) + '.')
        self.print_asm(self.get_pc(), 5, 5)
        while 1:
            inp = input('Press \'c\' to continue, \'h\' for help: ')
            if inp == 'h':
                self.__print_break_help()
            elif inp == 'q':
                exit()
            elif inp == 'c':
                break
            elif inp == 's':
                self.__set_force_break()
                break
            elif inp == 'n':
                self.__set_force_break(consider_callstack=True, callstack=len(self.call_stack))
                break
            elif inp == 'ol':
                if len(self.loop_stack) <= 0:
                    print('Nothing on loop stack, can\'t \"step out\".')
                else:
                    self.__set_force_break(consider_loopstack=True, loopstack=len(self.loop_stack) - 1)
                    break
            elif inp == 'o':
                if len(self.call_stack) <= 0:
                    print('Nothing on call stack, can\'t \"step out\".')
                else:
                    self.__set_force_break(consider_callstack=True, callstack=len(self.call_stack) - 1)
                    break
            elif inp == 'r':
                print(self.get_reg_table(True))
            elif inp == 'rs':
                print(self.get_s_reg_table(True))
            elif inp == 'ra':
                print(self.get_all_reg_table(True))
            elif inp == 'f':
                print(self.get_all_flags_table())
            elif inp == 'ls':
                print(self.loop_stack)
            elif inp == 'cs':
                print(self.call_stack)
            elif inp.split()[0] == 'd':
                dmem_cmd = inp.split()
                if len(dmem_cmd) == 1 and dmem_cmd[0] == 'd':
                    print(self.get_dmem_table(0, len(self.dmem) - 1))
                elif len(dmem_cmd) == 2:
                    if not dmem_cmd[1].isdigit():
                        print('Invalid print dmem command')
                    else:
                        print(self.get_dmem_table(0, int(dmem_cmd[1]) - 1))
                elif len(dmem_cmd) == 3:
                    if not (dmem_cmd[1].isdigit() and dmem_cmd[2].isdigit()):
                        print('Invalid print dmem command')
                    else:
                        print(self.get_dmem_table(int(dmem_cmd[2]), int(dmem_cmd[2]) + int(dmem_cmd[1]) - 1))
                else:
                    print('Invalid print dmem command')
            elif inp == 'a':
                self.print_asm(self.get_pc(), 5, 5)
            elif inp == 'lp':
                print(self.get_breakpoints())
            elif inp.split()[0] == 'b':
                p_cmd = inp.split()
                if len(p_cmd) == 1 and p_cmd[0] == 'b':
                    self.toggle_breakpoint(self.get_pc(), msg=True)
                    self.print_asm(self.get_pc())
                elif len(p_cmd) == 2:
                    self.toggle_breakpoint(p_cmd[1], msg=True)
                    self.print_asm(self.get_pc())
                elif len(p_cmd) == 3:
                    if not p_cmd[2].isdigit():
                        print('Invalid toggle breakpoint command')
                    else:
                        self.toggle_breakpoint(p_cmd[1], int(p_cmd[2]), msg=True)
                        self.print_asm(self.get_pc())
                else:
                    print('Invalid breakpoint command')
            elif inp.split()[0] == 'dump':
                cmd = inp.split()
                if len(cmd) == 2:
                    if not cmd[1].isdigit():
                        print('Invalid dump command.')
                    else:
                        self.dump_dmem(int(cmd[1]), self.DEFAULT_DUMP_FILENAME)
                elif len(cmd) == 3:
                    if not cmd[1].isdigit():
                        print('Invalid dump command.')
                    else:
                        self.dump_dmem(int(cmd[1]), cmd[2])
                else:
                    print('Invalid dump command.')
            else:
                print('Invalid command.')

    def finish(self):
        """Call this when a final 'ret' occurs without anything on the call stack"""
        self.finishFlag = True
        # break here
        self.toggle_breakpoint(self.get_pc())

    def step(self):
        """Next step"""
        halt = False
        if self.get_pc() == self.stop_addr:
            halt = True  # halt after this instruction

        if self.finishFlag:
            print('\nReached \'ret\' instruction with empty call stack. Finishing here.\n')

        is_break, passes = self.__check_break()
        if is_break:
            self.__handle_break_command(passes)

        instr = self.get_instruction(self.get_pc())
        cycles = instr.get_cycles()
        self.stat_record_instr(instr)
        trace_str, jump_addr = instr.execute(self)
        if len(self.loop_stack) and (self.get_pc() == self.get_top_loop_end_addr()):
            if self.dec_top_loop_cnt():
                jump_addr = self.get_top_loop_start_addr()
            else:
                # no loops left, pop the loop stack but continue without jump
                self.pop_loop_stack()

        if jump_addr:
            if jump_addr < 0 or jump_addr >= len(self.imem):
                raise Exception('Invalid jump address')
            self.set_pc(jump_addr)
            cont = True
        else:
            if (self.get_pc() + 1) >= len(self.imem):
                cont = False
            else:
                cont = True
                self.inc_pc()

        if halt:
            return False, trace_str, cycles
        else:
            return cont, trace_str, cycles


if __name__ == "__main__":
    raise Exception('This file is not executable')
