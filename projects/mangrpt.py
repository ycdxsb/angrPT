import os
import angr
import json
import pefile
import angrutils

class angrPTObject():
    def __init__(self, driver_path, dispatcher_address, ioctl_infos):
        self.global_variable_range_start = 0
        self.global_variable_range_end = 0
        self.external_functions = []
        self.driver_path = driver_path
        self.ioctl_infos = ioctl_infos      
        self.dispatcher_address = dispatcher_address
        
    
    def go_analysis(self):
        if self.get_PE_section() is False:
            print("No .data section in this driver.")
            return {}
        return self.get_function_table()
    
    def get_PE_section(self):     
        """PE section을 순회하면서 .data 영역을 가져오는 함수"""
        pe = pefile.PE(self.driver_path)
        data_section = None
        
        for section in pe.sections:
            if section.Name.decode().strip('\x00') == ".data":
                data_section = section
        if data_section is None:
            return False
        
        self.global_variable_range_start = pe.OPTIONAL_HEADER.ImageBase + data_section.VirtualAddress
        self.global_variable_range_end = self.global_variable_range_start + data_section.SizeOfRawData
        
        for entry in pe.DIRECTORY_ENTRY_IMPORT:
            for imp in entry.imports:
                if imp.name:
                    self.external_functions.append(imp.address)
        return True

    def find_function_end(self, p, function_address):
        """함수 블록 끝을 반환하는 함수"""
        function = p.kb.functions[function_address]
        block_addresses = [x.addr for x in function.blocks]
        
        last_block_size = 0
        for b in function.blocks:
            last_block_size = b.size
        
        return max(block_addresses) + last_block_size
    
    def get_function_table(self):
        """함수 블록을 가져오고 안에 자세한 정보를 저장하는 함수"""
        start_address = self.dispatcher_address
        
        p = angr.Project(self.driver_path, auto_load_libs=False)#, main_opts={"custom_base_addr": start_address})
        cfg = p.analyses.CFG()
        called_functions = dict()
        
        block_address = []
        
        for block in cfg.kb.functions[start_address].blocks:            
            block_address.append(block.addr)
        
        
        block_min = min(block_address)
        block_max = max(block_address)

        for addr, func in cfg.kb.functions.items():
            if block_min <= addr <= block_max:                
                for block in func.blocks:                    
                    if block.addr not in self.external_functions:
                        for disasm_block in block.capstone.insns:
                            
                            if disasm_block.mnemonic == 'call' and called_functions.get(hex(disasm_block.address)) == None \
                                and disasm_block.op_str.startswith('0x'): #and disasm_block.op_str not in value_cache:                                
                                called_functions[hex(disasm_block.address)] = {
                                    'address' : disasm_block.op_str,
                                }    
                        
        value_cache = list()
        called_functions_completed = dict()
        for key, value in called_functions.items():
            if value['address'] not in value_cache:            
                function_block_max = self.find_function_end(p, int(value['address'], 16))
                value_cache.append(value['address'])
                
                called_functions_completed[key] = {
                    'address' : int(value['address'], 16),
                    'max' : function_block_max
                }

        ######################################
        cfg = p.analyses.CFGFast()
        global_access_offset = list(p.kb.xrefs.get_xrefs_by_dst_region(self.global_variable_range_start, self.global_variable_range_end))
        
        global_xref = list()
        for var in global_access_offset:
            #함수 정적 분석 순회
            for key, value in called_functions_completed.items():
                internal_function_address_start = called_functions_completed[key]['address']
                internal_function_address_max = called_functions_completed[key]['max']
                
                if internal_function_address_start <= var.ins_addr <= internal_function_address_max:
                    global_xref.append(var)
                
            #IOCTL 18!!!!!!!!!!!!!!!!!
            for ioctl_num, rng in self.ioctl_infos.items():
                if rng['start'] <= var.ins_addr <= rng['end']:
                    global_xref.append(var)
        
        return self.ioctl_2_global(p, called_functions_completed, self.ioctl_infos, global_xref)
        
    def ioctl_2_global(self, p, called_functions_completed, ioctl_block_addresses, global_xref):
        ioctl_call_table = {}
        
        for ioctl_num in ioctl_block_addresses.keys():
            for rip in called_functions_completed.keys():
                if ioctl_block_addresses[ioctl_num]['start'] <= int(rip, 16) <= ioctl_block_addresses[ioctl_num]['end']:
                    if ioctl_call_table.get(ioctl_num):
                        ioctl_call_table[ioctl_num].append([
                            called_functions_completed[rip]['address'], called_functions_completed[rip]['max']
                        ])
                    else:
                        ioctl_call_table[ioctl_num] = list(
                        )
                        ioctl_call_table[ioctl_num].append([
                            called_functions_completed[rip]['address'], called_functions_completed[rip]['max']
                        ])
                        
        #드물게 call_table이 없는 경우도 존재
        for ioctl_num, rng in ioctl_block_addresses.items():
            if ioctl_call_table.get(ioctl_num):
                continue
            ioctl_call_table[ioctl_num] = list()
            ioctl_call_table[ioctl_num].append([
                rng['start'], rng['end']
            ]) 
                        
        for xref in global_xref:
            block = p.factory.block(xref.ins_addr)

            block_insn_op_str = [insn.op_str for insn in block.capstone.insns]
            block_insn_mnemonic = [insn.mnemonic for insn in block.capstone.insns]
            
            #print(block)
            #print(block_insn_op_str)
            #print(block_insn_mnemonic)
            if block_insn_mnemonic[0] == 'cmp' and (0 <= block_insn_op_str[0].split(',')[0].find('ptr [rip') <= 8) :
                xref.type = 1
            else:            
                if block_insn_mnemonic[0] in ['mov','movabs','movaps','and','or']  and (0 <= block_insn_op_str[0].split(',')[0].find('ptr [rip') <= 8):
                    xref.type = 2                
                else:
                    for idx in range(len(block_insn_op_str) - 1):
                        if block_insn_mnemonic[idx] == 'mov':
                            reg = block_insn_op_str[idx].split(',')[0]
                            next_reg_position = block_insn_op_str[idx + 1].find('ptr')
                            
                            if next_reg_position > 8 or next_reg_position == -1:
                                continue
                            
                            if reg == block_insn_op_str[idx + 1][next_reg_position: next_reg_position + len(reg)]:
                                xref.type = 2  

        ioctl_dependancy = {}
        for xref in global_xref:
            for ioctl_num, rng_list in ioctl_call_table.items():
                for rng in rng_list:
                    if rng[0] <= xref.ins_addr <= rng[1]:
                        if ioctl_dependancy.get(ioctl_num):
                            ioctl_dependancy[ioctl_num].append({
                                'addr' : xref.dst,
                                'mode' : xref.type_string
                            })
                        else:
                            ioctl_dependancy[ioctl_num] = list()
                            ioctl_dependancy[ioctl_num].append({
                                'addr' : xref.dst,
                                'mode' : xref.type_string
                            }) 
        
        #! 실험용 코드.. (read <-> write)
        # write_to_read_connections = {}
        # for ioctl_num, xref_values in ioctl_dependancy.items():
        #     read_addrs = {value['addr'] for value in xref_values if value['mode'] == 'read'}
        #     write_addrs = {value['addr'] for value in xref_values if value['mode'] == 'write'}
        #     unknown_addrs = {value['addr'] for value in xref_values if value['mode'] == 'offset'}
            
        #     for write_addr in write_addrs:
        #         if write_addr not in write_to_read_connections:
        #             write_to_read_connections[write_addr] = set()
        #         for read_addr in read_addrs:
        #             write_to_read_connections[write_addr].add(read_addr)
            
        #print(ioctl_dependancy)      
        #print(write_to_read_connections)

        return ioctl_dependancy
