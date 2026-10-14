# Glossary

Alphabetical, for lookup. Standard PCI and UEFI terms follow the PCI-SIG and UEFI specifications. Anything specific to this repo carries its source: *verified on the reference build* (the Lenovo ThinkCentre M720q Tiny with an NVIDIA A2, see [06-reference-build.md](06-reference-build.md)), a link, or *reported, unconfirmed*.

---

## Above 4G Decoding
Firmware option that lets the BIOS assign 64-bit BARs above the 4 GB line of the address space. Vendors name it differently: "Above 4GB MMIO", "Memory Mapped I/O above 4 GB" (Dell servers), "PCI 64-Bit Resource Allocation" (Lenovo ThinkSystem servers; both reported, unconfirmed). On AMI Aptio V it is usually a [dynamic question](#dynamic-question), and on the reference BIOS it is most likely byte 3 of [`PCI_COMMON`](#pci_common) (inferred). Turn CSM off first: with CSM on, enabling it can leave a black screen or lock you out of Setup ([ReBarUEFI wiki](https://github.com/xCuri0/ReBarUEFI/wiki/Enabling-hidden-4G-decoding)). See also [Above 4GB MMIO](#above-4gb-mmio-bios-assignment).

## Above 4GB MMIO (BIOS assignment)
A companion option that makes the firmware open a 64-bit MMIO window. Some vendors list it under System Agent configuration (reported, weak source: one vendor reply seen in a search summary). On the reference BIOS it is `Setup` offset `0xA49`, QuestionId `0x54`, default Disabled, hidden by a `SuppressIf` when "Aperture Size" is 2048 MB. After it was set, together with `PCI_COMMON` byte 3, a 256 GB root bus window appeared at `0x4000000000` to `0x7fffffffff` with no card installed (verified). The ReBarUEFI wiki notes that some platforms need this option as well as Above 4G Decoding.

## Attributes (NV, BS, RT, authenticated)
Bit flags attached to every [UEFI variable](#uefi-variable), as defined in the [Windows API documentation](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setfirmwareenvironmentvariableexa):

| Bit | Name | Meaning |
|---|---|---|
| `0x1` | NV | Non-volatile: survives power-off |
| `0x2` | BS | Readable at boot services time |
| `0x4` | RT | Readable and writable by the running OS (must be set together with BS) |
| `0x8` | Hardware error record | Reserved for firmware error records |
| `0x10` | Authenticated write | Needs a signed payload |
| `0x20` | Time-based authenticated write | Needs a signed, timestamped payload |
| `0x40` | Append write | |

`0x7` (NV+BS+RT) is writable from the OS and is what `Setup` and `PCI_COMMON` had on the reference build. `0x3` means invisible at runtime: only a pre-boot tool can touch it. A variable with `0x10` or `0x20` set is authenticated; `setup_var.efi`-style tools write blindly and fail with a security violation, so stop. Attributes cannot be changed by overwriting. The only way is delete and recreate, and this repo never does that to a setup variable. In [efivarfs](#efivarfs) the first 4 bytes of each file are the attributes, little-endian (`07 00 00 00`).

## BAR (Base Address Register)
A PCI configuration register in which a device states how much address space it needs and where the firmware placed it. Software finds the size by writing all ones and reading back. A 32-bit BAR can only live below 4 GB. A 64-bit BAR uses two consecutive registers and can be placed anywhere in the 64-bit space. A card with a 16 GB BAR cannot be placed below 4 GB, which is why POST fails without [Above 4G Decoding](#above-4g-decoding). See [03-how-it-works.md](03-how-it-works.md).

## Boot Guard
An Intel mechanism that checks the first block of firmware code at power-on against values fused into the platform, so a modified BIOS image may refuse to POST. A Dell Precision T3620 user reports exactly that after flashing a modified BIOS (reported, unconfirmed). It guards firmware code. A variable write does not change code, so it does not apply here (inference).

## Checksum guard (`LastCheckSum`)
Lenovo's integrity check on `Setup`: a PEI module sums its bytes into 16 bits and compares the result exactly with the 2-byte variable `LastCheckSum`. The stored value minus the OS-visible sum was +1 once and 0 later, so measure it each time. The compare and delta are verified; the Error 0162 defaults reload is from image analysis, never provoked. See [FAQ question 10](07-faq.md#10-what-is-the-checksum-error-0162-about) and [03](03-how-it-works.md#3-the-vendor-integrity-guard).

## CMOS clear
Resetting the battery-backed settings memory with a jumper or by pulling the coin cell. It is the usual first recovery step, but many boards keep setup variables in the SPI flash chip rather than in CMOS, and then it does not reset them. Evidence runs both ways: the edit survived a CMOS battery pull on a Dell T3620, a Dell G5 helper says a CMOS jumper does not reset NVRAM settings, and X299 byte writes were recoverable by CMOS clear. Do not promise yourself it works. See [05-safety-and-recovery.md](05-safety-and-recovery.md).

## CSM (Compatibility Support Module)
The part of UEFI firmware that emulates a legacy BIOS to boot MBR installs. Above 4G Decoding needs it off. The "Aperture Size" help text on the reference BIOS says as much. Turning it off can stop a Windows install from booting if it was installed in MBR mode; it needs conversion to GPT first ([ReBarUEFI wiki](https://github.com/xCuri0/ReBarUEFI/wiki/Enabling-hidden-4G-decoding)).

## Dynamic question
A Setup question added to the menu at runtime by a driver, so it is not in the static IFR of the `Setup` module. "Above 4G Decoding" on AMI Aptio V appears to be one, injected by the PCI bus driver (`PciBus`) (inference). In the static IFR you see only its neighbours (on the M720q, BME DMA Mitigation at byte 5 and Hot-Plug at byte 2). To find its offset, dump the live HII database from a UEFI shell, disassemble the driver, or compare other boards that use the same store ([Kostr's lesson](https://github.com/Kostr/UEFI-Lessons/tree/master/Lessons_uncategorized/Lesson_Hidden_BIOS_settings) describes the dump).

## efivarfs
The Linux filesystem for UEFI variables, mounted with `mount -t efivarfs none /sys/firmware/efi/efivars`. Files are named `<Name>-<guid-lowercase>`; content is 4 attribute bytes followed by the data. Variables that are not well-known are created immutable, so a write needs `chattr -i` first. A write replaces the whole value, so send the full blob in one `write` call. The kernel rejects a buffer under 4 bytes and attribute bits it does not know ([kernel documentation](https://docs.kernel.org/filesystems/efivarfs.html), [`file.c`](https://github.com/torvalds/linux/blob/master/fs/efivarfs/file.c); the one-call rule is a reading of that source).

## GUID
A 128-bit identifier that namespaces a variable. A variable is identified by name **and** GUID, and the same name can exist under different GUIDs: an early Dell XPS 8930 BIOS has two variables called `Setup`, a 12-byte dummy and the real one ([grub-mod-setup_var README](https://github.com/datasone/grub-mod-setup_var)). The AMI GUIDs used here are `EC87D643-EBA4-4BB5-A1E5-3F3E36B20DA9` for `Setup` and `ACA9F304-21E2-4852-9875-7FF4881D67A5` for `PCI_COMMON`. A shared GUID tells you what kind of store it is. It says nothing about offsets, which move with each BIOS build.

## HII (Human Interface Infrastructure)
UEFI's database of forms and strings from which Setup menus are drawn. The live HII database includes [dynamic questions](#dynamic-question). The static forms inside a firmware image are the [IFR](#ifr-internal-forms-representation).

## IFR (Internal Forms Representation)
The binary opcode format of HII forms. It describes each menu question: its prompt, options, defaults, `SuppressIf` and `GrayOutIf` conditions, and which [varstore](#varstore) and [offset](#varoffset-and-questionid) it reads and writes. [IFRExtractor-RS](https://github.com/LongSoft/IFRExtractor-RS) turns it into text. For numeric and one-of questions the low two bits of `Flags` give the field width: `0x00` one byte, `0x01` two, `0x02` four, `0x03` eight (constants in EDK2's [`UefiInternalFormRepresentation.h`](https://github.com/tianocore/edk2/blob/master/MdePkg/Include/Uefi/UefiInternalFormRepresentation.h)). Check the width before writing; a size that disagrees with it is a reason to stop.

## MMIO (memory-mapped I/O)
Device registers and device memory that appear in the CPU's physical address space. MMIO takes address space, not RAM. A GPU's BARs are MMIO.

## PCI_COMMON
An AMI variable store holding PCI options (GUID under [GUID](#guid)): 6 bytes on a Dell T3620 and a ThinkStation P310, 7 on the M720q, 8 on an ASUS X299 board. Byte 3 is reported or inferred to be Above 4G Decoding there (triangulated on the M720q, necessity unproven: [FAQ question 11](07-faq.md#11-which-of-the-two-settings-actually-did-the-work)), and byte 4 is Re-Size BAR on the X299 layout. Byte 0 is unknown: do not write it.

## PEI, DXE, SMM
UEFI firmware phases. PEI (Pre-EFI Initialization) runs first, before memory is fully up. DXE (Driver Execution Environment) loads the drivers that draw Setup and run the boot. SMM (System Management Mode) is privileged code that keeps running under the OS. On the reference BIOS the checksum compare is in PEI (verified by decoding its bytes). That the error message and defaults reload are in DXE, and that a Lenovo SMM module toggles `PCI_COMMON` byte 3 for Lenovo's own interface, come from earlier image analysis and were not re-run.

## Resizable BAR
A PCIe capability that lets a device advertise several BAR sizes so firmware or the OS can pick a larger one. It needs Above 4G Decoding on, and it is a different setting. This repo does not resize BARs. See [FAQ question 1](07-faq.md#1-is-this-the-same-as-resizable-bar-rebar).

## Root bus window
The address range the host bridge decodes for devices behind a root bus. Linux prints it as `pci_bus 0000:00: root bus resource [mem ...]`, taken from the ACPI `_CRS` method. A new window above `0x100000000` appearing after the reboot is the first physical sign that the unlock took.

## Secure Boot
Signature checking of what the firmware launches. The efivarfs route does not need it off. Unsigned UEFI-shell tools do ("Make sure that Secure Boot is disabled", ReBarUEFI wiki); it can be re-enabled afterwards. Secure Boot's own key variables are authenticated and a separate class from `Setup`: an attempted Secure Boot KEK update on the reference box was refused by the firmware with `Permission denied`.

## Setup (the store)
The AMI variable named `Setup`, GUID `EC87D643-EBA4-4BB5-A1E5-3F3E36B20DA9`, holding most BIOS options. Its size depends on the BIOS build: `0x1388` bytes on the M720q, `0x12C2` on an X299 board. Other stores share the GUID under different names (for example `SetupCpuFeatures`), so select by **name**, GUID and size together. The reference offset is `0xA49`; do not assume it on another BIOS.

## setup_var.efi
datasone's UEFI-shell tool for reading and writing a variable at an offset. Syntax in the 0.3.x line is `setup_var.efi [-r] <VAR_NAME>[(VAR_ID)]:<OFFSET>[(VALUE_SIZE)][=VALUE]`. It addresses variables by name, not GUID, and its README warns that touching the wrong variable "may completely brick your computer". Because it works in the pre-boot environment it reaches variables that have no runtime access. Its README says nothing about checksums, so it will not keep `LastCheckSum` in step ([README](https://github.com/datasone/setup_var.efi)). More in [09-tooling-and-prior-art.md](09-tooling-and-prior-art.md).

## SPI flash
The chip on the board that holds the firmware and, on many boards, the NVRAM where UEFI variables live. Writing it directly (with a programmer or a vendor flash tool) is a different and riskier operation than writing a variable, and is what the repo avoids.

## TOLUD (Top Of Low Usable DRAM)
An Intel chipset term for the top of installed memory below 4 GB. Addresses from TOLUD up to 4 GB are not RAM. They are the MMIO region (the "PCI hole") where 32-bit BARs and system devices must fit. A 16 GB BAR can never fit there. Register details differ by platform.

## UEFI variable
A named blob in firmware storage, keyed by name and [GUID](#guid), with [attributes](#attributes-nv-bs-rt-authenticated). Firmware reads and writes them through `GetVariable` and `SetVariable`. The OS can reach those with the runtime attribute. Setup menus, boot entries and the Secure Boot keys are all stored this way.

## VariablePolicy and VarCheck
EDK2 mechanisms a platform may use to lock variables: `VariablePolicy` ([`VariablePolicy.h`](https://github.com/tianocore/edk2/blob/master/MdeModulePkg/Include/Protocol/VariablePolicy.h)), `VarCheck` ([`VarCheckLib.h`](https://github.com/tianocore/edk2/blob/master/MdeModulePkg/Include/Library/VarCheckLib.h)) and the older `VariableLock`. A locked write surfaces on Linux as `EROFS`, `EACCES` or `EINVAL`: treat it as a stop and restore from backup ([05](05-safety-and-recovery.md#35-stop-conditions)). Which boards use them was not established, and nothing here tries to defeat them.

## VarOffset and QuestionId
In the IFR, a question carries a `QuestionId` and a `VarOffset` (the byte it controls in its varstore). Read both from the question's own block, never by grepping a prompt or a number: on the reference dump the "Above 4GB MMIO" hit was the end of the help text of "Aperture Size" (offset `0x993`, value 1 = "256 MB"), and `grep 0xA49` hits an unrelated `QuestionId`. Verified on the reference build.

## Varstore
An IFR declaration that binds a form's questions to a variable: a store ID, the variable's GUID, size and name. A question then says "store 1, offset `0xA49`". On the reference BIOS store `0x1` is `Setup` (size `0x1388`) and store `0xCCCC` is `PCI_COMMON` (size 7). Resolve the store by ID in the same IFR, never by guessing a name.
