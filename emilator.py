import struct

from . import errors
from . import llilvisitor
from . import memory
from binaryninja import (LLIL_GET_TEMP_REG_INDEX, LLIL_REG_IS_TEMP,
                         Architecture, BinaryView, Endianness, ILRegister,
                         ImplicitRegisterExtend, LowLevelILFunction,
                         SegmentFlag)

fmt = {1: 'B', 2: 'H', 4: 'L', 8: 'Q'}


def sign_extend(value, bits):
    sign_bit = 1 << (bits - 1)
    return (value & (sign_bit - 1)) - (value & sign_bit)


class Emilator(llilvisitor.LLILVisitor):
    def __init__(self, function, view=None):
        super(Emilator, self).__init__()

        if not isinstance(function, LowLevelILFunction):
            raise TypeError('function must be a LowLevelILFunction')

        self._function = function

        if view is None:
            view = BinaryView()

        self._view = view

        self._regs = {}
        self._flags = {}
        self._memory = memory.Memory(function.arch.address_size)

        for segment in view.segments:
            self._memory.map(
                segment.start, segment.length, segment.flags,
                view.read(segment.start, segment.length)
            )

        self._function_hooks = {}
        self.instr_index = 0

    @property
    def function(self):
        return self._function

    @property
    def mapped_memory(self):
        return list(self._memory)

    @property
    def registers(self):
        return dict(self._regs)

    @property
    def function_hooks(self):
        return dict(self._function_hooks)

    @property
    def instr_hooks(self):
        return dict(self._hooks)

    def map_memory(self,
                   start=None,
                   length=0x1000,
                   flags=SegmentFlag.SegmentReadable | SegmentFlag.SegmentWritable,
                   data=None):
        return self._memory.map(start, length, flags, data)

    def unmap_memory(self, base, size):
        raise errors.UnimplementedError('Unmapping memory not implemented')

    def register_function_hook(self, function, hook):
        self._function_hooks[function] = hook

    def register_instruction_hook(self, operand, hook):
        # These hooks will be fallen back on if LLIL_UNIMPLEMENTED
        # is encountered
        pass

    def unregister_function_hook(self, function, hook):
        pass

    def unregister_instruction_hook(self, operand, hook):
        pass

    def set_register_value(self, register, value):
        # If it's a temp register, just set the value no matter what.
        # Maybe this will be an issue eventually, maybe not.
        if (isinstance(register, int) and 
                LLIL_REG_IS_TEMP(register)):
            self._regs[register] = value
            return value

        if isinstance(register, ILRegister):
            if not LLIL_REG_IS_TEMP(register.index):
                register = register.name
            else:
                self._regs[register.index] = value
                return value

        arch = self._function.arch

        reg_info = arch.regs[register]

        # normalize value to be unsigned
        if value < 0:
            value = value + (1 << reg_info.size * 8)

        if register == reg_info.full_width_reg:
            self._regs[register] = value
            return value

        full_width_reg_info = arch.regs[reg_info.full_width_reg]
        full_width_reg_value = self._regs.get(full_width_reg_info.full_width_reg)

        if (full_width_reg_value is None and
                (reg_info.extend == ImplicitRegisterExtend.NoExtend or
                 reg_info.offset != 0)):
            raise errors.UndefinedError(
                'Register {} not defined'.format(
                    reg_info.full_width_reg
                )
            )

        if reg_info.extend == ImplicitRegisterExtend.ZeroExtendToFullWidth:
            full_width_reg_value = value

        elif reg_info.extend == ImplicitRegisterExtend.SignExtendToFullWidth:
            full_width_reg_value = (
                (value ^ ((1 << reg_info.size * 8) - 1)) -
                ((1 << reg_info.size * 8) - 1) +
                (1 << full_width_reg_info.size * 8)
            )

        elif reg_info.extend == ImplicitRegisterExtend.NoExtend:
            # mask off the value that will be replaced
            mask = (1 << reg_info.size * 8) - 1
            full_mask = (1 << full_width_reg_info.size * 8) - 1
            reg_bits = mask << (reg_info.offset * 8)

            full_width_reg_value &= full_mask ^ reg_bits
            full_width_reg_value |= value << reg_info.offset * 8

        self._regs[full_width_reg_info.full_width_reg] = full_width_reg_value

        return full_width_reg_value

    def get_register_value(self, register):
        if (isinstance(register, int) and
                LLIL_REG_IS_TEMP(register)):
            reg_value = self._regs.get(register)

            if reg_value is None:
                raise errors.UndefinedError(
                    'Register {} not defined'.format(
                        LLIL_GET_TEMP_REG_INDEX(register)
                    )
                )

            return reg_value

        if isinstance(register, ILRegister):
            if not LLIL_REG_IS_TEMP(register.index):
                register = register.name
            else:
                reg_value = self._regs.get(register.index)
                if reg_value is None:
                    raise errors.UndefinedError(
                        'Register {} not defined'.format(
                            LLIL_GET_TEMP_REG_INDEX(register)
                        )
                    )
                return reg_value

        reg_info = self._function.arch.regs[register]

        full_reg_value = self._regs.get(reg_info.full_width_reg)

        if full_reg_value is None:
            raise errors.UndefinedError(
                'Register {} not defined'.format(
                    register
                )
            )

        mask = (1 << reg_info.size * 8) - 1

        if register == reg_info.full_width_reg:
            return full_reg_value & mask

        mask = (1 << reg_info.size * 8) - 1
        reg_bits = mask << (reg_info.offset * 8)

        reg_value = (full_reg_value & reg_bits) >> (reg_info.offset * 8)

        return reg_value

    def set_flag_value(self, flag, value):
        self._flags[flag] = value
        return value

    def get_flag_value(self, flag):
        # Assume that any previously unset flag is False
        value = self._flags.get(flag, False)
        return value

    def read_memory(self, addr, length):
        if length not in fmt:
            raise ValueError('read length must be in (1,2,4,8)')

        # XXX: Handle sizes > 8 bytes
        pack_fmt = (
            # XXX: Endianness string bug
            '<' if self._function.arch.endianness == Endianness.LittleEndian
            else ''
        ) + fmt[length]

        if addr not in self._memory:
            raise errors.MemoryAccessError(
                'Address {:x} is not valid.'.format(addr)
            )

        try:
            return struct.unpack(
                pack_fmt, self._memory.read(addr, length)
            )[0]
        except:
            raise errors.MemoryAccessError(
                'Could not read memory at {:x}'.format(addr)
            )

    def write_memory(self, addr, data, length=None):
        # XXX: This is terribly implemented
        if addr not in self._memory:
            raise errors.MemoryAccessError(
                'Address {:x} is not valid.'.format(addr)
            )

        if isinstance(data, int):
            if length not in (1, 2, 4, 8):
                raise KeyError('length is not 1, 2, 4, or 8.')

            # XXX: Handle sizes > 8 bytes
            pack_fmt = (
                # XXX: Endianness string bug
                '<' if self._function.arch.endianness == Endianness.LittleEndian
                else ''
            ) + fmt[length]

            data = struct.pack(pack_fmt, data)

        self._memory.write(addr, data)

        return True

    def execute_instruction(self):
        # Execute the current IL instruction
        instruction = self._function[self.instr_index]

        # increment to next instruction (can be changed by instruction)
        self.instr_index += 1

        self.visit(instruction)

    def run(self):
        while True:
            try:
                yield self.execute_instruction()
            except IndexError:
                if self.instr_index >= len(self.function):
                    raise StopIteration()
                else:
                    raise

    def _find_available_segment(self, size=0x1000, align=1):
        new_segment = None
        current_address = 0
        max_address = (1 << (self._function.arch.address_size * 8)) - 1
        align_mask = (1 << (self._function.arch.address_size * 8)) - align

        while current_address < (max_address - size):
            segment = self._view.get_segment_at(current_address)

            if segment is not None:
                current_address = (segment.end + align) & align_mask
                continue

            segment_end = current_address + size - 1

            if self._view.get_segment_at(segment_end) is None:
                new_segment = current_address
                break

        return new_segment

    def visit_LLIL_SET_REG(self, expr):
        value = self.visit(expr.src)
        self.set_register_value(expr.dest, value)
        return True

    def visit_LLIL_CONST(self, expr):
        return expr.constant

    def visit_LLIL_CONST_PTR(self, expr):
        return expr.constant

    def visit_LLIL_REG(self, expr):
        value = self.get_register_value(expr.src)
        return value

    def visit_LLIL_LOAD(self, expr):
        addr = self.visit(expr.src)
        return self.read_memory(addr, expr.size)

    def visit_LLIL_STORE(self, expr):
        addr = self.visit(expr.dest)
        value = self.visit(expr.src)
        self.write_memory(addr, value, expr.size)
        return True

    def visit_LLIL_PUSH(self, expr):
        sp = self.function.arch.stack_pointer

        value = self.visit(expr.src)

        sp_value = self.get_register_value(sp)

        self.write_memory(sp_value, value, expr.size)

        sp_value -= expr.size

        return self.set_register_value(sp, sp_value)

    def visit_LLIL_POP(self, expr):
        sp = self.function.arch.stack_pointer

        sp_value = self.get_register_value(sp)

        sp_value += expr.size

        value = self.read_memory(sp_value, expr.size)

        self.set_register_value(sp, sp_value)

        return value

    def visit_LLIL_GOTO(self, expr):
        self.instr_index = expr.dest
        return self.instr_index

    def visit_LLIL_IF(self, expr):
        condition = self.visit(expr.condition)

        if condition:
            self.instr_index = expr.true
        else:
            self.instr_index = expr.false

        return condition

    def visit_LLIL_CMP_NE(self, expr):
        left = self.visit(expr.left)
        right = self.visit(expr.right)

        return left != right

    def visit_LLIL_CMP_E(self, expr):
        left = self.visit(expr.left)
        right = self.visit(expr.right)

        return left == right

    def visit_LLIL_CMP_SLT(self, expr):
        left = self.visit(expr.left)
        right = self.visit(expr.right)

        if (left & (1 << ((expr.size * 8) - 1))):
            left = left - (1 << (expr.size * 8))

        if (right & (1 << ((expr.size * 8) - 1))):
            right = right - (1 << (expr.size * 8))

        return left < right

    def visit_LLIL_CMP_UGT(self, expr):
        left = self.visit(expr.left)
        right = self.visit(expr.right)
        return left > right

    def visit_LLIL_ADD(self, expr):
        left = self.visit(expr.left)
        right = self.visit(expr.right)
        mask = (1 << expr.size * 8) - 1
        return (left + right) & mask

    def visit_LLIL_AND(self, expr):
        left = self.visit(expr.left)
        right = self.visit(expr.right)
        return left & right

    def visit_LLIL_OR(self, expr):
        left = self.visit(expr.left)
        right = self.visit(expr.right)
        return left | right

    def visit_LLIL_SUB(self, expr):
        left = self.visit(expr.left)
        right = self.visit(expr.right)
        return left - right

    def visit_LLIL_SET_FLAG(self, expr):
        flag = expr.dest.index
        value = self.visit(expr.src)
        return self.set_flag_value(flag, value)

    def visit_LLIL_FLAG(self, expr):
        flag = expr.src.index
        return self.get_flag_value(flag)

    def visit_LLIL_RET(self, expr):
        # we'll stop for now, but this will need to retrieve the return
        # address and jump to it.
        raise StopIteration

    def visit_LLIL_CALL(self, expr):
        target = self.visit(expr.dest)

        if target in self._function_hooks:
            self._function_hooks[target](self)
            return True

        target_function = self._view.get_function_at(target)

        if not target_function:
            self._view.create_user_function(target)
            self._view.update_analysis_and_wait()
            target_function = self._view.get_function_at(target)

        self._function = target_function.low_level_il
        self.instr_index = 0

        return True

    def visit_LLIL_SX(self, expr):
        orig_value = self.visit(expr.src)
        sign_bit = 1 << ((expr.size * 8) - 1)
        extend_value = (orig_value & (sign_bit - 1)) - (orig_value & sign_bit)
        return extend_value

    def visit_LLIL_ZX(self, expr):
        return self.visit(expr.src)

    def visit_LLIL_XOR(self, expr):
        left = self.visit(expr.left)
        right = self.visit(expr.right)
        return left ^ right

    def visit_LLIL_LSL(self, expr):
        mask = (1 << expr.size * 8) - 1
        left = self.visit(expr.left)
        right = self.visit(expr.right)
        return (left << right) & mask

    def visit_LLIL_LSR(self, expr):
        left = self.visit(expr.left)
        right = self.visit(expr.right)
        return left >> right


if __name__ == '__main__':
    il = LowLevelILFunction(Architecture['x86_64'])
    emi = Emilator(il)

    emi.set_register_value('rbx', -1)
    emi.set_register_value('rsp', 0x1000)

    print('[+] Mapping memory at 0x1000 (size: 0x1000)...')
    emi.map_memory(0x1000, flags=SegmentFlag.SegmentReadable)

    print('[+] Initial Register State:')
    for r, v in emi.registers.iteritems():
        print('\t{}:\t{:x}'.format(r, v))

    il.append(il.push(8, il.const(8, 0xbadf00d)))
    il.append(il.push(8, il.const(8, 0x1000)))
    il.append(il.set_reg(8, 'rax', il.pop(8)))
    il.append(il.set_reg(8, 'rbx', il.load(8, il.reg(8, 'rax'))))

    print('[+] Instructions:')
    for i in range(len(emi.function)):
        print('\t'+repr(il[i]))

    print('[+] Executing instructions...')
    for i in emi.run():
        print('\tInstruction completed.')

    print('[+] Final Register State:')
    for r, v in emi.registers.iteritems():
        print('\t{}:\t{:x}'.format(r, v))
