"""Optional shared SRAM data/execution views for the BES startup path."""
import ctypes
from unicorn import UC_PROT_READ, UC_PROT_WRITE, UC_PROT_EXEC


class RamExecution:
    def __init__(self, cpu, data_base, size, alias_base):
        self.cpu, self.data_base, self.size, self.alias_base = cpu, data_base, size, alias_base
        self.dirty = None
        if alias_base % 4096 or alias_base < 0 or alias_base + size > 2**32:
            raise ValueError('Invalid RAM execution alias')
        if any(alias_base <= end and alias_base + size - 1 >= start
               for start, end, _ in cpu.mem_regions()):
            raise ValueError('RAM execution alias overlaps existing memory')
        data = bytes(cpu.mem_read(data_base, size))
        self.buffer = ctypes.create_string_buffer(size)
        ctypes.memmove(ctypes.addressof(self.buffer), data, size)
        cpu.mem_unmap(data_base, size)
        try:
            cpu.mem_map_ptr(data_base, size, UC_PROT_READ | UC_PROT_WRITE,
                            ctypes.addressof(self.buffer))
            cpu.mem_map_ptr(alias_base, size, UC_PROT_READ | UC_PROT_EXEC,
                            ctypes.addressof(self.buffer))
        except Exception:
            for start, end, _ in list(cpu.mem_regions()):
                if start in (data_base, alias_base):
                    cpu.mem_unmap(start, end - start + 1)
            cpu.mem_map(data_base, size, UC_PROT_READ | UC_PROT_WRITE)
            cpu.mem_write(data_base, data)
            raise

    def invalidate(self, address, size):
        if size:
            start = self.alias_base + address - self.data_base
            end = start + size
            if self.dirty:
                start, end = min(start, self.dirty[0]), max(end, self.dirty[1])
            self.dirty = start, end

    def flush_translations(self):
        # Unicorn's cache API must run outside a live memory/code hook.
        if self.dirty:
            self.cpu.ctl_remove_cache(*self.dirty)
            self.dirty = None

    def disable(self):
        data = bytes(self.cpu.mem_read(self.data_base, self.size))
        self.cpu.mem_unmap(self.alias_base, self.size)
        self.cpu.mem_unmap(self.data_base, self.size)
        self.cpu.mem_map(self.data_base, self.size, UC_PROT_READ | UC_PROT_WRITE)
        self.cpu.mem_write(self.data_base, data)

    def report(self):
        return dict(data_base=hex(self.data_base), execution_base=hex(self.alias_base), size=hex(self.size),
                    shared_backing=True, execution_alias_writable=False,
                    limitations=['Experimental coherent SRAM mirror; no MPU, bus or cache timing.',
                                 'Data view remains non-executable; execution view is read-only.'])
