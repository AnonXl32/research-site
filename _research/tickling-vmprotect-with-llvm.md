---
title: "Tickling VMProtect with LLVM: Lifting, Exploration, and Optimization"
description: "A complete journey from virtualized x64 code to readable LLVM IR: lifting VMProtect handlers, exploring control flow, and building a custom optimization pipeline."
date: 2021-09-08
category: Reverse engineering
tags: [LLVM, VMProtect, Deobfuscation]
historical: false
---

* On this page
{:toc}

This article brings together a collection of experiments I did while playing around with LLVM and VMProtect: lifting virtualized code, exploring its control flow, and optimizing the resulting LLVM IR.

## Introduction
{: #introduction }

First, let me list some important events that led to my curiosity for reversing obfuscation solutions and attack them with LLVM.

- In 2017, a group of friends (SmilingWolf, mrexodia and xSRTsect) and I, hacked up a Python-based devirtualizer and solved a couple of [VMProtect challenges](https://forum.tuts4you.com/topic/39481-devirtualizeme-vmprotect-309/) posted on the Tuts4You forum. That was my first experience reversing a known commercial protector, and taught me that writing compiler-like optimizations, especially built on top of a not so well-designed IR, can be an awful adventure.
- In 2018, a person nicknamed RYDB3RG, posted on Tuts4You a [first insight](https://forum.tuts4you.com/topic/40832-vmprotect-vs-llvm/) on how LLVM optimizations could be beneficial when optimising VMProtected code. Although the easy example that was provided left me with a lot of questions on whether that approach would have been hassle-free or not.
- In 2019, at the SPRO conference in London, [Peter](https://twitter.com/Blips_and_Chitz) and I presented a [paper](https://arxiv.org/abs/1909.01752) titled “SATURN - Software Deobfuscation Framework Based On LLVM”, proposing, you guessed it, a software deobfuscation framework based on LLVM and describing the related pros/cons.

The ideas documented in this post come from insights obtained during the aforementioned research efforts, fine-tuned specifically to get a good-enough output prior to the recompilation/decompilation phases, and should be considered as stable as a proof-of-concept can be.

Before anyone starts a war about which framework is better for the job, pause a few seconds and search the truth deep inside you: every framework has pros/cons and everything boils down to choosing which framework to get mad at when something doesn’t work. I personally decided to get mad at LLVM, which over time proved to be a good research playground, rich with useful analysis and optimizations, consistently maintained, sporting a nice community and deeply entangled with the academic and industry worlds.

With that said, it’s crystal clear that LLVM is not born as a software deobfuscation framework, so scratching your head for hours, diving into its internals and bending them to your needs is a minimum requirement to achieve your goals.

I apologize in advance for the ample presence of long-ish code snippets, but I wanted the reader to have the relevant C++ or LLVM-IR code under their nose while discussing it.

## Lifting
{: #lifting }

The following diagram shows a high-level overview of all the actions and components described in the upcoming sections. The blue blocks represent the inputs, the yellow blocks the actions, the white blocks the intermediate information and the purple block the output.

[![Lifting pipeline](/assets/images/vmprotect/diagram2.svg)](/assets/images/vmprotect/diagram2.svg)

Enough words or code have been spent by others ([1](https://www.msreverseengineering.com/blog/2014/6/23/vmprotect-part-0-basics), [2](https://webtv.univ-lille.fr/video/7566/inside-vmprotect), [3](https://github.com/can1357/NoVmp), [4](https://back.engineering/17/05/2021/)) describing the virtual machine architecture used by VMProtect, so the next paragraph will quickly sum up the involved data structures with an eye on some details that will be fundamental to make LLVM’s job easier. To further simplify the explanation, the following paragraphs will assume the handling of x64 code virtualized by VMProtect 3.x. Drawing a parallel with x86 is trivial.

### Liveness and aliasing information
{: #liveness-and-aliasing-information }

Let’s start by saying that many deobfuscation tools are completely disregarding, or at best unsoundly handling, any information related to the [aliasing](https://en.wikipedia.org/wiki/Aliasing_(computing)) properties bound to the memory accesses present in the code under analysis. LLVM on the contrary is a framework that bases a lot of its optimization passes on precise aliasing information, in such a way that the semantic correctness of the code is preserved. Additionally LLVM also has strong optimization passes benefiting from precise [liveness](https://en.wikipedia.org/wiki/Live_variable_analysis) information, that we absolutely want to take advantage of to clean any unnecessary stores to memory that are irrelevant after the execution of the virtualized code.

This means that we need to pause for a moment to think about the properties of the data structures involved in the code that we are going to lift, keeping in mind how they may alias with each other, for how long we need them to hold their values and if there are safe assumptions that we can feed to LLVM to obtain the best possible result.

A suboptimal representation of the data structures is most likely going to lead to suboptimal lifted code because the LLVM’s optimizations are going to be hindered by the lack of information, erring on the safe side to keep the code semantically correct. Way worse though, is the case where an unsound assumption is going to lead to lifted code that is semantically incorrect.

At a high level we can summarize the data-related virtual machine components as follows:

- **30 virtual registers**: used internally by the virtual machine. Their liveness scope starts after the `VmEnter`, when they are initialized with the incoming host execution context, and ends before the `VmExit(s)`, when their values are copied to the outgoing host execution context. Therefore their state should not persist outside the virtualized code. They are allocated on the stack, in a memory chunk that can only be accessed by specific `VmHandlers` and is therefore guaranteed to be inaccessible by an arbitrary stack access executed by the virtualized code. They are independent from one another, so writing to one won’t affect the others. During the virtual execution they can be accessed as a whole or in subregisters. From now on referred to as `VmRegisters`.
- **19 passing slots**: used by VMProtect to pass the execution state from one `VmBlock` to another. Their liveness starts at the epilogue of a `VmBlock` and ends at the prologue of the successor `VmBlock(s)`. They are allocated on the stack and, while alive, they are only accessed by the push/pop instructions at the epilogue/prologue of each `VmBlock`. They are independent from one another and always accessed as a whole stack slot. From now on referred to as `VmPassingSlots`.
- **16 general purpose registers**: pushed to the stack during the `VmEnter`, loaded and manipulated by means of the `VmRegisters` and popped from the stack during the `VmExit(s)`, reflecting the changes made to them during the virtual execution. Their liveness scope starts before the `VmEnter` and ends after the `VmExit(s)`, so their state must persist after the execution of the virtualized code. They are independent from one another, so writing to one won’t affect the others. Contrarily to the `VmRegisters`, the general purpose registers are always accessed as a whole. The flags register is also treated as the general purpose registers liveness-wise, but it can be directly accessed by some `VmHandlers`.
- **4 general purpose segments**: the `FS` and `GS` general purpose segment registers have their liveness scope matching with the general purpose registers and the underlying segments are guaranteed not to overlap with other memory regions (e.g. `SS`, `DS`). On the contrary, accesses to the `SS` and `DS` segments are not always guaranteed to be distinct with each other. The liveness of the `SS` and `DS` segments also matches with the general purpose registers. A little digression: in the past I noticed that some projects were lifting the stack with an intra-virtual function scope which, in my experience, may cause a number of problems if the virtualized code is not a function with a well-formed stack frame, but rather a shellcode that pops some value pushed prior to entering the virtual machine or pushes some value that needs to live after exiting the virtual machine.

### Helper functions
{: #helper-functions }

With the information gathered from the previous section, we can proceed with defining some basic LLVM-IR structures that will then be used to lift the individual `VmHandlers`, `VmBlocks` and `VmFunctions`.

When I first started with LLVM, my approach to generate the needed structures or instruction chains was through the [IRBuilder](https://llvm.org/doxygen/classllvm_1_1IRBuilder.html) class, but I quickly realized that I was spending more time looking at the documentation to generate the required types and instructions than actually focusing on designing them. Then, while working on SATURN, it became obvious that following [Remill](https://github.com/lifting-bits/remill)’s approach is a winning strategy, at least for the initial high level design phase. In fact their idea is to implement the [structures](https://github.com/lifting-bits/remill/tree/master/include/remill/Arch/X86/Runtime) and [semantics](https://github.com/lifting-bits/remill/tree/master/lib/Arch/X86/Semantics) in C++, compile them to LLVM-IR and dynamically load the generated bitcode file to be used by the lifter.

Without further ado, the following is a minimal implementation of a stub function that we can use as a template to lift a `VmStub` (virtualized code between a `VmEnter` and one or more `VmExit(s)`):

```cpp
struct VirtualRegister final {
  union {
    alignas(1) struct {
      uint8_t b0;
      uint8_t b1;
      uint8_t b2;
      uint8_t b3;
      uint8_t b4;
      uint8_t b5;
      uint8_t b6;
      uint8_t b7;
    } byte;
    alignas(2) struct {
      uint16_t w0;
      uint16_t w1;
      uint16_t w2;
      uint16_t w3;
    } word;
    alignas(4) struct {
      uint32_t d0;
      uint32_t d1;
    } dword;
    alignas(8) uint64_t qword;
  } __attribute__((packed));
} __attribute__((packed));

using rref = size_t &__restrict__;

extern "C" uint8_t RAM[0];
extern "C" uint8_t GS[0];
extern "C" uint8_t FS[0];

extern "C"
size_t HelperStub(
  rref rax, rref rbx, rref rcx,
  rref rdx, rref rsi, rref rdi,
  rref rbp, rref rsp, rref r8,
  rref r9, rref r10, rref r11,
  rref r12, rref r13, rref r14,
  rref r15, rref flags,
  size_t KEY_STUB, size_t RET_ADDR, size_t REL_ADDR,
  rref vsp, rref vip,
  VirtualRegister *__restrict__ vmregs,
  size_t *__restrict__ slots);

extern "C"
size_t HelperFunction(
  rref rax, rref rbx, rref rcx,
  rref rdx, rref rsi, rref rdi,
  rref rbp, rref rsp, rref r8,
  rref r9, rref r10, rref r11,
  rref r12, rref r13, rref r14,
  rref r15, rref flags,
  size_t KEY_STUB, size_t RET_ADDR, size_t REL_ADDR)
{
  // Allocate the temporary virtual registers
  VirtualRegister vmregs[30] = {0};
  // Allocate the temporary passing slots
  size_t slots[19] = {0};
  // Initialize the virtual registers
  size_t vsp = rsp;
  size_t vip = 0;
  // Force the relocation address to 0
  REL_ADDR = 0;
  // Execute the virtualized code
  vip = HelperStub(
    rax, rbx, rcx, rdx, rsi, rdi,
    rbp, rsp, r8, r9, r10, r11,
    r12, r13, r14, r15, flags,
    KEY_STUB, RET_ADDR, REL_ADDR,
    vsp, vip, vmregs, slots);
  // Return the next address(es)
  return vip;
}
```

The `VirtualRegister` structure is meant to represent a `VmRegister`, divided in smaller sub-chunks that are going to be accessed by the `VmHandlers` in ways that don’t necessarily match the access to the subregisters on the x64 architecture. As an example, virtualizing the 64 bits `bswap` instruction will yield `VmHandlers` accessing all the word sub-chunks of a `VmRegister`. The `__attribute__((packed))` is meant to generate a structure without padding bytes, matching the exact data layout used by a `VmRegister`.

The `rref` definition is a convenience type adopted in the definition of the arguments used by the helper functions, that, once compiled to LLVM-IR, will generate a pointer parameter with a [noalias](https://llvm.org/docs/LangRef.html#noalias) attribute. The `noalias` attribute is hinting to the compiler that any memory access happening inside the function that is not dereferencing a pointer derived from the pointer parameter, is guaranteed not to alias with a memory access dereferencing a pointer derived from the pointer parameter.

The `RAM`, `GS` and `FS` array definitions are convenience zero-length arrays that we can use to generate indexed memory accesses to a generic memory slot (stack segment, data segment), `GS` segment and `FS` segment. The accesses will be generated as [getelementptr](https://llvm.org/doxygen/classllvm_1_1GetElementPtrInst.html) instructions and LLVM will automatically treat a pointer with base `RAM` as not aliasing with a pointer with base `GS` or `FS`, which is extremely convenient to us.

The `HelperStub` function prototype is a convenience declaration that we’ll be able to use in the lifter to represent a single `VmBlock`. It accepts as parameters the sequence of general purpose register pointers, the flags register pointer, three key values (`KEY_STUB`, `RET_ADDR`, `REL_ADDR`) pushed by each `VmEnter`, the virtual stack pointer, the virtual program counter, the `VmRegisters` pointer and the `VmPassingSlots` pointer.

The `HelperFunction` function definition is a convenience template that we’ll be able to use in the lifter to represent a single `VmStub`. It accepts as parameters the sequence of general purpose register pointers, the flags register pointer and the three key values (`KEY_STUB`, `RET_ADDR`, `REL_ADDR`) pushed by each `VmEnter`. The body is declaring an array of 30 `VmRegisters`, an array of 19 `VmPassingSlots`, the virtual stack pointer and the virtual program counter. Once compiled to LLVM-IR they’ll be turned into [alloca](https://llvm.org/docs/LangRef.html#alloca-instruction) declarations (stack frame allocations), guaranteed not to alias with other pointers used into the function and that will be automatically released at the end of the function scope. As a convenience we are setting the `REL_ADDR` to `0`, but that can be dynamically set to the proper `REL_ADDR` provided by the user according to the needs of the binary under analysis. Last but not least, we are issuing the call to the `HelperStub` function, passing all the needed parameters and obtaining as output the updated instruction pointer, that, in turn, will be returned by the `HelperFunction` too.

The global variable and function declarations are marked as `extern "C"` to avoid any form of name mangling. In fact we want to be able to fetch them from the dynamically loaded LLVM-IR [Module](https://llvm.org/doxygen/classllvm_1_1Module.html) using functions like `getGlobalVariable` and `getFunction`.

The compiled and optimized LLVM-IR code for the described C++ definitions follows:

```llvm
%struct.VirtualRegister = type { %union.anon }
%union.anon = type { i64 }
%struct.anon = type { i8, i8, i8, i8, i8, i8, i8, i8 }

@RAM = external local_unnamed_addr global [0 x i8], align 1
@GS = external local_unnamed_addr global [0 x i8], align 1
@FS = external local_unnamed_addr global [0 x i8], align 1

declare i64 @HelperStub(i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), i64, i64, i64, i64* nonnull align 8 dereferenceable(8), i64* nonnull align 8 dereferenceable(8), %struct.VirtualRegister*, i64*) local_unnamed_addr

define i64 @HelperFunction(i64* noalias nonnull align 8 dereferenceable(8) %rax, i64* noalias nonnull align 8 dereferenceable(8) %rbx, i64* noalias nonnull align 8 dereferenceable(8) %rcx, i64* noalias nonnull align 8 dereferenceable(8) %rdx, i64* noalias nonnull align 8 dereferenceable(8) %rsi, i64* noalias nonnull align 8 dereferenceable(8) %rdi, i64* noalias nonnull align 8 dereferenceable(8) %rbp, i64* noalias nonnull align 8 dereferenceable(8) %rsp, i64* noalias nonnull align 8 dereferenceable(8) %r8, i64* noalias nonnull align 8 dereferenceable(8) %r9, i64* noalias nonnull align 8 dereferenceable(8) %r10, i64* noalias nonnull align 8 dereferenceable(8) %r11, i64* noalias nonnull align 8 dereferenceable(8) %r12, i64* noalias nonnull align 8 dereferenceable(8) %r13, i64* noalias nonnull align 8 dereferenceable(8) %r14, i64* noalias nonnull align 8 dereferenceable(8) %r15, i64* noalias nonnull align 8 dereferenceable(8) %flags, i64 %KEY_STUB, i64 %RET_ADDR, i64 %REL_ADDR) local_unnamed_addr {
entry:
  %vmregs = alloca [30 x %struct.VirtualRegister], align 16
  %slots = alloca [30 x i64], align 16
  %vip = alloca i64, align 8
  %0 = bitcast [30 x %struct.VirtualRegister]* %vmregs to i8*
  call void @llvm.memset.p0i8.i64(i8* nonnull align 16 dereferenceable(240) %0, i8 0, i64 240, i1 false)
  %1 = bitcast [30 x i64]* %slots to i8*
  call void @llvm.memset.p0i8.i64(i8* nonnull align 16 dereferenceable(240) %1, i8 0, i64 240, i1 false)
  %2 = bitcast i64* %vip to i8*
  store i64 0, i64* %vip, align 8
  %arraydecay = getelementptr inbounds [30 x %struct.VirtualRegister], [30 x %struct.VirtualRegister]* %vmregs, i64 0, i64 0
  %arraydecay1 = getelementptr inbounds [30 x i64], [30 x i64]* %slots, i64 0, i64 0
  %call = call i64 @HelperStub(i64* nonnull align 8 dereferenceable(8) %rax, i64* nonnull align 8 dereferenceable(8) %rbx, i64* nonnull align 8 dereferenceable(8) %rcx, i64* nonnull align 8 dereferenceable(8) %rdx, i64* nonnull align 8 dereferenceable(8) %rsi, i64* nonnull align 8 dereferenceable(8) %rdi, i64* nonnull align 8 dereferenceable(8) %rbp, i64* nonnull align 8 dereferenceable(8) %rsp, i64* nonnull align 8 dereferenceable(8) %r8, i64* nonnull align 8 dereferenceable(8) %r9, i64* nonnull align 8 dereferenceable(8) %r10, i64* nonnull align 8 dereferenceable(8) %r11, i64* nonnull align 8 dereferenceable(8) %r12, i64* nonnull align 8 dereferenceable(8) %r13, i64* nonnull align 8 dereferenceable(8) %r14, i64* nonnull align 8 dereferenceable(8) %r15, i64* nonnull align 8 dereferenceable(8) %flags, i64 %KEY_STUB, i64 %RET_ADDR, i64 0, i64* nonnull align 8 dereferenceable(8) %rsp, i64* nonnull align 8 dereferenceable(8) %vip, %struct.VirtualRegister* nonnull %arraydecay, i64* nonnull %arraydecay1)
  ret i64 %call
}
```

### Semantics of the handlers
{: #semantics-of-the-handlers }

We can now move on to the implementation of the semantics of the handlers used by VMProtect. As mentioned before, implementing them directly at the LLVM-IR level can be a tedious task, so we’ll proceed with the same C++ to LLVM-IR logic adopted in the previous section.

The following selection of handlers should give an idea of the logic adopted to implement the handlers’ semantics.

#### STACK_PUSH
{: #stack_push }

To access the stack using the push operation, we define a templated helper function that takes the virtual stack pointer and value to push as parameters.

```cpp
template <typename T> __attribute__((always_inline)) void STACK_PUSH(size_t &vsp, T value) {
  // Update the stack pointer
  vsp -= sizeof(T);
  // Store the value
  std::memcpy(&RAM[vsp], &value, sizeof(T));
}
```

We can see that the virtual stack pointer is decremented using the byte size of the template parameter. Then we proceed to use the `std::memcpy` function to execute a [safe type punning](https://gist.github.com/shafik/848ae25ee209f698763cffee272a58f8#how-do-we-type-pun-correctly) store operation accessing the `RAM` array with the virtual stack pointer as index. The C++ implementation is compiled with `-O3` optimizations, so the function will be inlined (as expected from the `always_inline` attribute) and the `std::memcpy` call will be converted to the proper pointer type cast and store instructions.

#### STACK_POP
{: #stack_pop }

As expected, also the stack pop operation is defined as a templated helper function that takes the virtual stack pointer as parameter and returns the popped value as output.

```cpp
template <typename T> __attribute__((always_inline)) T STACK_POP(size_t &vsp) {
  // Fetch the value
  T value = 0;
  std::memcpy(&value, &RAM[vsp], sizeof(T));
  // Undefine the stack slot
  T undef = UNDEF<T>();
  std::memcpy(&RAM[vsp], &undef, sizeof(T));
  // Update the stack pointer
  vsp += sizeof(T);
  // Return the value
  return value;
}
```

We can see that the value is read from the stack using the same `std::memcpy` logic explained above, an undefined value is written to the current stack slot and the virtual stack pointer is incremented using the byte size of the template parameter. As in the previous case, the `-O3` optimizations will take care of inlining and lowering the `std::memcpy` call.

#### ADD
{: #add }

Being a stack machine, we know that it is going to pop the two input operands from the top of the stack, add them together, calculate the updated flags and push the result and the flags back to the stack. There are four variations of the addition handler, meant to handle 8/16/32/64 bits operands, with the peculiarity that the 8 bits case is really popping 16 bits per operand off the stack and pushing a 16 bits result back to the stack to be consistent with the x64 push/pop alignment rules.

From what we just described the only thing we need is the virtual stack pointer, to be able to access the stack.

```cpp
// ADD semantic

template <typename T>
__attribute__((always_inline))
__attribute__((const))
bool AF(T lhs, T rhs, T res) {
  return AuxCarryFlag(lhs, rhs, res);
}

template <typename T>
__attribute__((always_inline))
__attribute__((const))
bool PF(T res) {
  return ParityFlag(res);
}

template <typename T>
__attribute__((always_inline))
__attribute__((const))
bool ZF(T res) {
  return ZeroFlag(res);
}

template <typename T>
__attribute__((always_inline))
__attribute__((const))
bool SF(T res) {
  return SignFlag(res);
}

template <typename T>
__attribute__((always_inline))
__attribute__((const))
bool CF_ADD(T lhs, T rhs, T res) {
  return Carry<tag_add>::Flag(lhs, rhs, res);
}

template <typename T>
__attribute__((always_inline))
__attribute__((const))
bool OF_ADD(T lhs, T rhs, T res) {
  return Overflow<tag_add>::Flag(lhs, rhs, res);
}

template <typename T>
__attribute__((always_inline))
void ADD_FLAGS(size_t &flags, T lhs, T rhs, T res) {
  // Calculate the flags
  bool cf = CF_ADD(lhs, rhs, res);
  bool pf = PF(res);
  bool af = AF(lhs, rhs, res);
  bool zf = ZF(res);
  bool sf = SF(res);
  bool of = OF_ADD(lhs, rhs, res);
  // Update the flags
  UPDATE_EFLAGS(flags, cf, pf, af, zf, sf, of);
}

template <typename T>
__attribute__((always_inline))
void ADD(size_t &vsp) {
  // Check if it's 'byte' size
  bool isByte = (sizeof(T) == 1);
  // Initialize the operands
  T op1 = 0;
  T op2 = 0;
  // Fetch the operands
  if (isByte) {
    op1 = Trunc(STACK_POP<uint16_t>(vsp));
    op2 = Trunc(STACK_POP<uint16_t>(vsp));
  } else {
    op1 = STACK_POP<T>(vsp);
    op2 = STACK_POP<T>(vsp);
  }
  // Calculate the add
  T res = UAdd(op1, op2);
  // Calculate the flags
  size_t flags = 0;
  ADD_FLAGS(flags, op1, op2, res);
  // Save the result
  if (isByte) {
    STACK_PUSH<uint16_t>(vsp, ZExt(res));
  } else {
    STACK_PUSH<T>(vsp, res);
  }
  // 7. Save the flags
  STACK_PUSH<size_t>(vsp, flags);
}

DEFINE_SEMANTIC_64(ADD_64) = ADD<uint64_t>;
DEFINE_SEMANTIC(ADD_32) = ADD<uint32_t>;
DEFINE_SEMANTIC(ADD_16) = ADD<uint16_t>;
DEFINE_SEMANTIC(ADD_8) = ADD<uint8_t>;
```

We can see that the function definition is templated with a `T` parameter that is internally used to generate the properly-sized stack accesses executed by the `STACK_PUSH` and `STACK_POP` helpers defined above. Additionally we are taking care of truncating and zero extending the special 8 bits case. Finally, after the unsigned addition took place, we rely on Remill’s semantically proven flag computations to calculate the fresh flags before pushing them to the stack.

The other binary and arithmetic operations are implemented following the same structure, with the correct operands access and flag computations.

#### PUSH_VMREG
{: #push_vmreg }

This handler is meant to fetch the value stored in a `VmRegister` and push it to the stack. The value can also be a sub-chunk of the virtual register, not necessarily starting from the base of the `VmRegister` slot. Therefore the function arguments are going to be the virtual stack pointer and the value of the `VmRegister`. The template is additionally defining the size of the pushed value and the offset from the `VmRegister` slot base.

```cpp
template <size_t Size, size_t Offset>
__attribute__((always_inline)) void PUSH_VMREG(size_t &vsp, VirtualRegister vmreg) {
  // Update the stack pointer
  vsp -= ((Size != 8) ? (Size / 8) : ((Size / 8) * 2));
  // Select the proper element of the virtual register
  if constexpr (Size == 64) {
    std::memcpy(&RAM[vsp], &vmreg.qword, sizeof(uint64_t));
  } else if constexpr (Size == 32) {
    if constexpr (Offset == 0) {
      std::memcpy(&RAM[vsp], &vmreg.dword.d0, sizeof(uint32_t));
    } else if constexpr (Offset == 1) {
      std::memcpy(&RAM[vsp], &vmreg.dword.d1, sizeof(uint32_t));
    }
  } else if constexpr (Size == 16) {
    if constexpr (Offset == 0) {
      std::memcpy(&RAM[vsp], &vmreg.word.w0, sizeof(uint16_t));
    } else if constexpr (Offset == 1) {
      std::memcpy(&RAM[vsp], &vmreg.word.w1, sizeof(uint16_t));
    } else if constexpr (Offset == 2) {
      std::memcpy(&RAM[vsp], &vmreg.word.w2, sizeof(uint16_t));
    } else if constexpr (Offset == 3) {
      std::memcpy(&RAM[vsp], &vmreg.word.w3, sizeof(uint16_t));
    }
  } else if constexpr (Size == 8) {
    if constexpr (Offset == 0) {
      uint16_t byte = ZExt(vmreg.byte.b0);
      std::memcpy(&RAM[vsp], &byte, sizeof(uint16_t));
    } else if constexpr (Offset == 1) {
      uint16_t byte = ZExt(vmreg.byte.b1);
      std::memcpy(&RAM[vsp], &byte, sizeof(uint16_t));
    }
    // NOTE: there might be other offsets here, but they were not observed
  }
}

DEFINE_SEMANTIC(PUSH_VMREG_8_LOW) = PUSH_VMREG<8, 0>;
DEFINE_SEMANTIC(PUSH_VMREG_8_HIGH) = PUSH_VMREG<8, 1>;
DEFINE_SEMANTIC(PUSH_VMREG_16_LOWLOW) = PUSH_VMREG<16, 0>;
DEFINE_SEMANTIC(PUSH_VMREG_16_LOWHIGH) = PUSH_VMREG<16, 1>;
DEFINE_SEMANTIC_64(PUSH_VMREG_16_HIGHLOW) = PUSH_VMREG<16, 2>;
DEFINE_SEMANTIC_64(PUSH_VMREG_16_HIGHHIGH) = PUSH_VMREG<16, 3>;
DEFINE_SEMANTIC_64(PUSH_VMREG_32_LOW) = PUSH_VMREG<32, 0>;
DEFINE_SEMANTIC_32(POP_VMREG_32) = POP_VMREG<32, 0>;
DEFINE_SEMANTIC_64(PUSH_VMREG_32_HIGH) = PUSH_VMREG<32, 1>;
DEFINE_SEMANTIC_64(PUSH_VMREG_64) = PUSH_VMREG<64, 0>;
```

We can see how the proper `VmRegister` sub-chunk is accessed based on the size and offset template parameters (e.g. `vmreg.word.w1`, `vmreg.qword`) and how once again the `std::memcpy` is used to implement a safe memory write on the indexed `RAM` array. The virtual stack pointer is also decremented as usual.

#### POP_VMREG
{: #pop_vmreg }

This handler is meant to pop a value from the stack and store it into a `VmRegister`. The value can also be a sub-chunk of the virtual register, not necessarily starting from the base of the `VmRegister` slot. Therefore the function arguments are going to be the virtual stack pointer and a reference to the `VmRegister` to be updated. As before the template is defining the size of the popped value and the offset into the `VmRegister` slot.

```cpp
template <size_t Size, size_t Offset>
__attribute__((always_inline)) void POP_VMREG(size_t &vsp, VirtualRegister &vmreg) {
  // Fetch and store the value on the virtual register
  if constexpr (Size == 64) {
    uint64_t value = 0;
    std::memcpy(&value, &RAM[vsp], sizeof(uint64_t));
    vmreg.qword = value;
  } else if constexpr (Size == 32) {
    if constexpr (Offset == 0) {
      uint32_t value = 0;
      std::memcpy(&value, &RAM[vsp], sizeof(uint32_t));
      vmreg.qword = ((vmreg.qword & 0xFFFFFFFF00000000) | value);
    } else if constexpr (Offset == 1) {
      uint32_t value = 0;
      std::memcpy(&value, &RAM[vsp], sizeof(uint32_t));
      vmreg.qword = ((vmreg.qword & 0x00000000FFFFFFFF) | UShl(ZExt(value), 32));
    }
  } else if constexpr (Size == 16) {
    if constexpr (Offset == 0) {
      uint16_t value = 0;
      std::memcpy(&value, &RAM[vsp], sizeof(uint16_t));
      vmreg.qword = ((vmreg.qword & 0xFFFFFFFFFFFF0000) | value);
    } else if constexpr (Offset == 1) {
      uint16_t value = 0;
      std::memcpy(&value, &RAM[vsp], sizeof(uint16_t));
      vmreg.qword = ((vmreg.qword & 0xFFFFFFFF0000FFFF) | UShl(ZExtTo<uint64_t>(value), 16));
    } else if constexpr (Offset == 2) {
      uint16_t value = 0;
      std::memcpy(&value, &RAM[vsp], sizeof(uint16_t));
      vmreg.qword = ((vmreg.qword & 0xFFFF0000FFFFFFFF) | UShl(ZExtTo<uint64_t>(value), 32));
    } else if constexpr (Offset == 3) {
      uint16_t value = 0;
      std::memcpy(&value, &RAM[vsp], sizeof(uint16_t));
      vmreg.qword = ((vmreg.qword & 0x0000FFFFFFFFFFFF) | UShl(ZExtTo<uint64_t>(value), 48));
    }
  } else if constexpr (Size == 8) {
    if constexpr (Offset == 0) {
      uint16_t byte = 0;
      std::memcpy(&byte, &RAM[vsp], sizeof(uint16_t));
      vmreg.byte.b0 = Trunc(byte);
    } else if constexpr (Offset == 1) {
      uint16_t byte = 0;
      std::memcpy(&byte, &RAM[vsp], sizeof(uint16_t));
      vmreg.byte.b1 = Trunc(byte);
    }
    // NOTE: there might be other offsets here, but they were not observed
  }
  // Clear the value on the stack
  if constexpr (Size == 64) {
    uint64_t undef = UNDEF<uint64_t>();
    std::memcpy(&RAM[vsp], &undef, sizeof(uint64_t));
  } else if constexpr (Size == 32) {
    uint32_t undef = UNDEF<uint32_t>();
    std::memcpy(&RAM[vsp], &undef, sizeof(uint32_t));
  } else if constexpr (Size == 16) {
    uint16_t undef = UNDEF<uint16_t>();
    std::memcpy(&RAM[vsp], &undef, sizeof(uint16_t));
  } else if constexpr (Size == 8) {
    uint16_t undef = UNDEF<uint16_t>();
    std::memcpy(&RAM[vsp], &undef, sizeof(uint16_t));
  }
  // Update the stack pointer
  vsp += ((Size != 8) ? (Size / 8) : ((Size / 8) * 2));
}

DEFINE_SEMANTIC(POP_VMREG_8_LOW) = POP_VMREG<8, 0>;
DEFINE_SEMANTIC(POP_VMREG_8_HIGH) = POP_VMREG<8, 1>;
DEFINE_SEMANTIC(POP_VMREG_16_LOWLOW) = POP_VMREG<16, 0>;
DEFINE_SEMANTIC(POP_VMREG_16_LOWHIGH) = POP_VMREG<16, 1>;
DEFINE_SEMANTIC_64(POP_VMREG_16_HIGHLOW) = POP_VMREG<16, 2>;
DEFINE_SEMANTIC_64(POP_VMREG_16_HIGHHIGH) = POP_VMREG<16, 3>;
DEFINE_SEMANTIC_64(POP_VMREG_32_LOW) = POP_VMREG<32, 0>;
DEFINE_SEMANTIC_64(POP_VMREG_32_HIGH) = POP_VMREG<32, 1>;
DEFINE_SEMANTIC_64(POP_VMREG_64) = POP_VMREG<64, 0>;
```

In this case we can see that the update operation on the sub-chunks of the `VmRegister` is being done with some masking, shifting and zero extensions. This is to help LLVM with merging smaller integer values into a bigger integer value, whenever possible. As we saw in the `STACK_POP` operation, we are writing an undefined value to the current stack slot. Finally we are incrementing the virtual stack pointer.

#### LOAD and LOAD_GS
{: #load-and-load_gs }

Generically speaking the `LOAD` handler is meant to pop an address from the stack, dereference it to load a value from one of the program segments and push the retrieved value to the top of the stack.

The following C++ snippet shows the implementation of a memory load from a generic memory pointer (e.g. SS or DS segments) and from the GS segment:

```cpp
template <typename T> __attribute__((always_inline)) void LOAD(size_t &vsp) {
  // Check if it's 'byte' size
  bool isByte = (sizeof(T) == 1);
  // Pop the address
  size_t address = STACK_POP<size_t>(vsp);
  // Load the value
  T value = 0;
  std::memcpy(&value, &RAM[address], sizeof(T));
  // Save the result
  if (isByte) {
    STACK_PUSH<uint16_t>(vsp, ZExt(value));
  } else {
    STACK_PUSH<T>(vsp, value);
  }
}

DEFINE_SEMANTIC_64(LOAD_SS_64) = LOAD<uint64_t>;
DEFINE_SEMANTIC(LOAD_SS_32) = LOAD<uint32_t>;
DEFINE_SEMANTIC(LOAD_SS_16) = LOAD<uint16_t>;
DEFINE_SEMANTIC(LOAD_SS_8) = LOAD<uint8_t>;

DEFINE_SEMANTIC_64(LOAD_DS_64) = LOAD<uint64_t>;
DEFINE_SEMANTIC(LOAD_DS_32) = LOAD<uint32_t>;
DEFINE_SEMANTIC(LOAD_DS_16) = LOAD<uint16_t>;
DEFINE_SEMANTIC(LOAD_DS_8) = LOAD<uint8_t>;

template <typename T> __attribute__((always_inline)) void LOAD_GS(size_t &vsp) {
  // Check if it's 'byte' size
  bool isByte = (sizeof(T) == 1);
  // Pop the address
  size_t address = STACK_POP<size_t>(vsp);
  // Load the value
  T value = 0;
  std::memcpy(&value, &GS[address], sizeof(T));
  // Save the result
  if (isByte) {
    STACK_PUSH<uint16_t>(vsp, ZExt(value));
  } else {
    STACK_PUSH<T>(vsp, value);
  }
}

DEFINE_SEMANTIC_64(LOAD_GS_64) = LOAD_GS<uint64_t>;
DEFINE_SEMANTIC(LOAD_GS_32) = LOAD_GS<uint32_t>;
DEFINE_SEMANTIC(LOAD_GS_16) = LOAD_GS<uint16_t>;
DEFINE_SEMANTIC(LOAD_GS_8) = LOAD_GS<uint8_t>;
```

By now the process should be clear. The only difference is the accessed zero-length array that will end up as base of the `getelementptr` instruction, which will directly reflect on the aliasing information that LLVM will be able to infer. The same kind of logic is applied to all the read or write memory accesses to the different segments.

#### DEFINE_SEMANTIC
{: #define_semantic }

In the code snippets of this section you may have noticed three macros named `DEFINE_SEMANTIC_64`, `DEFINE_SEMANTIC_32` and `DEFINE_SEMANTIC`. They are the umpteenth trick borrowed from Remill and are meant to generate global variables with unmangled names, pointing to the function definition of the specialized template handlers. As an example, the `ADD` semantic definition for the 8/16/32/64 bits cases looks like this at the LLVM-IR level:

```llvm
@SEM_ADD_64 = dso_local constant void (i64*)* @_Z3ADDIyEvRm, align 8
@SEM_ADD_32 = dso_local constant void (i64*)* @_Z3ADDIjEvRm, align 8
@SEM_ADD_16 = dso_local constant void (i64*)* @_Z3ADDItEvRm, align 8
@SEM_ADD_8 = dso_local constant void (i64*)* @_Z3ADDIhEvRm, align 8
```

#### UNDEF
{: #undef }

In the code snippets of this section you may also have noticed the usage of a function called `UNDEF`. This function is used to store a fictitious `__undef` value after each pop from the stack. This is done to signal to LLVM that the popped value is no longer needed after being popped from the stack.

The `__undef` value is modeled as a global variable, which during the first phase of the optimization pipeline will be used by passes like [DSE](https://llvm.org/doxygen/DeadStoreElimination_8cpp.html) to kill overlapping post-dominated dead stores and it’ll be replaced with a real [undef](https://llvm.org/doxygen/classllvm_1_1UndefValue.html) value near the end of the optimization pipeline such that the related store instruction will be gone on the final optimized LLVM-IR function.

### Lifting a basic block
{: #lifting-a-basic-block }

We now have a bunch of templates, structures and helper functions, but how do we actually end up lifting some virtualized code?

The high level idea is the following:

- A new LLVM-IR function with the `HelperStub` signature is generated;
- The function’s body is populated with [call](https://llvm.org/doxygen/classllvm_1_1CallInst.html) instructions to the `VmHandler` helper functions fed with the proper arguments (obtained from the `HelperStub` parameters);
- The optimization pipeline is executed on the function, resulting in the inlining of all the helper functions (that are marked `always_inline`) and in the propagation of the values;
- The updated state of the `VmRegisters`, `VmPassingSlots` and stores to the segments is optimized, removing most of the obfuscation patterns used by VMProtect;
- The updated state of the virtual stack pointer and virtual instruction pointer is computed.

A fictitious example of a full pipeline based on the `HelperStub` function, implemented at the C++ level and optimized to obtain propagated LLVM-IR code follows:

```cpp
extern "C" __attribute__((always_inline)) size_t SimpleExample_HelperStub(
  rptr rax, rptr rbx, rptr rcx,
  rptr rdx, rptr rsi, rptr rdi,
  rptr rbp, rptr rsp, rptr r8,
  rptr r9, rptr r10, rptr r11,
  rptr r12, rptr r13, rptr r14,
  rptr r15, rptr flags,
  size_t KEY_STUB, size_t RET_ADDR, size_t REL_ADDR, rptr vsp,
  rptr vip, VirtualRegister *__restrict__ vmregs,
  size_t *__restrict__ slots) {

  PUSH_REG(vsp, rax);
  PUSH_REG(vsp, rbx);
  POP_VMREG<64, 0>(vsp, vmregs[1]);
  POP_VMREG<64, 0>(vsp, vmregs[0]);
  PUSH_VMREG<64, 0>(vsp, vmregs[0]);
  PUSH_VMREG<64, 0>(vsp, vmregs[1]);
  ADD<uint64_t>(vsp);
  POP_VMREG<64, 0>(vsp, vmregs[2]);
  POP_VMREG<64, 0>(vsp, vmregs[3]);
  PUSH_VMREG<64, 0>(vsp, vmregs[3]);
  POP_REG(vsp, rax);

  return vip;
}
```

The C++ `HelperStub` function with calls to the handlers. This only serves as an example, normally the LLVM-IR for this is automatically generated from VM bytecode.

```llvm
define dso_local i64 @SimpleExample_HelperStub(i64* noalias nonnull align 8 dereferenceable(8) %rax, i64* noalias nonnull align 8 dereferenceable(8) %rbx, i64* noalias nonnull align 8 dereferenceable(8) %rcx, i64* noalias nonnull align 8 dereferenceable(8) %rdx, i64* noalias nonnull align 8 dereferenceable(8) %rsi, i64* noalias nonnull align 8 dereferenceable(8) %rdi, i64* noalias nonnull align 8 dereferenceable(8) %rbp, i64* noalias nonnull align 8 dereferenceable(8) %rsp, i64* noalias nonnull align 8 dereferenceable(8) %r8, i64* noalias nonnull align 8 dereferenceable(8) %r9, i64* noalias nonnull align 8 dereferenceable(8) %r10, i64* noalias nonnull align 8 dereferenceable(8) %r11, i64* noalias nonnull align 8 dereferenceable(8) %r12, i64* noalias nonnull align 8 dereferenceable(8) %r13, i64* noalias nonnull align 8 dereferenceable(8) %r14, i64* noalias nonnull align 8 dereferenceable(8) %r15, i64* noalias nonnull align 8 dereferenceable(8) %flags, i64 %KEY_STUB, i64 %RET_ADDR, i64 %REL_ADDR, i64* noalias nonnull align 8 dereferenceable(8) %vsp, i64* noalias nonnull align 8 dereferenceable(8) %vip, %struct.VirtualRegister* noalias %vmregs, i64* noalias %slots) local_unnamed_addr {
entry:
  %rax.addr = alloca i64*, align 8
  %rbx.addr = alloca i64*, align 8
  %rcx.addr = alloca i64*, align 8
  %rdx.addr = alloca i64*, align 8
  %rsi.addr = alloca i64*, align 8
  %rdi.addr = alloca i64*, align 8
  %rbp.addr = alloca i64*, align 8
  %rsp.addr = alloca i64*, align 8
  %r8.addr = alloca i64*, align 8
  %r9.addr = alloca i64*, align 8
  %r10.addr = alloca i64*, align 8
  %r11.addr = alloca i64*, align 8
  %r12.addr = alloca i64*, align 8
  %r13.addr = alloca i64*, align 8
  %r14.addr = alloca i64*, align 8
  %r15.addr = alloca i64*, align 8
  %flags.addr = alloca i64*, align 8
  %KEY_STUB.addr = alloca i64, align 8
  %RET_ADDR.addr = alloca i64, align 8
  %REL_ADDR.addr = alloca i64, align 8
  %vsp.addr = alloca i64*, align 8
  %vip.addr = alloca i64*, align 8
  %vmregs.addr = alloca %struct.VirtualRegister*, align 8
  %slots.addr = alloca i64*, align 8
  %agg.tmp = alloca %struct.VirtualRegister, align 1
  %agg.tmp4 = alloca %struct.VirtualRegister, align 1
  %agg.tmp10 = alloca %struct.VirtualRegister, align 1
  store i64* %rax, i64** %rax.addr, align 8
  store i64* %rbx, i64** %rbx.addr, align 8
  store i64* %rcx, i64** %rcx.addr, align 8
  store i64* %rdx, i64** %rdx.addr, align 8
  store i64* %rsi, i64** %rsi.addr, align 8
  store i64* %rdi, i64** %rdi.addr, align 8
  store i64* %rbp, i64** %rbp.addr, align 8
  store i64* %rsp, i64** %rsp.addr, align 8
  store i64* %r8, i64** %r8.addr, align 8
  store i64* %r9, i64** %r9.addr, align 8
  store i64* %r10, i64** %r10.addr, align 8
  store i64* %r11, i64** %r11.addr, align 8
  store i64* %r12, i64** %r12.addr, align 8
  store i64* %r13, i64** %r13.addr, align 8
  store i64* %r14, i64** %r14.addr, align 8
  store i64* %r15, i64** %r15.addr, align 8
  store i64* %flags, i64** %flags.addr, align 8
  store i64 %KEY_STUB, i64* %KEY_STUB.addr, align 8
  store i64 %RET_ADDR, i64* %RET_ADDR.addr, align 8
  store i64 %REL_ADDR, i64* %REL_ADDR.addr, align 8
  store i64* %vsp, i64** %vsp.addr, align 8
  store i64* %vip, i64** %vip.addr, align 8
  store %struct.VirtualRegister* %vmregs, %struct.VirtualRegister** %vmregs.addr, align 8
  store i64* %slots, i64** %slots.addr, align 8
  %0 = load i64*, i64** %vsp.addr, align 8
  %1 = load i64*, i64** %rax.addr, align 8
  %2 = load i64, i64* %1, align 8
  call void @_Z8PUSH_REGRmm(i64* nonnull align 8 dereferenceable(8) %0, i64 %2)
  %3 = load i64*, i64** %vsp.addr, align 8
  %4 = load i64*, i64** %rbx.addr, align 8
  %5 = load i64, i64* %4, align 8
  call void @_Z8PUSH_REGRmm(i64* nonnull align 8 dereferenceable(8) %3, i64 %5)
  %6 = load i64*, i64** %vsp.addr, align 8
  %7 = load %struct.VirtualRegister*, %struct.VirtualRegister** %vmregs.addr, align 8
  %arrayidx = getelementptr inbounds %struct.VirtualRegister, %struct.VirtualRegister* %7, i64 1
  call void @_Z9POP_VMREGILm64ELm0EEvRmR15VirtualRegister(i64* nonnull align 8 dereferenceable(8) %6, %struct.VirtualRegister* nonnull align 1 dereferenceable(8) %arrayidx)
  %8 = load i64*, i64** %vsp.addr, align 8
  %9 = load %struct.VirtualRegister*, %struct.VirtualRegister** %vmregs.addr, align 8
  %arrayidx1 = getelementptr inbounds %struct.VirtualRegister, %struct.VirtualRegister* %9, i64 0
  call void @_Z9POP_VMREGILm64ELm0EEvRmR15VirtualRegister(i64* nonnull align 8 dereferenceable(8) %8, %struct.VirtualRegister* nonnull align 1 dereferenceable(8) %arrayidx1)
  %10 = load i64*, i64** %vsp.addr, align 8
  %11 = load %struct.VirtualRegister*, %struct.VirtualRegister** %vmregs.addr, align 8
  %arrayidx2 = getelementptr inbounds %struct.VirtualRegister, %struct.VirtualRegister* %11, i64 0
  %12 = bitcast %struct.VirtualRegister* %agg.tmp to i8*
  %13 = bitcast %struct.VirtualRegister* %arrayidx2 to i8*
  call void @llvm.memcpy.p0i8.p0i8.i64(i8* align 1 %12, i8* align 1 %13, i64 8, i1 false)
  %coerce.dive = getelementptr inbounds %struct.VirtualRegister, %struct.VirtualRegister* %agg.tmp, i32 0, i32 0
  %coerce.dive3 = getelementptr inbounds %union.anon, %union.anon* %coerce.dive, i32 0, i32 0
  %14 = load i64, i64* %coerce.dive3, align 1
  call void @_Z10PUSH_VMREGILm64ELm0EEvRm15VirtualRegister(i64* nonnull align 8 dereferenceable(8) %10, i64 %14)
  %15 = load i64*, i64** %vsp.addr, align 8
  %16 = load %struct.VirtualRegister*, %struct.VirtualRegister** %vmregs.addr, align 8
  %arrayidx5 = getelementptr inbounds %struct.VirtualRegister, %struct.VirtualRegister* %16, i64 1
  %17 = bitcast %struct.VirtualRegister* %agg.tmp4 to i8*
  %18 = bitcast %struct.VirtualRegister* %arrayidx5 to i8*
  call void @llvm.memcpy.p0i8.p0i8.i64(i8* align 1 %17, i8* align 1 %18, i64 8, i1 false)
  %coerce.dive6 = getelementptr inbounds %struct.VirtualRegister, %struct.VirtualRegister* %agg.tmp4, i32 0, i32 0
  %coerce.dive7 = getelementptr inbounds %union.anon, %union.anon* %coerce.dive6, i32 0, i32 0
  %19 = load i64, i64* %coerce.dive7, align 1
  call void @_Z10PUSH_VMREGILm64ELm0EEvRm15VirtualRegister(i64* nonnull align 8 dereferenceable(8) %15, i64 %19)
  %20 = load i64*, i64** %vsp.addr, align 8
  call void @_Z3ADDIyEvRm(i64* nonnull align 8 dereferenceable(8) %20)
  %21 = load i64*, i64** %vsp.addr, align 8
  %22 = load %struct.VirtualRegister*, %struct.VirtualRegister** %vmregs.addr, align 8
  %arrayidx8 = getelementptr inbounds %struct.VirtualRegister, %struct.VirtualRegister* %22, i64 2
  call void @_Z9POP_VMREGILm64ELm0EEvRmR15VirtualRegister(i64* nonnull align 8 dereferenceable(8) %21, %struct.VirtualRegister* nonnull align 1 dereferenceable(8) %arrayidx8)
  %23 = load i64*, i64** %vsp.addr, align 8
  %24 = load %struct.VirtualRegister*, %struct.VirtualRegister** %vmregs.addr, align 8
  %arrayidx9 = getelementptr inbounds %struct.VirtualRegister, %struct.VirtualRegister* %24, i64 3
  call void @_Z9POP_VMREGILm64ELm0EEvRmR15VirtualRegister(i64* nonnull align 8 dereferenceable(8) %23, %struct.VirtualRegister* nonnull align 1 dereferenceable(8) %arrayidx9)
  %25 = load i64*, i64** %vsp.addr, align 8
  %26 = load %struct.VirtualRegister*, %struct.VirtualRegister** %vmregs.addr, align 8
  %arrayidx11 = getelementptr inbounds %struct.VirtualRegister, %struct.VirtualRegister* %26, i64 3
  %27 = bitcast %struct.VirtualRegister* %agg.tmp10 to i8*
  %28 = bitcast %struct.VirtualRegister* %arrayidx11 to i8*
  call void @llvm.memcpy.p0i8.p0i8.i64(i8* align 1 %27, i8* align 1 %28, i64 8, i1 false)
  %coerce.dive12 = getelementptr inbounds %struct.VirtualRegister, %struct.VirtualRegister* %agg.tmp10, i32 0, i32 0
  %coerce.dive13 = getelementptr inbounds %union.anon, %union.anon* %coerce.dive12, i32 0, i32 0
  %29 = load i64, i64* %coerce.dive13, align 1
  call void @_Z10PUSH_VMREGILm64ELm0EEvRm15VirtualRegister(i64* nonnull align 8 dereferenceable(8) %25, i64 %29)
  %30 = load i64*, i64** %vsp.addr, align 8
  %31 = load i64*, i64** %rax.addr, align 8
  call void @_Z7POP_REGRmS_(i64* nonnull align 8 dereferenceable(8) %30, i64* nonnull align 8 dereferenceable(8) %31)
  %32 = load i64*, i64** %vip.addr, align 8
  %33 = load i64, i64* %32, align 8
  ret i64 %33
}
```

The LLVM-IR compiled from the previous C++ `HelperStub` function.

```llvm
define dso_local i64 @SimpleExample_HelperStub(i64* noalias nocapture nonnull align 8 dereferenceable(8) %rax, i64* noalias nocapture nonnull readonly align 8 dereferenceable(8) %rbx, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %rcx, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %rdx, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %rsi, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %rdi, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %rbp, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %rsp, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r8, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r9, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r10, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r11, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r12, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r13, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r14, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r15, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %flags, i64 %KEY_STUB, i64 %RET_ADDR, i64 %REL_ADDR, i64* noalias nonnull align 8 dereferenceable(8) %vsp, i64* noalias nocapture nonnull readonly align 8 dereferenceable(8) %vip, %struct.VirtualRegister* noalias nocapture %vmregs, i64* noalias nocapture readnone %slots) local_unnamed_addr {
entry:
  %0 = load i64, i64* %rax, align 8
  %1 = load i64, i64* %vsp, align 8
  %sub.i.i = add i64 %1, -8
  %arrayidx.i.i = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %sub.i.i
  %value.addr.0.arrayidx.sroa_cast.i.i = bitcast i8* %arrayidx.i.i to i64*
  %2 = load i64, i64* %rbx, align 8
  %sub.i.i66 = add i64 %1, -16
  %arrayidx.i.i67 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %sub.i.i66
  %value.addr.0.arrayidx.sroa_cast.i.i68 = bitcast i8* %arrayidx.i.i67 to i64*
  %qword.i62 = getelementptr inbounds %struct.VirtualRegister, %struct.VirtualRegister* %vmregs, i64 1, i32 0, i32 0
  store i64 %2, i64* %qword.i62, align 1
  %3 = load i64, i64* @__undef, align 8
  %qword.i55 = getelementptr inbounds %struct.VirtualRegister, %struct.VirtualRegister* %vmregs, i64 0, i32 0, i32 0
  store i64 %0, i64* %qword.i55, align 1
  %add.i32.i = add i64 %2, %0
  %cmp.i.i.i.i = icmp ult i64 %add.i32.i, %2
  %cmp1.i.i.i.i = icmp ult i64 %add.i32.i, %0
  %4 = or i1 %cmp.i.i.i.i, %cmp1.i.i.i.i
  %conv.i.i.i.i = trunc i64 %add.i32.i to i32
  %conv.i.i.i.i.i = and i32 %conv.i.i.i.i, 255
  %5 = tail call i32 @llvm.ctpop.i32(i32 %conv.i.i.i.i.i)
  %xor.i.i28.i.i = xor i64 %2, %0
  %xor1.i.i.i.i = xor i64 %xor.i.i28.i.i, %add.i32.i
  %and.i.i.i.i = and i64 %xor1.i.i.i.i, 16
  %cmp.i.i27.i.i = icmp eq i64 %add.i32.i, 0
  %shr.i.i.i.i = lshr i64 %2, 63
  %shr1.i.i.i.i = lshr i64 %0, 63
  %shr2.i.i.i.i = lshr i64 %add.i32.i, 63
  %xor.i.i.i.i = xor i64 %shr2.i.i.i.i, %shr.i.i.i.i
  %xor3.i.i.i.i = xor i64 %shr2.i.i.i.i, %shr1.i.i.i.i
  %add.i.i.i.i = add nuw nsw i64 %xor.i.i.i.i, %xor3.i.i.i.i
  %cmp.i.i25.i.i = icmp eq i64 %add.i.i.i.i, 2
  %conv.i.i.i = zext i1 %4 to i64
  %6 = shl nuw nsw i32 %5, 2
  %7 = and i32 %6, 4
  %8 = xor i32 %7, 4
  %9 = zext i32 %8 to i64
  %shl22.i.i.i = select i1 %cmp.i.i27.i.i, i64 64, i64 0
  %10 = lshr i64 %add.i32.i, 56
  %11 = and i64 %10, 128
  %shl34.i.i.i = select i1 %cmp.i.i25.i.i, i64 2048, i64 0
  %or6.i.i.i = or i64 %11, %shl22.i.i.i
  %and13.i.i.i = or i64 %or6.i.i.i, %and.i.i.i.i
  %or17.i.i.i = or i64 %and13.i.i.i, %conv.i.i.i
  %and25.i.i.i = or i64 %or17.i.i.i, %shl34.i.i.i
  %or29.i.i.i = or i64 %and25.i.i.i, %9
  %qword.i36 = getelementptr inbounds %struct.VirtualRegister, %struct.VirtualRegister* %vmregs, i64 2, i32 0, i32 0
  store i64 %or29.i.i.i, i64* %qword.i36, align 1
  store i64 %3, i64* %value.addr.0.arrayidx.sroa_cast.i.i68, align 1
  %qword.i = getelementptr inbounds %struct.VirtualRegister, %struct.VirtualRegister* %vmregs, i64 3, i32 0, i32 0
  store i64 %add.i32.i, i64* %qword.i, align 1
  store i64 %3, i64* %value.addr.0.arrayidx.sroa_cast.i.i, align 1
  store i64 %add.i32.i, i64* %rax, align 8
  %12 = load i64, i64* %vip, align 8
  ret i64 %12
}
```

The LLVM-IR of the `HelperStub` function with inlined and optimized calls to the handlers

The last snippet is representing all the semantic computations related with a `VmBlock`, as described in the high level overview. Although, if the code we lifted is capturing the whole semantics related with a `VmStub`, we can wrap the `HelperStub` function with the `HelperFunction` function, which enforces the liveness properties described in the *Liveness and aliasing information* section, enabling us to obtain only the computations updating the host execution context:

```cpp
extern "C" size_t SimpleExample_HelperFunction(
    rptr rax, rptr rbx, rptr rcx,
    rptr rdx, rptr rsi, rptr rdi,
    rptr rbp, rptr rsp, rptr r8,
    rptr r9, rptr r10, rptr r11,
    rptr r12, rptr r13, rptr r14,
    rptr r15, rptr flags, size_t KEY_STUB,
    size_t RET_ADDR, size_t REL_ADDR) {
  // Allocate the temporary virtual registers
  VirtualRegister vmregs[30] = {0};
  // Allocate the temporary passing slots
  size_t slots[30] = {0};
  // Initialize the virtual registers
  size_t vsp = rsp;
  size_t vip = 0;
  // Force the relocation address to 0
  REL_ADDR = 0;
  // Execute the virtualized code
  vip = SimpleExample_HelperStub(
    rax, rbx, rcx, rdx, rsi, rdi,
    rbp, rsp, r8, r9, r10, r11,
    r12, r13, r14, r15, flags,
    KEY_STUB, RET_ADDR, REL_ADDR,
    vsp, vip, vmregs, slots);
  // Return the next address(es)
  return vip;
}
```

The C++ `HelperFunction` function with the call to the `HelperStub` function and the relevant stack frame allocations.

```llvm
define dso_local i64 @SimpleExample_HelperFunction(i64* noalias nocapture nonnull align 8 dereferenceable(8) %rax, i64* noalias nocapture nonnull readonly align 8 dereferenceable(8) %rbx, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %rcx, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %rdx, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %rsi, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %rdi, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %rbp, i64* noalias nocapture nonnull readonly align 8 dereferenceable(8) %rsp, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r8, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r9, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r10, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r11, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r12, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r13, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r14, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %r15, i64* noalias nocapture nonnull align 8 dereferenceable(8) %flags, i64 %KEY_STUB, i64 %RET_ADDR, i64 %REL_ADDR) local_unnamed_addr {
entry:
  %0 = load i64, i64* %rax, align 8
  %1 = load i64, i64* %rbx, align 8
  %add.i32.i.i = add i64 %1, %0
  store i64 %add.i32.i.i, i64* %rax, align 8
  ret i64 0
}
```

The LLVM-IR `HelperFunction` function with fully optimized code.

It can be seen that the example is just pushing the values of the registers `rax` and `rbx`, loading them in `vmregs[0]` and `vmregs[1]` respectively, pushing the `VmRegisters` on the stack, adding them together, popping the updated flags in `vmregs[2]`, popping the addition’s result to `vmregs[3]` and finally pushing `vmregs[3]` on the stack to be popped in the `rax` register at the end. The liveness of the values of the `VmRegisters` ends with the end of the function, hence the updated flags saved in `vmregs[2]` won’t be reflected on the host execution context. Looking at the final snippet we can see that the semantics of the code have been successfully obtained.

## Exploration
{: #exploration }

### Poor man’s slicer
{: #poor-mans-slicer }

Slicing a symbolic expression to be able to evaluate it, throw it at an SMT solver or match it against some pattern is something extremely common in all symbolic reasoning tools. Luckily for us this capability is trivial to implement with yet another C++ helper function. This technique has been referred to as *Poor man’s slicer* in the SATURN paper, hence the title of the section.

In the VMProtect context we are mainly interested in slicing one expression: the next program counter. We want to do that either while exploring the single `VmBlocks` (that, once connected, form a `VmStub`) or while exploring the `VmStubs` (that, once connected, form a `VmFunction`). The following C++ code is meant to keep only the computations related to the final value of the virtual instruction pointer at the end of a `VmBlock` or `VmStub`:

```cpp
extern "C"
size_t HelperSlicePC(
  size_t rax, size_t rbx, size_t rcx, size_t rdx, size_t rsi,
  size_t rdi, size_t rbp, size_t rsp, size_t r8, size_t r9,
  size_t r10, size_t r11, size_t r12, size_t r13, size_t r14,
  size_t r15, size_t flags,
  size_t KEY_STUB, size_t RET_ADDR, size_t REL_ADDR)
{
  // Allocate the temporary virtual registers
  VirtualRegister vmregs[30] = {0};
  // Allocate the temporary passing slots
  size_t slots[30] = {0};
  // Initialize the virtual registers
  size_t vsp = rsp;
  size_t vip = 0;
  // Force the relocation address to 0
  REL_ADDR = 0;
  // Execute the virtualized code
  vip = HelperStub(
    rax, rbx, rcx, rdx, rsi, rdi,
    rbp, rsp, r8, r9, r10, r11,
    r12, r13, r14, r15, flags,
    KEY_STUB, RET_ADDR, REL_ADDR,
    vsp, vip, vmregs, slots);
  // Return the sliced program counter
  return vip;
}
```

The acute observer will notice that the function definition is basically identical to the `HelperFunction` definition given before, with the fundamental difference that the arguments are passed by value and therefore useful if related to the computation of the sliced expression, but with their liveness scope ending at the end of the function, which guarantees that there won’t be store operations to the host context that could possibly bloat the code.

The steps to use the above helper function are:

- The `HelperSlicePC` is cloned into a new throwaway function;
- The call to the `HelperStub` function is swapped with a call to the `VmBlock` or `VmStub` of which we want to slice the final instruction pointer;
- The called function is forcefully inlined into the `HelperSlicePC` function;
- The optimization pipeline is executed on the cloned `HelperSlicePC` function resulting in the slicing of the final instruction pointer expression as a side-effect of the optimizations.

The following LLVM-IR snippet shows the idea in action, resulting in the final optimized function where the condition and edges of the conditional branch are clearly visible.

```llvm
define dso_local i64 @HelperSlicePC(i64 %rax, i64 %rbx, i64 %rcx, i64 %rdx, i64 %rsi, i64 %rdi, i64 %rbp, i64 %rsp, i64 %r8, i64 %r9, i64 %r10, i64 %r11, i64 %r12, i64 %r13, i64 %r14, i64 %r15, i64 %flags, i64 %KEY_STUB, i64 %RET_ADDR, i64 %REL_ADDR) #5 {
  %1 = call { i32, i32, i32, i32 } asm "  xchgq  %rbx,${1:q}\0A  cpuid\0A  xchgq  %rbx,${1:q}", "={ax},=r,={cx},={dx},0,~{dirflag},~{fpsr},~{flags}"(i32 1) #4, !srcloc !9
  %2 = extractvalue { i32, i32, i32, i32 } %1, 0
  %3 = and i32 %2, 4080
  %4 = icmp eq i32 %3, 4064
  %5 = select i1 %4, i64 5371013457, i64 5371013227
  ret i64 %5
}
```

In the following section we’ll see how variations of this technique are used to explore the virtualized control flow graph, solve the conditional branches, and recover the switch cases.

### Exploring the virtual control flow graph
{: #virtual-control-flow-graph }

The exploration of a virtualized control flow graph can be done in different ways and usually protectors like VMProtect or Themida show a distinctive shape that can be pattern-matched with ease, simplified and parsed to obtain the outgoing edges of a conditional block.

The logic used by different VMProtect conditional jump versions has been detailed in the past, so in this section we are going to delve into an SMT-driven algorithm based on the incremental construction of the explored control flow graph and specifically built on top of the slicing logic explained in the previous section.

Given the generic nature of the detailed algorithm, nothing stops it from being used on other protectors. The usual catch is obviously caused by protections embedding hard to solve constraints that may hinder the automated solving phase, but the construction and propagation of the partial CFG constraints and expressions could still be useful in practice to pull out less automated exploration algorithms, or to identify and simplify anti-dynamic symbolic execution tricks (e.g. dummy loops leading to path explosion that could be simplified by LLVM’s loop optimization passes or custom user passes).

#### Partial CFG
{: #partial-cfg }

A partial control flow graph is a control flow graph built connecting the currently explored basic blocks given the known edges between them. The idea behind building it, is that each time that we explore a new basic block, we gather new outgoing edges that could lead to new unexplored basic blocks, or even to known basic blocks. Every new edge between two blocks is therefore adding information to the entire control flow graph and we could actually propagate new useful constraints and values to enable stronger optimizations, possibly easing the solving of the conditional branches or even changing a known branch from unconditional to conditional.

Let’s look at two motivating examples of why building a partial CFG may be a good idea to be able to replicate the kind of reasoning usually implemented by symbolic execution tools, with the addition of useful built-in LLVM passes.

##### Motivating example #1
{: #motivating-example-1 }

Consider the following partial control flow graph, where blue represents the `VmBlock` that has just been processed, orange the unprocessed `VmBlock` and purple the `VmBlock` of interest for the example.

[![Motivating example 1](/assets/images/vmprotect/diagram1.svg)](/assets/images/vmprotect/diagram1.svg)

Let’s assume we just solved the outgoing edges for the basic block `A`, obtaining two connections leading to the new basic blocks `B` and `C`. Now assume that we sliced the branch condition of the sole basic block `B`, obtaining an access into a constant array with a 64 bits symbolic index. Enumerating all the valid indices may be a non-trivial task, so we may want to restrict the search using known constraints on the symbolic index that, if present, are most likely going to come from the chain(s) of predecessor(s) of the basic block `B`.

To draw a symbolic execution parallel, this is the case where we want to collect the path constraints from a certain number of predecessors (e.g. we may want to incrementally harvest the constraints, because sometimes the needed constraint is locally near to the basic block we are solving) and chain them to be fed to an SMT solver to execute a successful enumeration of the valid indices.

Tools like [Souper](https://github.com/google/souper) automatically harvest the set of path constraints while slicing an expression, so building the partial control flow graph and feeding it to Souper may be sufficient for the task. Additionally, with the LLVM API to walk the predecessors of a basic block it’s also quite easy to obtain the set of needed constraints and, when available, we may also take advantage of known-to-be-true conditions provided by the [llvm.assume](https://llvm.org/docs/LangRef.html#llvm-assume-intrinsic) intrinsic.

##### Motivating example #2
{: #motivating-example-2 }

Consider the following partial control flow graph, where blue represents the `VmBlock` that has just been processed, orange the unprocessed `VmBlocks`, purple the `VmBlock` of interest for the example, dashed red arrows the edges of interest for the example and the solid green arrow an edge that has just been processed.

[![Motivating example 2](/assets/images/vmprotect/diagram0.svg)](/assets/images/vmprotect/diagram0.svg)

Let’s assume we just solved the outgoing edges for the basic block `E`, obtaining two connections leading to a new block `G` and a known block `B`. In this case we know that we detected a jump to the previously visited block `B` (edge in green), which is basically forming a loop chain (`B` → `C` → `E` → `B`) and we know that starting from `B` we can reach two edges (`B` → `C` and `D` → `F`, marked in dashed red) that are currently known as unconditional, but that, given the newly obtained edge `E` → `B`, may not be anymore and therefore will need to be proved again. Building a new partial control flow graph including all the newly discovered basic block connections and slicing the branch of the blocks `B` and `D` may now show them as conditional.

As a real world case, when dealing with concolic execution approaches, the one mentioned above is the usual pattern that arises with index-based loops, starting with a known concrete index and running till the index reaches an upper or lower bound `N`. During the first `N-1` executions the tool would take the same path and only at the iteration `N` the other path would be explored. That’s the reason why concolic and symbolic execution tools attempt to build heuristics or use techniques like state-merging to avoid running into [path explosion](https://en.wikipedia.org/wiki/Symbolic_execution#Path_explosion) issues (or at best executing the loop `N` times).

Building the partial CFG with LLVM instead, would mark the loop back edge as unconditional the first time, but building it again, including the knowledge of the newly discovered back edge, would immediately reveal the loop pattern. The outcome is that LLVM would now be able to apply its loop analysis passes, the user would be able to use the API to build ad-hoc [LoopPass](https://llvm.org/docs/WritingAnLLVMPass.html#the-looppass-class) passes to handle special obfuscation applied to the loop components (e.g. encoded loop variant/invariant) or the SMT solvers would be able to treat newly created [Phi](https://en.wikipedia.org/wiki/Static_single_assignment_form) nodes at the merge points as symbolic variables.

The following LLVM-IR snippet shows the sliced partial control flow graphs obtained during the exploration of the virtualized assembly snippet presented below.

```nasm
start:
  mov rax, 2000
  mov rbx, 0
loop:
  inc rbx
  cmp rbx, rax
  jne loop
```

The original assembly snippet.

```llvm
define dso_local i64 @FirstSlice(i64 %rax, i64 %rbx, i64 %rcx, i64 %rdx, i64 %rsi, i64 %rdi, i64 %rbp, i64 %rsp, i64 %r8, i64 %r9, i64 %r10, i64 %r11, i64 %r12, i64 %r13, i64 %r14, i64 %r15, i64 %flags, i64 %KEY_STUB, i64 %RET_ADDR, i64 %REL_ADDR) {
  %1 = call i64 @HelperKeepPC(i64 5369464257)
  ret i64 %1
}
```

The first partial CFG obtained during the exploration phase

```llvm
define dso_local i64 @SecondSlice(i64 %rax, i64 %rbx, i64 %rcx, i64 %rdx, i64 %rsi, i64 %rdi, i64 %rbp, i64 %rsp, i64 %r8, i64 %r9, i64 %r10, i64 %r11, i64 %r12, i64 %r13, i64 %r14, i64 %r15, i64 %flags, i64 %KEY_STUB, i64 %RET_ADDR, i64 %REL_ADDR) {
  br label %1

1:                                                ; preds = %1, %0
  %2 = phi i64 [ 0, %0 ], [ %3, %1 ]
  %3 = add i64 %2, 1
  %4 = icmp eq i64 %2, 1999
  %5 = select i1 %4, i64 5369183207, i64 5369464257
  %6 = call i64 @HelperKeepPC(i64 %5)
  %7 = icmp eq i64 %6, 5369464257
  br i1 %7, label %1, label %8

8:                                                ; preds = %1
  ret i64 233496237
}
```

The second partial CFG obtained during the exploration phase. The block `8` is returning the dummy `0xdeaddead` (`233496237`) value, meaning that the `VmBlock` instructions haven’t been lifted yet.

```llvm
define dso_local i64 @F_0x14000101f_NoLoopOpt(i64* noalias nonnull align 8 dereferenceable(8) %rax, i64* noalias nonnull align 8 dereferenceable(8) %rbx, i64* noalias nonnull align 8 dereferenceable(8) %rcx, i64* noalias nonnull align 8 dereferenceable(8) %rdx, i64* noalias nonnull align 8 dereferenceable(8) %rsi, i64* noalias nonnull align 8 dereferenceable(8) %rdi, i64* noalias nonnull align 8 dereferenceable(8) %rbp, i64* noalias nonnull align 8 dereferenceable(8) %rsp, i64* noalias nonnull align 8 dereferenceable(8) %r8, i64* noalias nonnull align 8 dereferenceable(8) %r9, i64* noalias nonnull align 8 dereferenceable(8) %r10, i64* noalias nonnull align 8 dereferenceable(8) %r11, i64* noalias nonnull align 8 dereferenceable(8) %r12, i64* noalias nonnull align 8 dereferenceable(8) %r13, i64* noalias nonnull align 8 dereferenceable(8) %r14, i64* noalias nonnull align 8 dereferenceable(8) %r15, i64* noalias nonnull align 8 dereferenceable(8) %flags, i64 %KEY_STUB, i64 %RET_ADDR, i64 %REL_ADDR) {
  br label %1

1:                                                ; preds = %1, %0
  %2 = phi i64 [ 0, %0 ], [ %3, %1 ]
  %3 = add i64 %2, 1
  %4 = icmp eq i64 %2, 1999
  br i1 %4, label %5, label %1

5:                                                ; preds = %1
  store i64 %3, i64* %rbx, align 8
  store i64 2000, i64* %rax, align 8
  ret i64 5368713281
}
```

The final CFG obtained at the completion of the exploration phase.

```llvm
define dso_local i64 @F_0x14000101f_WithLoopOpt(i64* noalias nonnull align 8 dereferenceable(8) %rax, i64* noalias nonnull align 8 dereferenceable(8) %rbx, i64* noalias nonnull align 8 dereferenceable(8) %rcx, i64* noalias nonnull align 8 dereferenceable(8) %rdx, i64* noalias nonnull align 8 dereferenceable(8) %rsi, i64* noalias nonnull align 8 dereferenceable(8) %rdi, i64* noalias nonnull align 8 dereferenceable(8) %rbp, i64* noalias nonnull align 8 dereferenceable(8) %rsp, i64* noalias nonnull align 8 dereferenceable(8) %r8, i64* noalias nonnull align 8 dereferenceable(8) %r9, i64* noalias nonnull align 8 dereferenceable(8) %r10, i64* noalias nonnull align 8 dereferenceable(8) %r11, i64* noalias nonnull align 8 dereferenceable(8) %r12, i64* noalias nonnull align 8 dereferenceable(8) %r13, i64* noalias nonnull align 8 dereferenceable(8) %r14, i64* noalias nonnull align 8 dereferenceable(8) %r15, i64* noalias nonnull align 8 dereferenceable(8) %flags, i64 %KEY_STUB, i64 %RET_ADDR, i64 %REL_ADDR) {
  store i64 2000, i64* %rbx, align 8
  store i64 2000, i64* %rax, align 8
  ret i64 5368713281
}
```

The loop-optimized final CFG obtained at the completion of the exploration phase.

The `FirstSlice` function shows that a single unconditional branch has been detected, identifying the bytecode address **0x1400B85C1** (**5369464257**), this is because there’s no knowledge of the back edge and the comparison would be **cmp 1, 2000**. The `SecondSlice` function instead shows that a conditional branch has been detected selecting between the bytecode addresses **0x140073BE7** (**5369183207**) and **0x1400B85C1** (**5369464257**). The comparison is now done with a symbolic [PHINode](https://llvm.org/doxygen/classllvm_1_1PHINode.html). The `F_0x14000101f_WithLoopOpt` and `F_0x14000101f_NoLoopOpt` functions show the fully devirtualized code with and without loop optimizations applied.

#### Pseudocode
{: #pseudocode }

Given the knowledge obtained from the motivating examples, the pseudocode for the automated partial CFG driven exploration is the following:

1. We initialize the algorithm creating:

   - A `stack` of addresses of `VmBlocks` to explore, referred to as `Worklist`;
   - A `set` of addresses of explored `VmBlocks`, referred to as `Explored`;
   - A `set` of addresses of `VmBlocks` to reprove, referred to as `Reprove`;
   - A `map` of known edges between the `VmBlocks`, referred to as `Edges`.
2. We push the address of the entry `VmBlock` into the `Worklist`;
3. We fetch the address of a `VmBlock` to explore, we lift it to LLVM-IR if met for the first time, we build the partial CFG using the knowledge from the `Edges` map and we slice the branch condition of the current `VmBlock`. Finally we feed the branch condition to Souper, which will process the expression harvesting the needed constraints and converting it to an SMT query. We can then send the query to an SMT solver, asking for the valid solutions, incrementally rejecting the known solutions up to some limit (worst case) or till all the solutions have been found.
4. Once we obtained the outgoing edges for the current `VmBlock`, we can proceed with updating the maps and sets:

   - We verify if each solved edge is leading to a known `VmBlock`; if it is, we verify if this connection was previously known. If unknown, it means we found a new predecessor for a known `VmBlock` and we proceed with adding the addresses of all the `VmBlocks` reachable by the known `VmBlock` to the `Reprove` set and removing them from the `Explored` set; to speed things up, we can eventually skip each `VmBlock` known to be firmly unconditional;
   - We update the `Edges` map with the newly solved edges.
5. At this point we check if the `Worklist` is empty. If it isn’t, we jump back to step **3**. If it is, we populate it with all the addresses in the `Reprove` set, clearing it in the process and jumping back to step **3**. If also the `Reprove` set is empty, it means we explored the whole CFG and eventually reproved all the `VmBlocks` that obtained new predecessors during the exploration phase.

As mentioned at the start of the section, there are many ways to explore a virtualized CFG and using an SMT-driven solution may generalize most of the steps. Obviously, it brings its own set of issues (e.g. hard to solve constraints), so one could eventually fall back to the pattern matching based solution at need. As expected, the pattern matching based solution would also blindly explore unreachable paths at times, so a mixed solution could really offer the best CFG coverage.

The pseudocode presented in this section is a simplified version of the partial CFG based exploration algorithm used by SATURN at this point in time, streamlined from a set of reasonings that are unnecessary while exploring a CFG virtualized by VMProtect.

## Optimization pipeline
{: #pipeline }

So far we hinted at the underlying usage of LLVM’s optimization and analysis passes multiple times through the sections, so we can finally take a look at: how they fit in, their configuration and their limitations.

### Managing the pipeline
{: #managing-the-pipeline }

Running the whole `-O3` pipeline may not always be the best idea, because we may want to use only a subset of passes, instead of wasting cycles on passes that we know a priori don’t have any effect on the lifted LLVM-IR code. Additionally, by default, LLVM is providing a chain of optimizations which is executed once, is meant to optimize non-obfuscated code and should be as efficient as possible.

Although, in our case, we have different needs and want to be able to:

- Add some custom passes to tackle context-specific problems and do so at precise points in the pipeline to obtain the best possible output, while avoiding [phase ordering](https://ieeexplore.ieee.org/document/1611550) issues;
- Iterate the optimization pipeline more than once, ideally until our custom passes can’t apply any more changes to the IR code;
- Be able to pass custom flags to the pipeline to toggle some passes at will and eventually feed them with information obtained from the binary (e.g. access to the binary sections).

LLVM provides a [FunctionPassManager](https://llvm.org/doxygen/classllvm_1_1legacy_1_1FunctionPassManager.html) class to craft our own pipeline, using LLVM’s passes and custom passes. The following C++ snippet shows how we can add a mix of passes that will be executed in order until there won’t be any more changes or until a threshold will be reached:

```cpp
void optimizeFunction(llvm::Function *F, OptimizationGuide &G) {
  // Fetch the Module
  auto *M = F->getParent();
  // Create the function pass manager
  llvm::legacy::FunctionPassManager FPM(M);
  // Initialize the pipeline
  llvm::PassManagerBuilder PMB;
  PMB.OptLevel = 3;
  PMB.SizeLevel = 2;
  PMB.RerollLoops = false;
  PMB.SLPVectorize = false;
  PMB.LoopVectorize = false;
  PMB.Inliner = createFunctionInliningPass();
  // Add the alias analysis passes
  FPM.add(createCFLSteensAAWrapperPass());
  FPM.add(createCFLAndersAAWrapperPass());
  FPM.add(createTypeBasedAAWrapperPass());
  FPM.add(createScopedNoAliasAAWrapperPass());
  // Add some useful LLVM passes
  FPM.add(createCFGSimplificationPass());
  FPM.add(createSROAPass());
  FPM.add(createEarlyCSEPass());
  // Add a custom pass here
  if (G.RunCustomPass1)
    FPM.add(createCustomPass1(G));
  FPM.add(createInstructionCombiningPass());
  FPM.add(createCFGSimplificationPass());
  // Add a custom pass here
  if (G.RunCustomPass2)
    FPM.add(createCustomPass2(G));
  FPM.add(createGVNHoistPass());
  FPM.add(createGVNSinkPass());
  FPM.add(createDeadStoreEliminationPass());
  FPM.add(createInstructionCombiningPass());
  FPM.add(createCFGSimplificationPass());
  // Execute the pipeline
  size_t minInsCount = F->getInstructionCount();
  size_t pipExeCount = 0;
  FPM.doInitialization();
  do {
    // Reset the IR changed flag
    G.HasChanged = false;
    // Run the optimizations
    FPM.run(*F);
    // Check if the function changed
    size_t curInsCount = F->getInstructionCount();
    if (curInsCount < minInsCount) {
      minInsCount = curInsCount;
      G.HasChanged |= true;
    }
    // Increment the execution count
    pipExeCount++;
  } while (G.HasChanged && pipExeCount < 5);
  FPM.doFinalization();
}
```

The `OptimizationGuide` structure can be used to pass information to the custom passes and control the execution of the pipeline.

### Configuration
{: #configuration }

As previously stated, the LLVM default pipeline is meant to be as efficient as possible, therefore it’s configured with a tradeoff between efficiency and efficacy in mind. While devirtualizing big functions it’s not uncommon to see the effects of the stricter configurations employed by default. But an [example](https://godbolt.org/z/7Throjsa5) is worth a thousand words.

In the Godbolt UI we can see on the left a snippet of LLVM-IR code that is storing **i32** values at increasing indices of a global array named **arr**. The `store` at line 96, writing the value **91** at **arr[1]**, is a bit special because it is fully overwriting the `store` at line 6, writing the value **1** at **arr[1]**. If we look at the upper right result, we see that the `DSE` pass was applied, but somehow it didn’t do its job of removing the dead `store` at line 6. If we look at the bottom right result instead, we see that the `DSE` pass managed to achieve its goal and successfully killed the dead `store` at line 6. The reason for the difference is entirely associated to a conservative configuration of the `DSE` pass, which by default (at the time of writing), is walking up to 90 [MemorySSA](https://llvm.org/docs/MemorySSA.html) definitions before deciding that a **store** is not killing another post-dominated `store`. Setting the [MemorySSAUpwardsStepLimit](https://llvm.org/doxygen/DeadStoreElimination_8cpp.html#aa9d7ec729de7347f5dedc03860919917) to a higher value (e.g. 100 in the example) is definitely something that we want to do while deobfuscating some code.

Each pass that we are going to add to the custom pipeline is going to have configurations that may be giving suboptimal deobfuscation results, so it’s a good idea to check their C++ implementation and figure out if tweaking some of the options may improve the output.

### Limitations
{: #limitations }

When tweaking some configurations is not giving the expected results, we may have to dig deeper into the implementation of a pass to understand if something is hindering its job, or roll up our sleeves and [develop](https://llvm.org/docs/WritingAnLLVMPass.html) a custom LLVM pass. Some examples on why digging into a pass implementation may lead to fruitful improvements follow.

#### IsGuaranteedLoopInvariant (DSE, MSSA)
{: #isguaranteedloopinvariant-dse-mssa }

While looking at some devirtualized code, I noticed some clearly-dead stores that weren’t removed by the `DSE` pass, even though the tweaked configurations were enabled. A minimal example of the problem, its explanation and solution are provided in the following diffs: [D96979](https://reviews.llvm.org/D96979), [D97155](https://reviews.llvm.org/D97155). The bottom line is that the `IsGuarenteedLoopInvariant` function used by the `DSE` and `MSSA` passes was not using the safe assumption that a pointer computed in the entry block is, by design, guaranteed to be loop invariant as the entry block of a [Function](https://llvm.org/doxygen/classllvm_1_1Function.html) is guaranteed to have no predecessors and not to be part of a loop.

#### GetPointerBaseWithConstantOffset (DSE)
{: #getpointerbasewithconstantoffset-dse }

While looking at some devirtualized code that was accessing memory slots of different sizes, I noticed some clearly-dead stores that weren’t removed by the `DSE` pass, even though the tweaked configurations were enabled. A minimal example of the problem, its explanation and solution are provided in the following diff: [D97676](https://reviews.llvm.org/D97676). The bottom line is that while computing the partially overlapping memory stores, the `DSE` was considering only memory slots with the same base address, ignoring fully overlapping stores offsetted between each other. The solution is making use of another patch which is providing information about the offsets of the memory slots: [D93529](https://reviews.llvm.org/D93529).

#### Shift-Select folding (InstCombine)
{: #shift-select-folding-instcombine }

And obviously there is no two without three! Nah, just kidding, a patch I wanted to get accepted to simplify one of the recurring patterns in the computation of the VMProtect conditional branches has been on pause because [InstCombine](https://llvm.org/doxygen/InstructionCombining_8cpp.html) is an extremely complex piece of code and additions to it, especially if related to uncommon patterns, are unwelcome and seen as possibly bloating and slowing down the entire pipeline. Additional information on the pattern and the reasons that hinder its folding are available in the following differential: [D84664](https://reviews.llvm.org/D84664). Nothing stops us from maintaining our own version of `InstCombine` as a custom pass, with ad-hoc patterns specifically selected for the obfuscation under analysis.

## Custom optimization passes
{: #custom-passes }

This section will give an overview of some custom passes meant to:

- Solve VMProtect specific optimization problems;
- Solve some limitations of existing LLVM passes, but that won’t meet the same quality standard of an official LLVM pass.

### SegmentsAA
{: #segmentsaa }

This pass falls under the category of the VMProtect specific optimization problems and is probably the most delicate of the section, as it may be feeding LLVM with unsafe assumptions. The aliasing information described in the *Liveness and aliasing information* section will finally come in handy. In fact, the goal of the pass is to identify the type of two pointers and determine if they can be deemed as not aliasing with one another.

With the structures defined in the previous sections, LLVM is already able to infer that two pointers derived from the following sources don’t alias with one another:

- general purpose registers
- `VmRegisters`
- `VmPassingSlots`
- `GS` zero-sized array
- `FS` zero-sized array
- `RAM` zero-sized array (with constant index)
- `RAM` zero-sized array (with symbolic index)

Additionally LLVM can also discern between pointers with `RAM` base using a simple symbolic index. For example an access to `[rsp - 0x10]` (local stack slot) will be considered as `NoAlias` when compared with an access to `[rsp + 0x10]` (incoming stack argument).

But LLVM’s alias analysis passes fall short when handling pointers using as base the `RAM` array and employing a more convoluted symbolic index, and the reason for the shortcoming is entirely related to the lack of type and context information that got lost during the compilation to binary.

The pass is inspired by existing implementations ([1](https://blog.tartanllama.xyz/llvm-alias-analysis/), [2](https://llvm.org/doxygen/AMDGPUAliasAnalysis_8h.html), [3](https://github.com/zneak/fcd/blob/master/fcd/pass_regaa.cpp)) that are basing their checks on the identification of pointers belonging to different segments and address spaces.

Slicing the symbolic index used in a `RAM` array access we can discern with high confidence between the following additional `NoAlias` memory accesses:

- **indirect access**: if the access is a stack argument (`[rsp]` or `[rsp + positive_constant_offset + symbolic_offset]`), a dereferenced general purpose register (`[rax]`) or a nested dereference (`val1 = [rax]`, `val2 = [val1]`); identified as `TyIND` in the code;
- **local stack slot**: if the access is of the form `[rsp - positive_constant_offset + symbolic_offset]`; identified as `TySS` in the code;
- **local stack array**: if the access if of the form `[rsp - positive_constant_offset + phi_index]`; identified as `TyARR` in the code.

If the pointer type cannot be reliably detected, an unknown type (identified as `TyUNK` in the code) is being used, and the comparison between the pointers is automatically skipped. If the pass cannot return a `NoAlias` result, the query is passed back to the default alias analysis pipeline.

One could argue that the pass is not really needed, as it is unlikely that the propagation of the sensitive information we need to successfully explore the virtualized CFG is hindered by aliasing issues. In fact, the computation of a conditional branch at the end of a `VmBlock` is guaranteed not to be hindered by a symbolic memory store happening before the jump `VmHandler` accesses the branch destination. But there are some cases where VMProtect pushes the address of the next `VmStub` in one of the first `VmBlocks`, doing memory stores in between and accessing the pushed value only in one or more `VmExits`. That could be a case where discerning between a local stack slot and an indirect access enables the propagation of the pushed address.

Irregardless of the aforementioned issue, that can be solved with some ad-hoc `store-to-load` detection logic, playing around with the alias analysis information that can be fed to LLVM could make the devirtualized code more readable. We have to keep in mind that there may be edge cases where the original code is breaking our assumptions, so having at least a vague idea of the involved pointers accessed at runtime could give us more confidence or force us to err on the safe side, relying solely on the built-in LLVM alias analysis passes.

The assembly snippet shown below has been devirtualized with and without adding the `SegmentsAA` pass to the pipeline. If we are sure that at runtime, before the `push rax` instruction, `rcx` doesn’t contain the value `rsp - 8` (extremely unexpected on benign code), we can safely enable the `SegmentsAA` pass and obtain a cleaner output.

```nasm
start:
  push rax
  mov qword ptr [rsp], 1
  mov qword ptr [rcx], 2
  pop rax
```

The assembly code writing to two possibly aliasing memory slots

```llvm
define dso_local i64 @F_0x14000101f(i64* noalias nonnull align 8 dereferenceable(8) %rax, i64* noalias nonnull align 8 dereferenceable(8) %rbx, i64* noalias nonnull align 8 dereferenceable(8) %rcx, i64* noalias nonnull align 8 dereferenceable(8) %rdx, i64* noalias nonnull align 8 dereferenceable(8) %rsi, i64* noalias nonnull align 8 dereferenceable(8) %rdi, i64* noalias nonnull align 8 dereferenceable(8) %rbp, i64* noalias nonnull align 8 dereferenceable(8) %rsp, i64* noalias nonnull align 8 dereferenceable(8) %r8, i64* noalias nonnull align 8 dereferenceable(8) %r9, i64* noalias nonnull align 8 dereferenceable(8) %r10, i64* noalias nonnull align 8 dereferenceable(8) %r11, i64* noalias nonnull align 8 dereferenceable(8) %r12, i64* noalias nonnull align 8 dereferenceable(8) %r13, i64* noalias nonnull align 8 dereferenceable(8) %r14, i64* noalias nonnull align 8 dereferenceable(8) %r15, i64* noalias nonnull align 8 dereferenceable(8) %flags, i64 %KEY_STUB, i64 %RET_ADDR, i64 %REL_ADDR) #2 {
  %1 = load i64, i64* %rcx, align 8, !alias.scope !22, !noalias !27
  %2 = inttoptr i64 %1 to i64*
  store i64 2, i64* %2, align 1, !noalias !65
  store i64 1, i64* %rax, align 8, !tbaa !4, !alias.scope !66, !noalias !67
  ret i64 5368713278
}
```

The devirtualized code with the `SegmentsAA` pass added to the pipeline, with the assumption that `rcx` differs from `rsp - 8`

```llvm
define dso_local i64 @F_0x14000101f(i64* noalias nonnull align 8 dereferenceable(8) %rax, i64* noalias nonnull align 8 dereferenceable(8) %rbx, i64* noalias nonnull align 8 dereferenceable(8) %rcx, i64* noalias nonnull align 8 dereferenceable(8) %rdx, i64* noalias nonnull align 8 dereferenceable(8) %rsi, i64* noalias nonnull align 8 dereferenceable(8) %rdi, i64* noalias nonnull align 8 dereferenceable(8) %rbp, i64* noalias nonnull align 8 dereferenceable(8) %rsp, i64* noalias nonnull align 8 dereferenceable(8) %r8, i64* noalias nonnull align 8 dereferenceable(8) %r9, i64* noalias nonnull align 8 dereferenceable(8) %r10, i64* noalias nonnull align 8 dereferenceable(8) %r11, i64* noalias nonnull align 8 dereferenceable(8) %r12, i64* noalias nonnull align 8 dereferenceable(8) %r13, i64* noalias nonnull align 8 dereferenceable(8) %r14, i64* noalias nonnull align 8 dereferenceable(8) %r15, i64* noalias nonnull align 8 dereferenceable(8) %flags, i64 %KEY_STUB, i64 %RET_ADDR, i64 %REL_ADDR) #2 {
  %1 = load i64, i64* %rsp, align 8, !tbaa !4, !alias.scope !22, !noalias !27
  %2 = add i64 %1, -8
  %3 = load i64, i64* %rcx, align 8, !alias.scope !65, !noalias !66
  %4 = inttoptr i64 %2 to i64*
  store i64 1, i64* %4, align 1, !noalias !67
  %5 = inttoptr i64 %3 to i64*
  store i64 2, i64* %5, align 1, !noalias !67
  %6 = load i64, i64* %4, align 1, !noalias !67
  store i64 %6, i64* %rax, align 8, !tbaa !4, !alias.scope !68, !noalias !69
  ret i64 5368713278
}
```

The devirtualized code without the `SegmentsAA` pass added to the pipeline and therefore no assumptions fed to LLVM

Alias analysis is a complex topic, and experience thought me that most of the propagation issues happening while using LLVM to deobfuscate some code are related to the LLVM’s alias analysis passes being hinder by some pointer computation. Therefore, having the capability to feed LLVM with context-aware information could be the only way to handle certain types of obfuscation. Beware that other tools you are used to are most likely doing similar “safe” assumptions under the hood (e.g. concolic execution tools using the concrete pointer to answer the aliasing queries).

The takeaway from this section is that, if needed, you can define your own alias analysis callback pass to be integrated in the optimization pipeline in such a way that pre-existing passes can make use of the refined aliasing query results. This is similar to updating IDA’s stack variables with proper type definitions to improve the propagation results.

### KnownIndexSelect
{: #knownindexselect }

This pass falls under the category of the VMProtect specific optimization problems. In fact, whoever looked into VMProtect 3.0.9 knows that the following trick, reimplemented as high level C code for simplicity, is being internally used to select between two branches of a conditional jump.

```cpp
uint64_t ConditionalBranchLogic(uint64_t RFLAGS) {
  // Extracting the ZF flag bit
  uint64_t ConditionBit = (RFLAGS & 0x40) >> 6;
  // Writing the jump destinations
  uint64_t Stack[2] = { 0 };
  Stack[0] = 5369966919;
  Stack[1] = 5369966790;
  // Selecting the correct jump destination
  return Stack[ConditionBit];
}
```

What is really happening at the low level is that the branch destinations are written to adjacent stack slots and then a conditional load, controlled by the previously computed flags, is going to select between one slot or the other to fetch the right jump destination.

LLVM doesn’t automatically see through the conditional load, but it is providing us with all the needed information to write such an optimization ourselves. In fact, the [ValueTracking](https://llvm.org/doxygen/ValueTracking_8cpp.html) analysis exposes the `computeKnownBits` function that we can use to determine if the index used in a `getelementptr` instruction is bound to have just two values.

At this point we can generate two separated [load](https://llvm.org/doxygen/classllvm_1_1LoadInst.html) instructions accessing the stack slots with the inferred indices and feed them to a [select](https://llvm.org/doxygen/classllvm_1_1SelectInst.html) instruction controlled by the index itself. At the next `store-to-load` propagation, LLVM will happily identify the matching `store` and `load` instructions, propagating the constants representing the conditional branch destinations and generating a nice `select` instruction with second and third constant operands.

```llvm
; Matched pattern
%i0 = add i64 %base, %index
%i1 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %i0
%i2 = bitcast i8* %i1 to i64*
%i3 = load i64, i64* %i2, align 1

; Exploded form
%51 = add i64 %base, 0
%52 = add i64 %base, 8
%53 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %51
%54 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %52
%55 = bitcast i8* %53 to i64*
%56 = bitcast i8* %54 to i64*
%57 = load i64, i64* %55, align 8
%58 = load i64, i64* %56, align 8
%59 = icmp eq i64 %index, 0
%60 = select i1 %59, i64 %57, i64 %58

; Simplified select instruction
%59 = icmp eq i64 %index, 0
%60 = select i1 %59, i64 5369966919, i64 5369966790
```

The snippet above shows the matched pattern, its exploded form suitable for the LLVM propagation and its final optimized shape. In this case the `ValueTracking` analysis provided the values `0` and `8` as the only feasible ones for the `%index` value.

A brief discussion about this pass can be found in this [chain of messages](http://lists.llvm.org/pipermail/llvm-dev/2020-June/142426.html) in the LLVM mailing list.

### SynthesizeFlags
{: #synthesizeflags }

This pass falls in between the categories of the VMProtect specific optimization problems and LLVM optimization limitations. In fact, this pass is based on the [enumerative synthesis](https://github.com/google/souper/blob/main/lib/Infer/EnumerativeSynthesis.cpp) logic implemented by Souper, with some minor tweaks to make it more performant for our use-case.

This pass exists because I’m lazy and the fewer ad-hoc patterns I write, the happier I am. The patterns we are talking about are the ones generated by the flag manipulations that VMProtect does when computing the condition for a conditional branch. LLVM already does a good job with simplifying part of the patterns, but to obtain mint-like results we absolutely need to help it a bit.

There’s not much to say about this pass, it is basically invoking Souper’s enumerative synthesis with a selected set of components (`Inst::CtPop`, `Inst::Eq`, `Inst::Ne`, `Inst::Ult`, `Inst::Slt`, `Inst::Ule`, `Inst::Sle`, `Inst::SAddO`, `Inst::UAddO`, `Inst::SSubO`, `Inst::USubO`, `Inst::SMulO`, `Inst::UMulO`), requiring the synthesis of a single instruction, enabling the data-flow pruning option and bounding the LHS candidates to a maximum of 50. Additionally the pass is executing the synthesis only on the `i1` conditions used by the `select` and [br](https://llvm.org/doxygen/classllvm_1_1BranchInst.html) instructions.

This [Godbolt page](https://godbolt.org/z/aYdc3xhn9) shows the devirtualized LLVM-IR output obtained appending the `SynthesizeFlags` pass to the pipeline and the resulting assembly with the properly recompiled conditional jumps. The original assembly code can be seen below. It’s a dummy sequence of instructions where the key piece is the comparison between the `rax` and `rbx` registers that drives the conditional branch `jcc`.

```nasm
start:
  cmp rax, rbx
  jcc label
  and rcx, rdx
  mov qword ptr ds:[rcx], 1
  jmp exit
label:
  xor rcx, rdx
  add qword ptr ds:[rcx], 2
exit:
```

### MemoryCoalescing
{: #memorycoalescing }

This pass falls under the category of the generic LLVM optimization passes that couldn’t possibly be included in the mainline framework because they wouldn’t match the quality criteria of a stable pass. Although the transformations done by this pass are applicable to generic LLVM-IR code, even if the handled cases are most likely to be found in obfuscated code.

Passes like `DSE` already attempt to handle the case where a `store` instruction is partially or completely overlapping with other `store` instructions. Although the more convoluted case of multiple `stores` contributing to the value of a single memory slot are somehow only partially handled.

This pass is focusing on the handling of the case illustrated in the following snippet, where multiple smaller `stores` contribute to the creation of a bigger value subsequently accessed by a single `load` instruction.

```llvm
define dso_local i64 @WhatDidYouDoToMySonYouEvilMonster(i64* noalias nonnull align 8 dereferenceable(8) %rax, i64* noalias nonnull align 8 dereferenceable(8) %rbx, i64* noalias nonnull align 8 dereferenceable(8) %rcx, i64* noalias nonnull align 8 dereferenceable(8) %rdx, i64* noalias nonnull align 8 dereferenceable(8) %rsi, i64* noalias nonnull align 8 dereferenceable(8) %rdi, i64* noalias nonnull align 8 dereferenceable(8) %rbp, i64* noalias nonnull align 8 dereferenceable(8) %rsp, i64* noalias nonnull align 8 dereferenceable(8) %r8, i64* noalias nonnull align 8 dereferenceable(8) %r9, i64* noalias nonnull align 8 dereferenceable(8) %r10, i64* noalias nonnull align 8 dereferenceable(8) %r11, i64* noalias nonnull align 8 dereferenceable(8) %r12, i64* noalias nonnull align 8 dereferenceable(8) %r13, i64* noalias nonnull align 8 dereferenceable(8) %r14, i64* noalias nonnull align 8 dereferenceable(8) %r15, i64* noalias nonnull align 8 dereferenceable(8) %flags, i64 %KEY_STUB, i64 %RET_ADDR, i64 %REL_ADDR) {
  %1 = load i64, i64* %rsp, align 8
  %2 = add i64 %1, -8
  %3 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %2
  %4 = add i64 %1, -16
  %5 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %4
  %6 = load i64, i64* %rax, align 8
  %7 = add i64 %1, -10
  %8 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %7
  %9 = bitcast i8* %8 to i64*
  store i64 %6, i64* %9, align 1
  %10 = bitcast i8* %8 to i32*
  %11 = trunc i64 %6 to i32
  %12 = add i64 %1, -6
  %13 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %12
  %14 = bitcast i8* %13 to i32*
  store i32 %11, i32* %14, align 1
  %15 = bitcast i8* %13 to i16*
  %16 = trunc i64 %6 to i16
  %17 = add i64 %1, -4
  %18 = add i64 %1, -12
  %19 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %18
  %20 = bitcast i8* %19 to i64*
  %21 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %17
  %22 = bitcast i8* %21 to i16*
  %23 = load i16, i16* %22, align 1
  store i16 %23, i16* %15, align 1
  %24 = load i32, i32* %14, align 1
  %25 = shl i32 %24, 8
  %26 = bitcast i8* %21 to i32*
  store i32 %25, i32* %26, align 1
  %27 = bitcast i8* %3 to i16*
  store i16 %16, i16* %27, align 1
  %28 = bitcast i8* %8 to i16*
  store i16 %16, i16* %28, align 1
  %29 = load i32, i32* %10, align 1
  %30 = shl i32 %29, 8
  %31 = bitcast i8* %3 to i32*
  store i32 %30, i32* %31, align 1
  %32 = add i64 %1, -14
  %33 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %32
  %34 = add i64 %1, -2
  %35 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %34
  %36 = bitcast i8* %35 to i16*
  %37 = load i16, i16* %36, align 1
  store i16 %37, i16* %27, align 1
  %38 = add i64 %1, -18
  %39 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %38
  %40 = bitcast i8* %39 to i64*
  store i64 %6, i64* %40, align 1
  %41 = bitcast i8* %39 to i32*
  %42 = bitcast i8* %33 to i16*
  %43 = load i16, i16* %42, align 1
  %44 = bitcast i8* %19 to i16*
  %45 = load i16, i16* %44, align 1
  store i16 %45, i16* %42, align 1
  %46 = bitcast i8* %33 to i32*
  %47 = load i32, i32* %46, align 1
  %48 = shl i32 %47, 8
  %49 = bitcast i8* %19 to i32*
  store i32 %48, i32* %49, align 1
  %50 = bitcast i8* %5 to i16*
  store i16 %43, i16* %50, align 1
  %51 = bitcast i8* %39 to i16*
  store i16 %43, i16* %51, align 1
  %52 = load i32, i32* %41, align 1
  %53 = shl i32 %52, 8
  %54 = bitcast i8* %5 to i32*
  store i32 %53, i32* %54, align 1
  %55 = load i16, i16* %28, align 1
  store i16 %55, i16* %50, align 1
  %56 = load i32, i32* %54, align 1
  store i32 %56, i32* %49, align 1
  %57 = load i64, i64* %20, align 1
  store i64 %57, i64* %rax, align 8
  ret i64 5368713262
}
```

Now, you can arm yourself with patience and manually match all the `store` and `load` operations, or you can trust me when I tell you that all of them are concurring to the creation of a single `i64` value that will be finally saved in the `rax` register.

The pass is working at the intra-block level and it’s relying on the analysis results provided by the `MemorySSA`, [ScalarEvolution](https://llvm.org/doxygen/classllvm_1_1ScalarEvolution.html) and [AAResults](https://llvm.org/doxygen/classllvm_1_1AAResults.html) interfaces to backward walk the definition chains concurring to the creation of the value fetched by each `load` instruction in the block. Doing that, it is filling a structure which keeps track of the aliasing `store` instructions, the stored values, and the offsets and sizes overlapping with the memory slot fetched by each `load`. If a sequence of `store` assignments completely defining the value of the whole memory slot is found, the chain is processed to remove the `store-to-load` indirection. Subsequent passes may then rely on this new indirection-free chain to apply more transformations. As an example the previous LLVM-IR snippet turns in the following optimized LLVM-IR snippet when the `MemoryCoalescing` pass is applied before executing the `InstCombine` pass. Nice huh?

```llvm
define dso_local i64 @ThanksToLLVMMySonBswap64IsBack(i64* noalias nonnull align 8 dereferenceable(8) %rax, i64* noalias nonnull align 8 dereferenceable(8) %rbx, i64* noalias nonnull align 8 dereferenceable(8) %rcx, i64* noalias nonnull align 8 dereferenceable(8) %rdx, i64* noalias nonnull align 8 dereferenceable(8) %rsi, i64* noalias nonnull align 8 dereferenceable(8) %rdi, i64* noalias nonnull align 8 dereferenceable(8) %rbp, i64* noalias nonnull align 8 dereferenceable(8) %rsp, i64* noalias nonnull align 8 dereferenceable(8) %r8, i64* noalias nonnull align 8 dereferenceable(8) %r9, i64* noalias nonnull align 8 dereferenceable(8) %r10, i64* noalias nonnull align 8 dereferenceable(8) %r11, i64* noalias nonnull align 8 dereferenceable(8) %r12, i64* noalias nonnull align 8 dereferenceable(8) %r13, i64* noalias nonnull align 8 dereferenceable(8) %r14, i64* noalias nonnull align 8 dereferenceable(8) %r15, i64* noalias nonnull align 8 dereferenceable(8) %flags, i64 %KEY_STUB, i64 %RET_ADDR, i64 %REL_ADDR) {
  %1 = load i64, i64* %rax, align 8
  %2 = call i64 @llvm.bswap.i64(i64 %1)
  store i64 %2, i64* %rax, align 8
  ret i64 5368713262
}
```

### PartialOverlapDSE
{: #partialoverlapdse }

This pass also falls under the category of the generic LLVM optimization passes that couldn’t possibly be included in the mainline framework because they wouldn’t match the quality criteria of a stable pass. Although the transformations done by this pass are applicable to generic LLVM-IR code, even if the handled cases are most likely to be found in obfuscated code.

Conceptually similar to the `MemoryCoalescing` pass, the goal of this pass is to sweep a function to identify chains of `store` instructions that post-dominate a single `store` instruction and kill its value before it is actually being fetched. Passes like `DSE` are doing a similar job, although limited to some forms of full overlapping caused by multiple `stores` on a single post-dominated `store`.

Applying the `-O3` pipeline to the following example won’t remove the first 64 bits dead `store` at `RAM[%0]`, even if the subsequent 64 bits `stores` at `RAM[%0 - 4]` and `RAM[%0 + 4]` fully overlap it, redefining its value.

```llvm
define dso_local void @DSE(i64 %0) local_unnamed_addr #0 {
  %2 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %0
  %3 = bitcast i8* %2 to i64*
  store i64 1234605616436508552, i64* %3, align 1
  %4 = add i64 %0, -4
  %5 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %4
  %6 = bitcast i8* %5 to i64*
  store i64 -6148858396813837381, i64* %6, align 1
  %7 = add i64 %0, 4
  %8 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %7
  %9 = bitcast i8* %8 to i64*
  store i64 -3689348814455574802, i64* %9, align 1
  ret void
}
```

Adding the `PartialOverlapDSE` pass to the pipeline will identify and kill the first `store`, enabling other passes to eventually kill the chain of computations contributing to the stored value. The built-in `DSE` pass is most likely not executing such a kill because collecting information about multiple overlapping `stores` is an expensive operation.

### PointersHoisting
{: #pointershoisting }

This pass is strictly related to the `IsGuaranteedLoopInvariant` patch I submitted, in fact it is just identifying all the pointers that could be safely hoisted to the entry block because depending solely on values coming directly from the entry block. Applying this kind of transformation prior to the execution of the `DSE` pass may lead to better optimization results.

As an example, consider this devirtualized function containing a rather useless switch case. I’m saying rather useless because each `store` in the case blocks is post-dominated and killed by the `store i32 22, i32* %85` instruction, but LLVM is not going to kill those stores until we move the pointer computation to the entry block.

```llvm
define dso_local i64 @F_0x1400045b0(i64* noalias nonnull align 8 dereferenceable(8) %rax, i64* noalias nonnull align 8 dereferenceable(8) %rbx, i64* noalias nonnull align 8 dereferenceable(8) %rcx, i64* noalias nonnull align 8 dereferenceable(8) %rdx, i64* noalias nonnull align 8 dereferenceable(8) %rsi, i64* noalias nonnull align 8 dereferenceable(8) %rdi, i64* noalias nonnull align 8 dereferenceable(8) %rbp, i64* noalias nonnull align 8 dereferenceable(8) %rsp, i64* noalias nonnull align 8 dereferenceable(8) %r8, i64* noalias nonnull align 8 dereferenceable(8) %r9, i64* noalias nonnull align 8 dereferenceable(8) %r10, i64* noalias nonnull align 8 dereferenceable(8) %r11, i64* noalias nonnull align 8 dereferenceable(8) %r12, i64* noalias nonnull align 8 dereferenceable(8) %r13, i64* noalias nonnull align 8 dereferenceable(8) %r14, i64* noalias nonnull align 8 dereferenceable(8) %r15, i64* noalias nonnull align 8 dereferenceable(8) %flags, i64 %KEY_STUB, i64 %RET_ADDR, i64 %REL_ADDR) {
  %1 = load i64, i64* %rsp, align 8
  %2 = load i64, i64* %rcx, align 8
  %3 = call { i32, i32, i32, i32 } asm "  xchgq  %rbx,${1:q}\0A  cpuid\0A  xchgq  %rbx,${1:q}", "={ax},=r,={cx},={dx},0,~{dirflag},~{fpsr},~{flags}"(i32 1)
  %4 = extractvalue { i32, i32, i32, i32 } %3, 0
  %5 = extractvalue { i32, i32, i32, i32 } %3, 1
  %6 = and i32 %4, 4080
  %7 = icmp eq i32 %6, 4064
  %8 = xor i32 %4, 32
  %9 = select i1 %7, i32 %8, i32 %4
  %10 = and i32 %5, 16777215
  %11 = add i32 %9, %10
  %12 = load i64, i64* bitcast (i8* getelementptr inbounds ([0 x i8], [0 x i8]* @GS, i64 0, i64 96) to i64*), align 1
  %13 = add i64 %12, 288
  %14 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %13
  %15 = bitcast i8* %14 to i16*
  %16 = load i16, i16* %15, align 1
  %17 = zext i16 %16 to i32
  %18 = load i64, i64* bitcast (i8* getelementptr inbounds ([0 x i8], [0 x i8]* @RAM, i64 0, i64 5369231568) to i64*), align 1
  %19 = shl nuw nsw i32 %17, 7
  %20 = add i64 %18, 32
  %21 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %20
  %22 = bitcast i8* %21 to i32*
  %23 = load i32, i32* %22, align 1
  %24 = xor i32 %23, 2480
  %25 = add i32 %11, %19
  %26 = trunc i64 %18 to i32
  %27 = add i64 %18, 88
  %28 = xor i32 %25, %26
  br label %29

29:                                               ; preds = %37, %0
  %30 = phi i64 [ %27, %0 ], [ %38, %37 ]
  %31 = phi i32 [ %24, %0 ], [ %39, %37 ]
  %32 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %30
  %33 = bitcast i8* %32 to i32*
  %34 = load i32, i32* %33, align 1
  %35 = xor i32 %34, 30826
  %36 = icmp eq i32 %28, %35
  br i1 %36, label %41, label %37

37:                                               ; preds = %29
  %38 = add i64 %30, 8
  %39 = add i32 %31, -1
  %40 = icmp eq i32 %31, 1
  br i1 %40, label %86, label %29

41:                                               ; preds = %29
  %42 = add i64 %1, 40
  %43 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %42
  %44 = bitcast i8* %43 to i32*
  %45 = load i32, i32* %44, align 1
  %46 = add i64 %1, 36
  %47 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %46
  %48 = bitcast i8* %47 to i32*
  store i32 %45, i32* %48, align 1
  %49 = icmp ugt i32 %45, 5
  %50 = zext i32 %45 to i64
  %51 = add i64 %1, 32
  br i1 %49, label %81, label %52

52:                                               ; preds = %41
  %53 = shl nuw nsw i64 %50, 1
  %54 = and i64 %53, 4294967296
  %55 = sub nsw i64 %50, %54
  %56 = shl nsw i64 %55, 2
  %57 = add nsw i64 %56, 5368964976
  %58 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %57
  %59 = bitcast i8* %58 to i32*
  %60 = load i32, i32* %59, align 1
  %61 = zext i32 %60 to i64
  %62 = add nuw nsw i64 %61, 5368709120
  switch i32 %60, label %66 [
    i32 1465288, label %63
    i32 1510355, label %78
    i32 1706770, label %75
    i32 2442748, label %72
    i32 2740242, label %69
  ]

63:                                               ; preds = %52
  %64 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %51
  %65 = bitcast i8* %64 to i32*
  store i32 9, i32* %65, align 1
  br label %66

66:                                               ; preds = %52, %63
  %67 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %51
  %68 = bitcast i8* %67 to i32*
  store i32 20, i32* %68, align 1
  br label %69

69:                                               ; preds = %52, %66
  %70 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %51
  %71 = bitcast i8* %70 to i32*
  store i32 30, i32* %71, align 1
  br label %72

72:                                               ; preds = %52, %69
  %73 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %51
  %74 = bitcast i8* %73 to i32*
  store i32 55, i32* %74, align 1
  br label %75

75:                                               ; preds = %52, %72
  %76 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %51
  %77 = bitcast i8* %76 to i32*
  store i32 99, i32* %77, align 1
  br label %78

78:                                               ; preds = %52, %75
  %79 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %51
  %80 = bitcast i8* %79 to i32*
  store i32 100, i32* %80, align 1
  br label %81

81:                                               ; preds = %41, %78
  %82 = phi i64 [ %62, %78 ], [ %50, %41 ]
  %83 = phi i64 [ 5368709120, %78 ], [ %2, %41 ]
  %84 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %51
  %85 = bitcast i8* %84 to i32*
  store i32 22, i32* %85, align 1
  store i64 %83, i64* %rcx, align 8
  br label %86

86:                                               ; preds = %37, %81
  %87 = phi i64 [ %82, %81 ], [ 3735929054, %37 ]
  %88 = phi i64 [ 5368727067, %81 ], [ 0, %37 ]
  store i64 %87, i64* %rax, align 8
  ret i64 %88
}
```

When the `PointersHoisting` pass is applied before executing the `DSE` pass we obtain the following code, where the switch case has been completely removed because it has been deemed dead.

```llvm
define dso_local i64 @F_0x1400045b0(i64* noalias nocapture nonnull align 8 dereferenceable(8) %0, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %1, i64* noalias nocapture nonnull align 8 dereferenceable(8) %2, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %3, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %4, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %5, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %6, i64* noalias nocapture nonnull readonly align 8 dereferenceable(8) %7, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %8, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %9, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %10, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %11, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %12, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %13, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %14, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %15, i64* noalias nocapture nonnull readnone align 8 dereferenceable(8) %16, i64 %17, i64 %18, i64 %19) local_unnamed_addr {
  %21 = load i64, i64* %7, align 8
  %22 = load i64, i64* %2, align 8
  %23 = tail call { i32, i32, i32, i32 } asm "  xchgq  %rbx,${1:q}\0A  cpuid\0A  xchgq  %rbx,${1:q}", "={ax},=r,={cx},={dx},0,~{dirflag},~{fpsr},~{flags}"(i32 1)
  %24 = extractvalue { i32, i32, i32, i32 } %23, 0
  %25 = extractvalue { i32, i32, i32, i32 } %23, 1
  %26 = and i32 %24, 4080
  %27 = icmp eq i32 %26, 4064
  %28 = xor i32 %24, 32
  %29 = select i1 %27, i32 %28, i32 %24
  %30 = and i32 %25, 16777215
  %31 = add i32 %29, %30
  %32 = load i64, i64* bitcast (i8* getelementptr inbounds ([0 x i8], [0 x i8]* @GS, i64 0, i64 96) to i64*), align 1
  %33 = add i64 %32, 288
  %34 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %33
  %35 = bitcast i8* %34 to i16*
  %36 = load i16, i16* %35, align 1
  %37 = zext i16 %36 to i32
  %38 = load i64, i64* bitcast (i8* getelementptr inbounds ([0 x i8], [0 x i8]* @RAM, i64 0, i64 5369231568) to i64*), align 1
  %39 = shl nuw nsw i32 %37, 7
  %40 = add i64 %38, 32
  %41 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %40
  %42 = bitcast i8* %41 to i32*
  %43 = load i32, i32* %42, align 1
  %44 = xor i32 %43, 2480
  %45 = add i32 %31, %39
  %46 = trunc i64 %38 to i32
  %47 = add i64 %38, 88
  %48 = xor i32 %45, %46
  %49 = add i64 %21, 32
  %50 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %49
  br label %51

  %52 = phi i64 [ %47, %20 ], [ %60, %59 ]
  %53 = phi i32 [ %44, %20 ], [ %61, %59 ]
  %54 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %52
  %55 = bitcast i8* %54 to i32*
  %56 = load i32, i32* %55, align 1
  %57 = xor i32 %56, 30826
  %58 = icmp eq i32 %48, %57
  br i1 %58, label %63, label %59

  %60 = add i64 %52, 8
  %61 = add i32 %53, -1
  %62 = icmp eq i32 %53, 1
  br i1 %62, label %88, label %51

  %64 = add i64 %21, 40
  %65 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %64
  %66 = bitcast i8* %65 to i32*
  %67 = load i32, i32* %66, align 1
  %68 = add i64 %21, 36
  %69 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %68
  %70 = bitcast i8* %69 to i32*
  store i32 %67, i32* %70, align 1
  %71 = icmp ugt i32 %67, 5
  %72 = zext i32 %67 to i64
  br i1 %71, label %84, label %73

  %74 = shl nuw nsw i64 %72, 1
  %75 = and i64 %74, 4294967296
  %76 = sub nsw i64 %72, %75
  %77 = shl nsw i64 %76, 2
  %78 = add nsw i64 %77, 5368964976
  %79 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %78
  %80 = bitcast i8* %79 to i32*
  %81 = load i32, i32* %80, align 1
  %82 = zext i32 %81 to i64
  %83 = add nuw nsw i64 %82, 5368709120
  br label %84

  %85 = phi i64 [ %83, %73 ], [ %72, %63 ]
  %86 = phi i64 [ 5368709120, %73 ], [ %22, %63 ]
  %87 = bitcast i8* %50 to i32*
  store i32 22, i32* %87, align 1
  store i64 %86, i64* %2, align 8
  br label %88

  %89 = phi i64 [ %85, %84 ], [ 3735929054, %59 ]
  %90 = phi i64 [ 5368727067, %84 ], [ 0, %59 ]
  store i64 %89, i64* %0, align 8
  ret i64 %90
}
```

### ConstantConcretization
{: #constantconcretization }

This pass falls under the category of the generic LLVM optimization passes that are useful when dealing with obfuscated code, but basically useless, at least in the current shape, in a standard compilation pipeline. In fact, it’s not uncommon to find obfuscated code relying on constants stored in data sections added during the protection phase.

As an example, on some versions of VMProtect, when the `Ultra` mode is used, the conditional branch computations involve dummy constants fetched from a data section. Or if we think about a virtualized jump table (e.g. generated by a switch in the original binary), we also have to deal with a set of constants fetched from a data section.

Hence the reason for having a custom pass that, during the execution of the pipeline, identifies potential constant data accesses and converts the associated memory `load` into an LLVM [constant](https://llvm.org/doxygen/classllvm_1_1ConstantInt.html) (or chain of constants). This process can be referred to as constant(s) concretization.

The pass is going to identify all the load memory accesses in the function and determine if they fall in the following categories:

- A [constantexpr](https://llvm.org/doxygen/classllvm_1_1ConstantExpr.html) memory load that is using an address contained in one of the binary sections; this case is what you would hit when dealing with some kind of data-based obfuscation;
- A symbolic memory load that is using as base an address contained in one of the binary sections and as index an expression that is constrained to a limited amount of values; this case is what you would hit when dealing with a jump table.

In both cases the user needs to provide a safe set of memory ranges that the pass can consider as `read-only`, otherwise the pass will restrict the concretization to addresses falling in `read-only` sections in the binary.

In the first case, the address is directly available and the associated value can be resolved simply parsing the binary.

In the second case the expression computing the symbolic memory access is sliced, the constraint(s) coming from the predecessor block(s) are harvested and Souper is queried in an incremental way (conceptually similar to the one used while solving the outgoing edges in a `VmBlock`) to obtain the set of addresses accessing the binary. Each address is then verified to be really laying in a binary section and the corresponding value is fetched. At this point we have a unique mapping between each address and its value, that we can turn into a selection cascade, illustrated in the following LLVM-IR snippet:

```llvm
; Fetching the switch control value from [rsp + 40]
%2 = add i64 %rsp, 40
%3 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %2
%4 = bitcast i8* %3 to i32*
%72 = load i32, i32* %4, align 1
; Computing the symbolic address
%84 = zext i32 %72 to i64
%85 = shl nuw nsw i64 %84, 1
%86 = and i64 %85, 4294967296
%87 = sub nsw i64 %84, %86
%88 = shl nsw i64 %87, 2
%89 = add nsw i64 %88, 5368964976
; Generated selection cascade
%90 = icmp eq i64 %89, 5368964988
%91 = icmp eq i64 %89, 5368964980
%92 = icmp eq i64 %89, 5368964984
%93 = icmp eq i64 %89, 5368964992
%94 = icmp eq i64 %89, 5368964996
%95 = select i1 %90, i64 2442748, i64 1465288
%96 = select i1 %91, i64 650651, i64 %95
%97 = select i1 %92, i64 2740242, i64 %96
%98 = select i1 %93, i64 1706770, i64 %97
%99 = select i1 %94, i64 1510355, i64 %98
```

The `%99` value will hold the proper constant based on the address computed by the `%89` value. The example above represents the lifted jump table shown in the next snippet, where you can notice the jump table base `0x14003E770` (`5368964976`) and the corresponding addresses and values:

```cpp
.rdata:0x14003E770 dd 0x165BC8
.rdata:0x14003E774 dd 0x9ED9B
.rdata:0x14003E778 dd 0x29D012
.rdata:0x14003E77C dd 0x2545FC
.rdata:0x14003E780 dd 0x1A0B12
.rdata:0x14003E784 dd 0x170BD3
```

If we have a peek at the sliced jump condition implementing the virtualized switch case (below), this is how it looks after the `ConstantConcretization` pass has been scheduled in the pipeline and further `InstCombine` executions updated the selection cascade to compute the switch case addresses. Souper will therefore be able to identify the 6 possible outgoing edges, leading to the devirtualized switch case presented in the `PointersHoisting` section:

```llvm
; Fetching the switch control value from [rsp + 40]
%2 = add i64 %rsp, 40
%3 = getelementptr inbounds [0 x i8], [0 x i8]* @RAM, i64 0, i64 %2
%4 = bitcast i8* %3 to i32*
%72 = load i32, i32* %4, align 1
; Computing the symbolic address
%84 = zext i32 %72 to i64
%85 = shl nuw nsw i64 %84, 1
%86 = and i64 %85, 4294967296
%87 = sub nsw i64 %84, %86
%88 = shl nsw i64 %87, 2
%89 = add nsw i64 %88, 5368964976
; Generated selection cascade
%90 = icmp eq i64 %89, 5368964988
%91 = icmp eq i64 %89, 5368964980
%92 = icmp eq i64 %89, 5368964984
%93 = icmp eq i64 %89, 5368964992
%94 = icmp eq i64 %89, 5368964996
%95 = select i1 %90, i64 5371151872, i64 5370415894
%96 = select i1 %91, i64 5369359775, i64 %95
%97 = select i1 %92, i64 5371449366, i64 %96
%98 = select i1 %93, i64 5370174412, i64 %97
%99 = select i1 %94, i64 5370219479, i64 %98
%100 = call i64 @HelperKeepPC(i64 %99) #15
```

## Unsupported instructions
{: #unsupported-instructions }

It is well known that all the virtualization-based protectors support only a subset of the targeted ISA. Thus, when an unsupported instruction is found, an exit from the virtual machine is executed (context switching to the host code), running the unsupported instruction(s) and re-entering the virtual machine (context switching back to the virtualized code).

The [UnsupportedInstructionsLiftingToLLVM](https://github.com/LLVMParty/UnsupportedInstructionsLiftingToLLVM) proof-of-concept is an attempt to lift the unsupported instructions to LLVM-IR, generating an [InlineAsm](https://llvm.org/doxygen/classllvm_1_1InlineAsm.html) instruction configured with the set of clobbering constraints and (ex|im)plicitly accessed registers. An execution context structure representing the general purpose registers is employed during the lifting to feed the inline assembly call instruction with the loaded registers, and to store the updated registers after the inline assembly execution.

This approach guarantees a smooth connection between two virtualized `VmStubs` and an intermediate sequence of unsupported instructions, enabling some of the LLVM optimizations and a better registers allocation during the recompilation phase.

An example of the lifted unsupported instruction `rdtsc` follows:

```llvm
define void @Unsupported_rdtsc(%ContextTy* nocapture %0) local_unnamed_addr #0 {
  %2 = tail call %IAOutTy.0 asm sideeffect inteldialect "rdtsc", "={eax},={edx}"() #1
  %3 = bitcast %ContextTy* %0 to i32*
  %4 = extractvalue %IAOutTy.0 %2, 0
  store i32 %4, i32* %3, align 4
  %5 = getelementptr inbounds %ContextTy, %ContextTy* %0, i64 0, i32 3, i32 0
  %6 = bitcast %RegisterW* %5 to i32*
  %7 = extractvalue %IAOutTy.0 %2, 1
  store i32 %7, i32* %6, align 4
  ret void
}
```

An example of the lifted unsupported instruction `cpuid` follows:

```llvm
define void @Unsupported_cpuid(%ContextTy* nocapture %0) local_unnamed_addr #0 {
  %2 = bitcast %ContextTy* %0 to i32*
  %3 = load i32, i32* %2, align 4
  %4 = getelementptr inbounds %ContextTy, %ContextTy* %0, i64 0, i32 2, i32 0
  %5 = bitcast %RegisterW* %4 to i32*
  %6 = load i32, i32* %5, align 4
  %7 = tail call %IAOutTy asm sideeffect inteldialect "cpuid", "={eax},={ecx},={edx},={ebx},{eax},{ecx}"(i32 %3, i32 %6) #1
  %8 = extractvalue %IAOutTy %7, 0
  store i32 %8, i32* %2, align 4
  %9 = extractvalue %IAOutTy %7, 1
  store i32 %9, i32* %5, align 4
  %10 = getelementptr inbounds %ContextTy, %ContextTy* %0, i64 0, i32 3, i32 0
  %11 = bitcast %RegisterW* %10 to i32*
  %12 = extractvalue %IAOutTy %7, 2
  store i32 %12, i32* %11, align 4
  %13 = getelementptr inbounds %ContextTy, %ContextTy* %0, i64 0, i32 1, i32 0
  %14 = bitcast %RegisterW* %13 to i32*
  %15 = extractvalue %IAOutTy %7, 3
  store i32 %15, i32* %14, align 4
  ret void
}
```

## Recompilation
{: #recompilation }

I haven’t really explored the recompilation in depth so far, because my main objective was to obtain readable LLVM-IR code, but some considerations follow:

- If the goal is being able to execute, and eventually decompile, the recovered code, then compiling the devirtualized function using the layer of indirection offered by the general purpose register pointers is a valid way to do so. It is conceptually similar to the kind of indirection used by Remill with its own `State` structure. SATURN employs this technique when the stack slots and arguments recovery cannot be applied.
- If the goal is to achieve a 1:1 register allocation, then things get more complicated because one can’t simply map all the general purpose register pointers to the hardware registers hoping for no side-effect to manifest.

The major issue to deal with when attempting a 1:1 mapping is related to how the recompilation may unexpectedly change the stack layout. This could happen if, during the [register allocation](https://en.wikipedia.org/wiki/Register_allocation) phase, some spilling slot is allocated on the stack. If these additional spilling+reloading semantics are not adequately handled, some pointers used by the function may access unforeseen stack slots with disastrous results.

## Results showcase
{: #results-showcase }

The following log files contain the output of the PoC tool executed on functions showcasing different code constructs (e.g. loop, jump table) and accessing different data structures (e.g. GS segment, DS segment, KUSER_SHARED_DATA structure):

- [0x140001d10@DevirtualizeMe1](https://github.com/LLVMParty/TicklingVMProtect/blob/master/Results/DevirtualizeMe1Func1.ll): calling `KERNEL32.dll::GetTickCount64` and literally included as nostalgia kicked in;
- [0x140001e60@DevirtualizeMe2](https://github.com/LLVMParty/TicklingVMProtect/blob/master/Results/DevirtualizeMe2Func2.ll): executing an unsupported `cpuid` and with some nicely recovered `llvm.fshl.i64` intrinsic calls used as rotations;
- [0x140001d20@DevirtualizeMe2](https://github.com/LLVMParty/TicklingVMProtect/blob/master/Results/DevirtualizeMe2Func1.ll): calling `ADVAPI32.dll::GetUserNameW` and with a nicely recovered `llvm.bswap.i64` intrinsic call;
- [0x13001334@EMP](https://github.com/LLVMParty/TicklingVMProtect/blob/master/Results/RandomEMPFunc2.ll): DllEntryPoint, calling another internal function (intra-call);
- [0x1301d000@EMP](https://github.com/LLVMParty/TicklingVMProtect/blob/master/Results/RandomEMPFunc3.ll): calling `KERNEL32.dll::LoadLibraryA`, `KERNEL32.dll::GetProcAddress`, calling other internal functions (intra-calls), executing several unsupported `cpuid` instructions;
- [0x130209c0@EMP](https://github.com/LLVMParty/TicklingVMProtect/blob/master/Results/RandomEMPFunc1.ll): accessing `KUSER_SHARED_DATA` and with nicely synthesized conditional jumps;
- [0x1400044c0@Switches64](https://github.com/LLVMParty/TicklingVMProtect/blob/master/Results/Switches64Func1.ll): executing the `CPUID` handler and devirtualized with `PointersHoisting` disabled to preserve the switch case.

Searching for the `@F_` pattern in your favourite text editor will bring you directly to each devirtualized `VmStub`, immediately preceded by the textual representation of the recovered CFG.

## Closing notes
{: #afterword }

I apologize for the length of the series, but I didn’t want to discard bits of information that could possibly help others approaching LLVM as a deobfuscation framework, especially knowing that, at this time, several parties are currently working on their own LLVM-based solution. I felt like showcasing its effectiveness and limitations on a well-known obfuscator was a valid way to dive through most of the details. Please note that the process described in the posts is just one of the many possible ways to approach the problem, and by no means the best way.

The [source code](https://github.com/LLVMParty/TicklingVMProtect) of the proof-of-concept should be considered an experimentation playground, with everything that involves (e.g. bugs, unhandled edge cases, non production-ready quality). As a matter of fact, some of the components are barely sketched to let me focus on improving the LLVM optimization pipeline. In the future I’ll try to find the time to polish most of it, but in the meantime I hope it can at least serve as a reference to better understand the explained concepts.

Feel free to reach out with doubts, questions or even flaws you may have found in the process, I’ll be more than happy to allocate some time to discuss them.

## Acknowledgments
{: #acknowledgments }

- Peter, for introducing me to LLVM and working on SATURN together.
- mrexodia and mrphrazer, for the in-depth review of the posts.
- justmusjle, for enhancing the colors used by the diagrams.
- Secret Club, for their suggestions and series hosting.

See you at the next LLVM adventure!
