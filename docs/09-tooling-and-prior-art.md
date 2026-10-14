# Tooling and prior art

The method in this repo is assembled from public tools and from community threads where people worked out which variable byte does what on which board. This page lists them, says where each fits, gives the license where it could be read, and states what this repo adds.

**This repo ships no vendor firmware.** No BIOS images, ROMs, capsules, extracted modules, IFR dumps taken from a vendor image, or variable dumps from anyone's machine. The `.gitignore` excludes `*.bin`, `*.rom`, `*.cap`, `*.fd` and dump directories for that reason. Where a step needs a vendor image, you download it from the vendor and run the extraction tools on your own copy. The tools below are linked, not bundled.

Licenses were read from GitHub's license field for each repository on 2026-10-13. They are not legal advice; check the repository before you reuse anything. Hardware and software names belong to their owners, and nothing here is affiliated with or endorsed by them.

## How the pieces fit

1. Get the exact BIOS build you are running, from the vendor.
2. Parse it with **UEFITool / UEFIExtract** and find the `Setup` module.
3. Turn the module into text with **IFRExtractor-RS** and read the target question's store and offset.
4. Read and, if the checks pass, write the variables: through **efivarfs** on Linux (the reference build), or from a UEFI shell with **setup_var.efi** when the store has no runtime access.
5. Confirm by the physical outcome, not by reading the value back.

## The tools

| Tool | What it does | Where it fits | License |
|---|---|---|---|
| [UEFITool NE / UEFIExtract](https://github.com/LongSoft/UEFITool) (LongSoft) | Parses, searches and extracts UEFI firmware images | Step 2: locate `Setup`, search for text, list modules to scan for guards | BSD-2-Clause |
| [IFRExtractor-RS](https://github.com/LongSoft/IFRExtractor-RS) (LongSoft) | Converts IFR from a module into readable text | Step 3: find the question, its store and its offset | BSD-2-Clause |
| [setup_var.efi](https://github.com/datasone/setup_var.efi) (datasone) | UEFI-shell tool that reads and writes a variable at an offset | Step 4, shell route only | Apache-2.0 |
| [grub-mod-setup_var](https://github.com/datasone/grub-mod-setup_var) (datasone) | GRUB patch that does the same from the GRUB prompt | Predecessor of `setup_var.efi`; not used here | GPL-3.0 |
| [ReBarUEFI](https://github.com/xCuri0/ReBarUEFI) (xCuri0) | DXE driver that adds Resizable BAR; its wiki documents hidden 4G decoding | Related problem; closest public precursor to this method | MIT |
| [NvStrapsReBar](https://github.com/terminatorul/NvStrapsReBar) (terminatorul) | ReBarUEFI fork for NVIDIA Turing (GTX 16, RTX 20) | Different problem | MIT |
| [chipsec](https://github.com/chipsec/chipsec) | Platform security framework with a kernel driver and a variable CLI | Possible read-only enumeration; not needed on Linux with efivarfs | GPL-2.0 |
| [efivar](https://github.com/rhboot/efivar) (rhboot) and [efivarfs](https://docs.kernel.org/filesystems/efivarfs.html) | Variable plumbing in userspace and in the kernel | Step 4, Linux route: this repo uses plain files on efivarfs | efivar: LGPL-2.1. efivarfs is part of the Linux kernel (GPL-2.0) |
| [Smokeless_UMAF](https://github.com/DavidS95/Smokeless_UMAF) (DavidS95) | UEFI app that shows hidden Setup menus, AMD-focused | Not part of this repo's path | No license file detected by GitHub |
| [Kostr's "Hidden BIOS settings" lesson](https://github.com/Kostr/UEFI-Lessons/tree/master/Lessons_uncategorized/Lesson_Hidden_BIOS_settings) | Tutorial with HII-dump tools | Source for dynamic questions | MIT |
| [RU.efi / RU.exe](http://ruexe.blogspot.com/) | Closed-source hardware and UEFI-variable utility | Not used | Closed source; terms not checked |
| AMI SCEWIN | AMI's settings export utility | Not used | Proprietary; terms not checked |

## Notes on each

### ReBarUEFI (xCuri0)

A DXE driver that hooks `PreprocessController` of the PCI host bridge resource allocation protocol and takes its BAR size from a `ReBarState` NVRAM variable. It is a firmware module, so using it means flashing a modified BIOS image. Its FAQ says there is currently "no known method to get it on Windows without BIOS modification" ([README](https://github.com/xCuri0/ReBarUEFI)). Its issues page notes that Intel Arc cards need Primary Graphics Adapter set to PCI Express ([common issues](https://github.com/xCuri0/ReBarUEFI/wiki/Common-issues-%28and-fixes%29)).

The wiki page [Enabling hidden 4G decoding](https://github.com/xCuri0/ReBarUEFI/wiki/Enabling-hidden-4G-decoding) (updated 2024-03-23) is the nearest public ancestor of this repo: search the image for the Unicode string `4G Decod` (not "Decode"), extract the `Setup` module, run IFRExtractor-RS, and flip the byte with the GRUB `setup_var` patch. It states that some platforms need Above 4GB MMIO assignment as well, and that the `PCI_COMMON` store usually does not enable 4G decoding even though it contains the setting.

What it does not cover, and this repo does: a scan for integrity guards, checksum handling, a width check on the write, backups and a restore path.

### setup_var.efi (datasone)

A Rust rewrite of the GRUB patch, built for `x86_64-unknown-uefi` (aarch64 and i686 untested per the README). The 0.3.x syntax is:

```
setup_var.efi [-r/--reboot(=auto)] [--write_on_demand] <VAR_NAME>[(VAR_ID)]:<OFFSET>[(VALUE_SIZE)][=VALUE]
```

`VAR_NAME` defaults to `Setup`, `VALUE_SIZE` is in bytes and defaults to 1, and `--write_on_demand` skips a write that equals the stored value. It finds variables by name, not by GUID, so same-name collisions need `VAR_ID`. The [0.2.x README](https://github.com/datasone/setup_var.efi/tree/0.2.x) uses a different form (`<OFFSET> [<VALUE>] [-s <VALUE_SIZE>] [-n <VAR_NAME>] [-i <VAR_ID>]`), so a command copied from an old post may not mean what it says today.

Two cautions. Its README warns that "accessing wrong varstore or variable may completely brick your computer". And it says nothing about checksums; on Lenovo it would write `Setup` without updating `LastCheckSum` (inference from the README's silence). `LastCheckSum` is not declared anywhere in the reference image's Setup IFR, so addressing it by HII name looks impossible there; a shell recipe would need the UEFI shell's `setvar` with an explicit GUID, which was not tested.

The width hazard is real. One published M720q command used the 0.2.x form with `-s 0x08` against the byte at `0xA49` ([thread](https://winraid.level1techs.com/t/104420), 2024-11-02). The same post's IFR dump has flags `0x10`, which encode a one-byte field. Read as bytes, that command writes eight bytes and zeroes the seven after the target (reading of the constants, not tested). The thread reports no result.

### grub-mod-setup_var (datasone)

Deprecated in favour of `setup_var.efi`. It provides `setup_var` (store named `Setup`), `setup_var2` (store named `Custom`, used by some Insyde firmware), `setup_var_3` (skips `Setup` variables under `0x10` bytes, for early Dell XPS 8930 BIOS that has a dummy one), plus two lightly tested variants. Writes are limited to one byte, the patched binary drops to the GRUB rescue shell by design, and it was tested on GRUB 2.06 ([README](https://github.com/datasone/grub-mod-setup_var)).

### UEFITool, UEFIExtract and IFRExtractor-RS (LongSoft)

On the reference image, `UEFIExtract <rom> all` failed on Windows with a path-length error (Error 35); `report` mode worked, and the inner volume was then re-dumped from a short path (reported, from the project's notes; the output files exist). IFRExtractor-RS is described as a "Rust utility to extract UEFI IFR data ... into human-readable text" and is run as `ifrextractor <file>`. Parse its output question by question. Searching it for a prompt string hit the wrong question on the reference dump ([FAQ question 2](07-faq.md#2-how-do-i-tell-whether-my-board-hides-the-setting-or-just-doesnt-have-it)).

### chipsec

A general platform-security framework with a kernel driver. Its UEFI commands include `var-list`, `var-read` and `var-write`, with usage along the lines of `chipsec_util uefi var-write <name> <GUID> <file>` (from a usage string seen in a search result, reported, unconfirmed). It is not designed for this job, and a third-party guide warns that misuse can brick a board. On Linux, plain efivarfs does the same thing with fewer moving parts.

### efivar and efivarfs

The [efivarfs documentation](https://docs.kernel.org/filesystems/efivarfs.html) covers the file layout, the immutable flag and the removal warning. The `efivar` library's README carries a warning to "probably not run 'make a brick' *ever*" and says it is not a joke. Its CLI flags were not checked for this repo.

### Smokeless_UMAF, RU.efi, SCEWIN

- **Smokeless_UMAF** swaps in its own form browser to reveal hidden menus. Its README warns that changing settings "could lead to bricking your device" and says some options may need a proper reflash. A known issue: on Dell machines the normal BIOS would not load afterwards, fixed by deleting a stray boot option.
- **RU.efi / RU.exe** is closed source. The latest release notes seen (5.42.0435 Beta, 2025-11-02) are marked "NOT TESTED" and mention UEFI variable editing; its capabilities were not verified here. One report says that on an ASUS board with a "UEFI Variables Runtime protection" option, RU.efi "straight up reverts the changes" ([thread](https://winraid.level1techs.com/t/103324)).
- **AMI SCEWIN** exported only seven runtime-published questions on an ASUS X299 board, which makes it useless for hidden or boot-services-only settings (reported by the repo author, no public source).

### Kostr's lesson

A tutorial that captures the live HII database from a UEFI shell, runs `ifrextractor` on each package list, then edits a variable through HII routing or a variable dump. It is the clearest public treatment of dynamic questions, and it warns that the option you change may not be used by the driver code ("Unfortunately such scenario is possible"). The command sequence above is a summary; read the lesson before you run any of it.

## Community threads

Third-party usernames are left out on purpose.

| Thread | Dates | Why it matters |
|---|---|---|
| [ReBarUEFI wiki: Enabling hidden 4G decoding](https://github.com/xCuri0/ReBarUEFI/wiki/Enabling-hidden-4G-decoding) | updated 2024-03-23 | The method this repo extends |
| [ReBarUEFI discussion 211](https://github.com/xCuri0/ReBarUEFI/discussions/211) | 2024-05-28 to 2025-07-02 | Dell Precision T3620: `PCI_COMMON` byte 3, a dead first boot, CMOS battery pull |
| [M720q and `setup_var`](https://winraid.level1techs.com/t/104420) | 2024-11-02 | Independent derivation of the same IFR entry (QuestionId `0x54`, offset `0xA49`, `SuppressIf`); no result reported |
| [Unlocked Lenovo M720q BIOS](https://winraid.level1techs.com/t/38139) | 2021-12-07 to 2022-01-09 | BIOS M1UKT66A; a flash tool write fails with "Error 167" |
| [ThinkStation P310](https://winraid.level1techs.com/t/94916) | 2023-05-27 to 2026-02-03 | `PCI_COMMON` size 6, byte 3 reads 1, GPU-Z still reports no 4G decoding |
| [ThinkStation P330 tower](https://winraid.level1techs.com/t/104196) | 2024-10-20 to 2025-07-31 | Same `PCI_COMMON` layout as the M720q; enable flag reverts at restart |
| [ThinkCentre M90q Gen 1](https://winraid.level1techs.com/t/117014) | 2026-06-02 | Different BIOS family; GPU absent from the bus; unanswered |
| [EliteDesk 800 G5 SFF and Tesla T4](https://winraid.level1techs.com/t/96245) | 2023-08-11 to 2025-02-09 | `ACCESS_DENIED` on write; a G4 SFF workaround with a P4 |
| [Dell G5 5590 after `setup_var_3`](https://winraid.level1techs.com/t/103135) | 2024-08-20 to 2024-08-21 | What a wrong write to the wrong field can do |
| [`SetVariable` called security violation](https://winraid.level1techs.com/t/103324) | 2024-08-30 to 2024-09-09 | Authenticated variables; blind writers fail |
| [Tesla T4 PCI I/O region invalid](https://forums.developer.nvidia.com/t/tesla-t4-pci-i-o-region-invalid/120643) | 2020-04-22 | The `BAR0 is 0M @ 0x0` driver signature |
| [Dell Precision T7820 and Tesla P40](https://winraid.level1techs.com/t/102347) | 2024-07-02 | Option present and still failing |
| [GA-X99-UD3P and Tesla P100](https://winraid.level1techs.com/t/117195) | 2026-06-09 to 2026-06-17 | Option present, several cards, not all boot |
| [CERT/CC VU#758382](https://kb.cert.org/vuls/id/758382) | public 2014-05-27, revised 2015-02-03 | The `Setup` variable as a documented weakness (CVE-2014-2961) |

Those are forum posts, so the facts in them are reported, unconfirmed unless this repo reproduced them. A search for a ServeTheHome or Reddit thread on this exact unlock (M720q or M920q with a Tesla or Arc card) found nothing usable.

## What this repo adds

The public material above is a tutorial for finding the byte, a tool for writing a byte, and a pile of per-board anecdotes. What was missing is the part in between: doing it with checks, and knowing when to stop.

1. **A guarded sequence instead of a tutorial.** Fingerprint the BIOS version (and the board and product name, if the profile lists them) from the DMI fields against a profile you verified yourself. Dry-run first, showing exactly which bytes differ. Back up every related variable with its attribute prefix and verify the backup by re-reading. Write each variable in one call, read it back, and check the whole blob differs only where intended. Reboot **without the card**, verify, then fit it. The shipped tools implement this: [tools/above4g_unlock.py](../tools/above4g_unlock.py) (dry run by default, a typed `--confirm` token, a backup with a manifest, a whole-blob diff after the write) and [tools/restore.py](../tools/restore.py) (scripted restore). See [tools/README.md](../tools/README.md).
2. **A model of the Lenovo checksum guard.** Where `LastCheckSum` lives, how it is compared, why the stored value must be derived from a live-measured delta and never assumed, and the write order that keeps the 0162 window short. No public description of this was found; the P330 "reverts at restart" report suggests others hit it ([FAQ question 10](07-faq.md#10-what-is-the-checksum-error-0162-about)).
3. **Guard and lock detection before any write.** Scan the image for early modules whose names contain `CheckSum`, `Crc`, `Hash` or `Integrity`. Scan live variable names for the same patterns and for flag variables. Snapshot all variables, change one visible option through Setup, snapshot again, and diff to catch bookkeeping variables. Decode attributes, map Linux errors (`EROFS`, `EACCES`, `EINVAL`) to meanings, and stop on authenticated variables. If a guard exists and cannot be modelled, the answer is refusal, not a guess.
4. **A dynamic-question section**, so nobody has to guess "byte 0".
5. **A width rule.** The tools default to a one-byte field and refuse a wider write unless you pass `--allow-multibyte` after reading the width in your IFR. They cannot read the true width themselves, so you state it.
6. **A provenance-tagged table of known platforms** ([02](02-does-my-hardware-qualify.md#known-platforms)). Each row names the board and BIOS where known, the route, the findings and the outcome, and is tagged verified, reported, inference or unconfirmed.
7. **Verification by physical outcome.** A value echoing back is not the test. The 64-bit root window with the card out, then the BAR placement with the card in, is.
8. **Honest qualification.** Slot-less minis, the HP G4 and G5 limits, and Dell and HP write denials are listed as what they are.

## What it deliberately does not do

- No SPI flashing and no modified firmware images.
- No Resizable BAR sizing.
- No guard bypass. Nothing here deletes `LastCheckSum` or defeats a variable lock.
- No AMD-specific menu unhiding, and no HP Sure Start workaround.
- No claim that the offsets for one board apply to another.
