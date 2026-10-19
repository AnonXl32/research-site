---
title: Rust-Based Hypervisors for Memory Introspection and Reverse Engineering
description: "Research notes comparing illusion-rs and matrix-rs: EPT hooks, early-boot and runtime introspection, instruction replay, and detection trade-offs."
date: 2026-10-20
category: Hypervisor research
tags: [Rust, Hypervisors, Memory introspection]
---

* On this page
{:toc}

> **Project credits and context.** These notes discuss the research prototypes [illusion-rs](https://github.com/memN0ps/illusion-rs) and [matrix-rs](https://github.com/memN0ps/matrix-rs), developed by **memN0ps**. The code fragments are illustrative sketches, not a complete, tested implementation. Behavior depends on the CPU, Windows version, and project revision; the discussion does not guarantee invisibility or compatibility on current systems.

## Abstract

This article presents two proof-of-concept Rust implementations for stealthy Windows kernel introspection using Extended Page Tables (EPT): **illusion-rs** (UEFI-based, early boot hooking) and **matrix-rs** (kernel driver-based, runtime hooking). Both leverage Second-Level Address Translation (SLAT) to redirect control flow without modifying guest memory, bypassing integrity checks like PatchGuard.

---

## 1. Introduction

Traditional kernel hooking modifies guest memory directly, making detection trivial and triggering PatchGuard on Windows. EPT-based hypervisor introspection operates outside the guest's control plane, enabling:

- **Stealth**: No modification to guest virtual or physical memory
- **Integrity preservation**: Original code remains unchanged
- **Early boot visibility** (illusion-rs): Hooks before security controls initialize
- **Runtime flexibility** (matrix-rs): Dynamic hooking via kernel driver

### Core Concepts

| Term | Definition |
|------|------------|
| **EPT** | Intel's Second-Level Address Translation; translates guest physical → host physical addresses |
| **Shadow page** | Hypervisor-controlled copy of guest memory with modified permissions/content |
| **VM-exit** | CPU transition from guest to hypervisor mode triggered by instructions or memory violations |
| **MTF** | Monitor Trap Flag; enables single-stepping for instruction replay |

---

## 2. Detection Timing: SSDT Initialization

Before installing hooks, the hypervisor must wait until the System Service Descriptor Table (SSDT) is fully initialized in `ntoskrnl.exe`. Installing hooks prematurely causes crashes when resolving syscall numbers through invalid memory.

### Reliable Trigger Points

| Processor | Execution Path |
|-----------|----------------|
| **Intel** | `KiInitializeKernel` → `KeCompactServiceTable` → `KiSetCacheInformation` → `cpuid(4, 0)` |
| **AMD** | Conditional: If `CPUID(0x80000001).ECX[22]` set → `cpuid(0x8000001D, 0)`, else fallback to `cpuid(0x80000005/06)` |

**Implementation note**: The original illusion-rs used `cpuid(2, 0)` as a hack that worked empirically but isn't semantically correct. For production adaptations, use `cpuid(4, 0)` (Intel) or `cpuid(0x8000001D, 0)` (AMD).

### Code Pattern

```rust
match leaf {
    leaf if leaf == CpuidLeaf::CacheInformation as u32 => {
        if !hook_manager.has_cpuid_cache_info_been_called {
            hook_manager.manage_kernel_ept_hook(
                vm,
                djb2_hash("NtCreateFile"),
                0x0055,
                EptHookType::Function(InlineHookType::Vmcall),
                true,
            )?;
            hook_manager.has_cpuid_cache_info_been_called = true;
        }
    }
}
```

---

## 3. Illusion: UEFI-Based Hypervisor

### Architecture Overview

- **Loading**: UEFI firmware (Type-1 hypervisor)
- **EPT Model**: Per-logical-processor EPTs
- **Hook Method**: VMCALL + Monitor Trap Flag single-stepping
- **Best for**: Early boot introspection, firmware-level visibility

### EPT Hook Installation Pipeline

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Map 2MB large page to pre-allocated page table           │
│ 2. Split large page → 512 × 4KB entries                     │
│ 3. Allocate shadow page from heap                           │
│ 4. Clone guest memory → shadow page                         │
│ 5. Insert VMCALL inline hook at function offset             │
│ 6. Revoke EXECUTE permission on original page               │
│ 7. Invalidate TLB/EPT caches (INVEPT All Contexts)          │
│ 8. VM-exit on EPT violation → swap to shadow page           │
│ 9. VMCALL triggers VM-exit → introspection                  │
│ 10. MTF single-step → replay displaced instructions         │
│ 11. Restore original page + reapply hook                    │
└─────────────────────────────────────────────────────────────┘
```

### Key Implementation Details

#### Memory Management

- Pre-allocated heap (default 64MB) with linked-list allocator
- Fixed-size pool avoids runtime allocation failures during VM-exit handling
- All logical processors share the same pool

#### Instruction Replay with MTF

```rust
// During VMCALL handler:
vm.mtf_counter = Some(instruction_count);
set_monitor_trap_flag(true);
update_guest_interrupt_flag(vm, false)?;

// MTF handler decrements counter per instruction:
*counter = counter.saturating_sub(1);
if *counter == 0 {
    // Prologue fully replayed; reapply hook
    swap_page(guest_pa, shadow_pa, EXECUTE);
    set_monitor_trap_flag(false);
    restore_guest_interrupt_flag(vm)?;
}
```

#### Hypercall Channel

User-mode control via `CPUID` interception—unprivileged, password-protected backdoor for enabling/disabling hooks at runtime.

---

## 4. Matrix: Windows Kernel Driver-Based Hypervisor

### Architecture Overview

- **Loading**: Windows kernel driver (Type-2 hypervisor)
- **EPT Model**: Shared dual-EPT across all processors
- **Hook Method**: INT3 breakpoints + dynamic EPTP switching
- **Best for**: Runtime introspection, easier integration

### Dual-EPT Design

| EPT | Permissions | Purpose |
|-----|-------------|---------|
| **Primary** | `READ_WRITE` | Normal execution; read/write access |
| **Secondary** | `EXECUTE` only | Shadow pages with hook logic |

### Hook Installation Pipeline

```
┌─────────────────────────────────────────────────────────────┐
│ 1. Resolve target function (export table or SSDT)           │
│ 2. Create trampoline to preserve overwritten prologue       │
│ 3. Copy guest page → shadow region                          │
│ 4. Inject INT3 (0xCC) at function entry                     │
│ 5. Configure EXCEPTION_BITMAP to trap INT3                  │
│ 6. Set primary EPT: remove EXECUTE from target page         │
│ 7. Set secondary EPT: EXECUTE only, remap to shadow         │
│ 8. Load primary_eptp into VMCS                              │
│ 9. On EPT violation (execute access) → switch to secondary  │
│ 10. INT3 triggers VM-exit → redirect RIP to handler         │
│ 11. After handler: jump via trampoline to original function │
└─────────────────────────────────────────────────────────────┘
```

### EPTP Switching Logic

```rust
// In EPT violation handler:
if ept_violation_qualification.readable && 
   ept_violation_qualification.writable && 
   !ept_violation_qualification.executable {
    
    // Execute access denied in primary → switch to secondary
    vmwrite(EPTP_FULL, secondary_eptp);
}

if !ept_violation_qualification.readable && 
   !ept_violation_qualification.writable && 
   ept_violation_qualification.executable {
    
    // Read/write access denied in secondary → switch back to primary
    vmwrite(EPTP_FULL, primary_eptp);
}
```

### Trampoline Design

```asm
; Trampoline stub after INT3 overwrite
push [rip+target_addr]  ; Indirect jump to original function continuation
jmp qword ptr [rip+0]
```

Preserves registers and performs RIP-relative redirection without clobbering general-purpose registers.

---

## 5. Comparison: Illusion vs. Matrix

| Dimension | Illusion (UEFI) | Matrix (Driver) |
|-----------|-----------------|-----------------|
| **Loading Stage** | Early boot (firmware) | Runtime (driver load) |
| **EPT Model** | Per-processor EPTs | Shared dual-EPT |
| **Hook Trigger** | VMCALL + MTF | INT3 + breakpoint exception |
| **VM-exits per hook** | 2 (violation + VMCALL) + MTF steps | 2 (violation + breakpoint) |
| **Control Channel** | CPUID hypercalls (user-mode) | None (currently) |
| **Complexity** | Higher (per-processor sync) | Lower (global state) |
| **Detection Risk** | Firmware visibility | Driver enumeration |
| **Use Case** | Pre-security-control hooking | Post-boot runtime analysis |

### Trade-off Summary

**Choose illusion-rs if:**
- Need early boot visibility before security controls load
- Require fine-grained per-processor control
- Want fully host-side introspection without guest memory allocation

**Choose matrix-rs if:**
- Need runtime hooking without UEFI/firmware integration
- Prefer simpler shared-state model
- Require dynamically loadable driver

---

## 6. Detection Vectors

While EPT hooks avoid guest memory modifications, they introduce detectable artifacts:

| Technique | Mechanism |
|-----------|-----------|
| **Timing side-channels** | RDTSC latency spikes during VM-exits |
| **Thread discrepancy** | Multi-core timing divergence from per-processor EPTs |
| **Fault profiling** | Instruction Execution Time (IET) anomalies |
| **Descriptor checks** | SIDT/SGDT descriptor length inconsistencies |
| **MSR probing** | Reads to synthetic ranges (0x40000000+) |
| **Cache testing** | INVD/WBINVD caching consistency checks |
| **LBR inspection** | Last Branch Record stack mismatches |
| **Page table audit** | GDT/IDT integrity validation |

**Notable resources**: BattlEye detection research (@vmcall, @daax), Hypervisor-Assisted Hooking Detection (@momo5502), PatchGuard introspection series (@everdox, @ajkhoury).

---

## 7. Appendix

### Guest-Assisted Hooking (Deprecated in Illusion)

Early versions allocated guest memory and injected helper code. Removed due to:
- Increased complexity and synchronization requirements
- Higher detection surface
- Fragility during early kernel initialization phases

### Alternative Hook Methods

| Opcode | Pros | Cons |
|--------|------|------|
| **INT3** | Small (1 byte); simple | Requires breakpoint exception handler |
| **VMCALL** | Explicit hypervisor communication | Needs dedicated handler |
| **JMP `[rip+0]`** | Single VM-exit; no exception | Larger shellcode (14 bytes) |
| **CPUID** | Unprivileged; user-mode accessible | Slower; not ideal for frequent hooks |

### Related Projects

- **[barevisor](https://github.com/tandasat/barevisor)** - Minimal hypervisor template (Satoshi Tanda)
- **[vmi-rs](https://github.com/vmi-rs/vmi)** - Modular Virtual Machine Introspection library (Petr Beneš)
- **[amd_hypervisor](https://github.com/not-matthias/amd_hypervisor)** - AMD SVM-based hypervisor (not-matthias)

---

## 8. Conclusion

Both implementations demonstrate that EPT-based introspection can achieve:

1. **Memory integrity preservation** - No direct guest memory modification
2. **PatchGuard compatibility** - Bypasses kernel patch detection
3. **Fine-grained control** - Per-function syscall monitoring
4. **Extensibility** - Foundation for thread/process hiding, shellcode injection, reflective DLL loading

**Production caveats:**
- Virtualization-Based Security (VBS) prevents third-party hypervisor loading on modern Windows
- Intel VT-rp and nested virtualization barriers add defense layers
- Performance overhead scales with hook frequency and displaced instruction count

These techniques are well-documented in security research and game-hacking communities. The implementations serve as educational templates for advancing low-level security research, defensive instrumentation, and hypervisor-assisted threat detection.

---

## References

1. Intel® 64 and IA-32 Architectures Software Developer's Manual - Section 29.3.2 (EPT Translation Mechanism)
2. @everdox, @ajkhoury. "PatchGuard: Hypervisor-Based Introspection" (Parts 1-2)
3. @momo5502. "Detecting Hypervisor-Assisted Hooking"
4. @daax, @iPower, @ajkhoury, @drew. "How Anti-Cheats Detect System Emulation"
5. Satoshi Tanda. "Hypervisor for Malware Analysis"
6. memN0ps. illusion-rs & matrix-rs GitHub repositories