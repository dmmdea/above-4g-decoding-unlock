# FAQ

Short answers to the questions people ask first. Each answer says where its claim comes from:

- **Verified on the reference build**: measured on the Lenovo ThinkCentre M720q Tiny with an NVIDIA A2 16 GB, described in [06-reference-build.md](06-reference-build.md).
- **Reported, unconfirmed**: a forum post or secondary source. This repo did not reproduce it.
- **Inference**: a reading of the evidence that nobody has tested.
- A link: a primary page, quoted or paraphrased.

Terms are defined in [08-glossary.md](08-glossary.md). Prior tools and threads are in [09-tooling-and-prior-art.md](09-tooling-and-prior-art.md).

---

## 1. Is this the same as Resizable BAR (ReBAR)?

No. They are related, and in practice one depends on the other.

**Above 4G Decoding** lets the firmware place a device's 64-bit BARs above the 4 GB line of the address space. **Resizable BAR** is a PCIe capability that lets a device offer several BAR sizes so the firmware or OS can choose a bigger one, usually to expose all of a card's memory instead of a 256 MB window. Different problem, different setting.

- ReBAR needs Above 4G Decoding on. The [ReBarUEFI README](https://github.com/xCuri0/ReBarUEFI) lists 4G decoding as a prerequisite.
- Large datacenter cards generally do not need ReBAR. They ask for a big BAR by default. On the reference build the A2's BAR1 was 16 GB, placed at `0x4400000000` once a 64-bit window existed (verified). A Tesla P100 is reported to have a 16 GB BAR by default ([thread](https://winraid.level1techs.com/t/117195), reported, unconfirmed).
- The two can be separate bytes. On an ASUS ROG Strix X299-E Gaming II (BIOS 2103), `PCI_COMMON` byte 3 was Above 4G Decoding and byte 4 was Re-Size BAR; with both set, BAR1 on three cards went from 256 MiB to 16384 MiB. That was measured on one board with no public source, and offsets differ by board.
- Intel says Arc cards "can still work" without ReBAR but need it "for optimal performance" ([Intel article 000092416](https://www.intel.com/content/www/us/en/support/articles/000092416/graphics.html)). If you want Arc on a box like this, Above 4G is the first step and ReBAR is a second, separate one.

This repo only handles Above 4G Decoding. It does not resize BARs. Setting an unsupported BAR size can stop a board from booting; ReBarUEFI's README says to clear CMOS and "in some cases" pull the CMOS battery. ReBarUEFI is also a firmware module that needs a modified BIOS image, which is a different risk class (see question 14).

## 2. How do I tell whether my board hides the setting or just doesn't have it?

Read the firmware's own menu definitions, not the menu. The setting is hidden if the question exists in the BIOS image's IFR, usually under a `SuppressIf` or on a page the vendor never displays. It is missing if the IFR has no such question and nothing injects one at runtime.

On the reference BIOS the static question is `OneOf "Above 4GB MMIO BIOS assignment"`, QuestionId `0x54`, store `Setup`, offset `0xA49`, default Disabled, wrapped in a `SuppressIf` on another option (verified). "Above 4G Decoding" itself has no static question there (verified). It appears to be added at runtime by the PCI bus driver (inference), so the static IFR shows only its neighbours.

Some boards genuinely lack it: the ReBarUEFI wiki says Haswell-era boards often have no 4G decoding option ([wiki](https://github.com/xCuri0/ReBarUEFI/wiki/Enabling-hidden-4G-decoding)).

Parse the IFR question by question. Do not `grep` for the prompt text and trust the offset on the matching line: on the reference dump the grep hit was the last line of the *previous* question's help text ("Aperture Size", offset `0x993`), which is not the gate (verified). The method is in [02-does-my-hardware-qualify.md](02-does-my-hardware-qualify.md) and [04-procedure.md](04-procedure.md).

## 3. Why does my OEM hide this setting?

No vendor statement turned up in the research for this repo. What the evidence shows:

- Servers tend to expose it. Dell PowerEdge documents "Memory Mapped I/O above 4 GB" and Lenovo ThinkSystem documents "PCI 64-Bit Resource Allocation" (both seen only in search snippets, reported, unconfirmed).
- On the M720q the question is present in the image and gated by a `SuppressIf`. It is hidden by form logic, not removed (verified).

Why a vendor would do that is inference: the machine was sold as an office PC with no big-BAR card in mind, so the option was never validated; fewer "no display after I changed MMIO" support calls; one BIOS shared across several SKUs. None of that is documented.

What it means for you: the vendor never tested your configuration. The risk is yours.

## 4. Will it work on my model?

Unknown until you read your own BIOS image. What is known:

| Machine | What is known | Status |
|---|---|---|
| Lenovo ThinkCentre M720q Tiny, BIOS M1UKT72A | Both gates found, enabled from Linux, A2 16 GB booted and ran | Verified on the reference build |
| Lenovo M720s/t, M920q/s/t/x, ThinkStation P330 Tiny | Same vendor BIOS package as the M720q (its readme lists these models). Offsets are unknown; nobody has reported them | Untested. Derive offsets from your own image |
| Lenovo ThinkCentre M90q Gen 1, BIOS M2WKT63A | Different BIOS family. A user asked whether the M720q offsets apply; the GPU was "completely absent from the PCIe bus"; no answer in the [thread](https://winraid.level1techs.com/t/117014) | Unknown |
| Lenovo ThinkStation P330 tower | Same `PCI_COMMON` size and visible items as the M720q. A user says the tool "will accept the 'enable' flag fine, but then disables it at restart" ([thread](https://winraid.level1techs.com/t/104196)) | Reported. Fits the checksum guard in question 10, not proven |
| Lenovo ThinkStation P310 | `PCI_COMMON` byte 3 reads 1 but GPU-Z still reports no 4G decoding ([thread](https://winraid.level1techs.com/t/94916)) | Reported, mixed |
| Dell Precision T3620, BIOS 2.33.0 | `PCI_COMMON` byte 3 edit stuck; first boot "appeared dead" until a CMOS battery pull ([discussion](https://github.com/xCuri0/ReBarUEFI/discussions/211)) | Reported |
| Dell OptiPlex Micro and SFF (7060, 7070) | No primary evidence that the option is hidden or exposed | Unverified |
| HP EliteDesk 800 G5 SFF | No option in Setup; `setup_var.efi` returned `ACCESS_DENIED`; a Tesla T4 gave a black screen ([thread](https://winraid.level1techs.com/t/96245)) | Reported: variable write blocked |
| HP EliteDesk 800 G4 SFF | A Tesla P4 worked after booting a cheap card first, resetting the BIOS and setting the integrated GPU as primary and cutting its memory to 64 MB. No hidden option was involved; the mechanism is unknown (same thread) | Reported |
| HP EliteDesk/ProDesk Mini | No standard PCIe slot | Not a candidate without an adapter |

The same table with sources per row lives in [02-does-my-hardware-qualify.md](02-does-my-hardware-qualify.md). If you try another model, open a hardware report (`.github/ISSUE_TEMPLATE/hardware-report.md`) with the model, BIOS version, store sizes and what happened. Leave out serial numbers and MAC addresses.

## 5. Will it void my warranty?

This repo assumes the hardware is already out of warranty and past end of life. If yours is under warranty, leased, or managed by an IT department, do not use it.

A settings change is not a hardware modification, but warranty terms belong to the vendor and this repo has not read any vendor's. Assume a board that stops booting after a bad write is yours to fix.

## 6. Is it safe? Can it brick the machine?

It writes data, not firmware. Nothing on the reference build needed recovery, which is a thin base and not a promise. It is not risk-free.

**What changes.** A few bytes in UEFI variables (`Setup`, `PCI_COMMON`, and on Lenovo `LastCheckSum`). No SPI flash write, no firmware code change. That is why flash-time protections such as Boot Guard and BIOS signature checks, which stop modified images from booting, are not involved (inference: a variable write alters no firmware code).

**What has gone wrong:**

| Hardware | What happened | Recovery | Source |
|---|---|---|---|
| Lenovo M720q (reference) | First attempt wrote the wrong byte (`PCI_COMMON` byte 0). The box kept booting, the card still looped at POST, and the byte stayed set | None needed. The recipe never writes byte 0 | Verified |
| Dell Precision T3620 | `PCI_COMMON` write: first boot appeared dead | CMOS battery pull; the edit persisted | [Reported](https://github.com/xCuri0/ReBarUEFI/discussions/211) |
| Dell G5 15 5590 laptop | Typo while changing RAM frequency with `setup_var_3`: no RAM detected, no display, RTC resets did not help | Reply advised an SPI programmer and a valid dump | [Reported](https://winraid.level1techs.com/t/103135) |
| ASUS TUF X570-PLUS | SR-IOV on, EFI boot, CSM off, Tesla T4 installed: board no longer POSTed | Not stated | [Reported](https://forums.developer.nvidia.com/t/tesla-t4-pci-i-o-region-invalid/120643) |

**What the failures have in common.** Writing a field you did not verify, or one that is not about PCI at all (memory timing, voltage). Writing the wrong width: [`setup_var.efi`](https://github.com/datasone/setup_var.efi) takes a size in bytes, so writing the value 1 with a size of 8 against a one-byte field also zeroes the seven bytes after it. And a CMOS clear is not a reliable undo. In the Dell G5 thread the helper points out that these settings sit in NVRAM "in the same SPI chip" and "won't get reset by CMOS jumper".

**Never delete a variable.** The kernel's [efivarfs documentation](https://docs.kernel.org/filesystems/efivarfs.html) says non-standard variables are created immutable so they are not removed by accident, because removing them can leave the firmware unable to POST. Writing needs `chattr -i`, and the write must keep the same length and the same attributes.

**The reference box had no out-of-band recovery** (reported, from an earlier probe), so a dead boot would have needed an SPI programmer (inference). That is why the sequence is: back up, write, reboot *without* the card, verify, then fit the card.

Recovery steps, in order of cost, and what to do per symptom: [05-safety-and-recovery.md](05-safety-and-recovery.md).

## 7. Do I need to open the case?

To fit the card, yes. To change the setting, no.

- The card needs a slot, a riser, or an adapter (M.2 to PCIe, OCuLink, Thunderbolt/USB4 dock). Machines with no slot, such as the HP EliteDesk/ProDesk Mini line, do not qualify without one.
- On the M720q, riser listings say the slot carries one PCIe x8 link from the CPU, and a low-profile card needs a riser and a 135 W Lenovo adapter (reported, unconfirmed). The A2 ran at Gen3 x8 in an x8-electrical slot (verified).
- The setting change itself is software. On the reference build the image analysis ran on the AI agent's own computer, and the live variable reads and writes ran over SSH. The owner's only hands-on steps were installing the card and taking it out again.
- Order matters. If the card is already installed and POST loops on "PCI Out of Resources", pull it first to get back into the OS. Starting with the card out means opening the case once.
- Boards whose variables are boot-services-only (no runtime access, for example the X299 board in question 1) need a one-shot UEFI shell, which means a keyboard, a monitor and someone at the machine.

## 8. Will any datacenter GPU work?

No. Above 4G Decoding is necessary for a large-BAR card in these machines. It is not always sufficient.

**BAR size and placement.** The only BAR sizes with a source are the A2 (16 GB, verified) and the P100 (16 GB, reported). No source was found for the T4, P4, L4 or P40, so do not assume 16 to 32 GB for every card. Read it from your card with `nvidia-smi -q -d MEMORY`.

Cards that still failed with the option on:

- Dell Precision T7820, Tesla P40: Above 4G exposed and enabled, NVIDIA driver reports BAR1 to BAR5 as "0M @ 0x0". The card POSTed only after BIOS virtualization options were disabled ([thread](https://winraid.level1techs.com/t/102347), reported).
- Gigabyte GA-X99-UD3P, four Tesla P100: the option toggles but not all cards boot ([thread](https://winraid.level1techs.com/t/117195), reported).

**Power and cooling.** Most datacenter cards are passively cooled and assume server airflow. In the 1-litre M720q chassis the A2 plateaued at 85 to 86 C with a cooler fitted and a 40 W power limit (verified). Check the slot's electrical width and the power adapter before buying a card.

**Display.** Most of these cards have no video outputs. The machine's integrated GPU keeps driving the screen.

**Driver branch.** NVIDIA's open kernel modules "can be used on any Turing or later GPU" ([README](https://github.com/NVIDIA/open-gpu-kernel-modules)). The 580 series is the last driver branch to support Maxwell, Pascal and Volta, and CUDA 12.x is the last toolkit line for them ([Phoronix, 2025-07-01](https://www.phoronix.com/news/NVIDIA-580-Linux-Driver-Last-HW)).

| Card | Architecture | Driver path |
|---|---|---|
| A2 | Ampere | Open modules work (reference build used `nvidia-open` 610.57.04, verified) |
| T4 | Turing | Open modules supported |
| L4 | Ada | Open modules supported |
| P4, P40, P100 | Pascal | Proprietary driver, 580 branch or older |

## 9. Can I do it from Windows?

Partly, and this repo has no verified Windows recipe. The reference build used Linux.

- The API is `SetFirmwareEnvironmentVariableEx` (or the older non-Ex form). It needs the `SE_SYSTEM_ENVIRONMENT_NAME` privilege, the GUID as a `{...}` string, and a UEFI-mode install; on legacy-BIOS installs it always fails ([Microsoft docs](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setfirmwareenvironmentvariableexa)). `msinfo32` shows "BIOS Mode".
- It reaches only variables the firmware exposes at runtime. On the X299 board, Windows returned error 203 for both `Setup` and `PCI_COMMON`, which fits stores that lack the runtime attribute. Those need the UEFI-shell route. Whether Windows sees the M720q's variables (attributes `0x7`) was not tested.
- Two hazards from the same page. A size of zero **deletes** the variable. And changing a variable's attributes means deleting and recreating it, which this repo never does. The Ex form makes you pass the attributes; they must match the original exactly (`0x7` on the M720q). What the non-Ex form defaults to was not verified.
- The practical route from a Windows PC is a Linux live USB booted in UEFI mode, which should expose the same efivarfs interface (inference, not tested here).

## 10. What is the checksum error (0162) about?

On Lenovo AMI builds, Error 0162 "Setup data integrity check failure" means the firmware found that `Setup` no longer matches a checksum it stored. It is expected to respond by reloading BIOS defaults, which undoes your edit (from image analysis; the error was never provoked on the reference build).

- **The guard (verified on the reference BIOS by decoding the PEI module's bytes).** `SetupCheckSumPei` sums the bytes of `Setup` into 16 bits and compares the result exactly with `LastCheckSum`: 2 data bytes, little-endian, under the Lenovo GUID `E102C8AA-0F10-4A0E-90A7-A6F816089BB7`, **not** the `Setup` GUID. The 0162 message and the defaults reload belong to a later (DXE) stage that was not re-decoded.
- **The gap is not constant.** The stored value minus the OS-visible sum was +1 on 2026-09-04 and 0 on 2026-10-07, on the same machine. Measure it live and write `sum16(new Setup) + delta`, never the plain sum. Refuse to continue if `delta` is anything other than 0 or 1. Write `Setup` and `LastCheckSum` back to back, then `PCI_COMMON`; if the second write fails, restore the original `Setup` at once from your backup. Never delete `LastCheckSum`. This repo offers no way around the guard.
- **If you see 0162,** the BIOS is expected to have loaded defaults already (never observed on the reference build). Pull the card if it is installed, then restore your backup or redo the write with a fresh delta. A reload is also expected to put other settings, such as CSM and Secure Boot, back to their defaults, so check them again ([04](04-procedure.md)). Save Changes and Exit clears the entry from the POST log; Lenovo's [POST error 0162](https://pubs.lenovo.com/st50/POST_ERROR_0162) page says so, though it is for the ThinkSystem ST50 server, not a ThinkCentre. Do not retry blindly.
- **Other vendors.** No checksum guard was seen on the X299 board (written bytes survived a full power cycle). None is documented for Dell or HP in anything found. The P330 report in question 4 is consistent with this guard and does not prove it.

The mechanism, the read-only measurement script and the failure handling are in [03, section 3](03-how-it-works.md#3-the-vendor-integrity-guard).

## 11. Which of the two settings actually did the work?

Not established. The recipe sets two things: `Setup[0xA49]` ("Above 4GB MMIO BIOS assignment") and `PCI_COMMON[3]` ("Above 4G Decoding"). On the reference build they were changed in the same run, so the effect of each was never isolated.

- **Points at the `Setup` gate:** the new 64-bit root window, `[mem 0x4000000000-0x7fffffffff window]` (256 GB), appeared at the first boot after the change, before any card was installed (verified). An earlier disassembly of the PCI bus driver found it reads only bytes 0 and 5 of `PCI_COMMON` in this build (reported, not re-run).
- **Points the other way:** byte 3 of `PCI_COMMON` was the field edited on a Dell T3620 and the X299 board. The ReBarUEFI wiki says the `PCI_COMMON` store usually does not enable 4G decoding even though it contains the setting, and that some platforms also need Above 4GB MMIO assignment ([wiki](https://github.com/xCuri0/ReBarUEFI/wiki/Enabling-hidden-4G-decoding)).
- **The test that would settle it:** revert byte 3 alone from a backup, reboot without the card, read `/proc/iomem` for the 64-bit window, then fit the card. Nobody has run it. Until then treat both stores as candidates and judge by the physical outcome.
- **Do not write `PCI_COMMON` byte 0.** The first attempt wrote it on the AMI "first field is byte 0" guess. It changed nothing visible, it stayed set, and what it controls is unknown.

The full reasoning is in [03, section 2.5](03-how-it-works.md#25-the-two-gates-and-what-is-not-proven); the story of both attempts is in [06-reference-build.md](06-reference-build.md).

## 12. The value reads back correctly but nothing changed. Why?

A value echoing back proves the firmware accepted the write. It proves nothing about behaviour. Causes seen or reported:

- **Wrong field.** On the reference build the byte-0 write persisted across a power cycle and the card still looped (verified). On a ThinkStation P310, byte 3 reads 1 while GPU-Z reports no 4G decoding (reported).
- **Nothing consumes the setting.** The [Hidden BIOS settings lesson](https://github.com/Kostr/UEFI-Lessons/tree/master/Lessons_uncategorized/Lesson_Hidden_BIOS_settings) warns that a changed option may not be used by driver code.
- **A guard reverts it.** See question 10.
- **Firmware restores settings.** A Japanese HP FAQ sample alert says Sure Start restored "critical BIOS settings" after an unauthorized change (search snippet only). Whether it reverts variable edits is unconfirmed.

Check the outcome, with the card out, after the reboot:

```
dmesg | grep -i -E 'root bus resource|can.t assign|no space'
cat /proc/iomem          # a 64-bit window above 0x100000000
```

On the reference build the line was `pci_bus 0000:00: root bus resource [mem 0x4000000000-0x7fffffffff window]`. That line only appears if the firmware's ACPI `_CRS` publishes the window; `pci=nocrs` was not needed there. After fitting the card:

```
lspci -vv -s <bus:dev.fn>     # Region lines: 64-bit, prefetchable, address above 4 GB
nvidia-smi -q -d MEMORY       # BAR1 total
```

## 13. Does the change survive power cycles, CMOS clears and BIOS updates?

- **Power cycles and reboots:** yes on the reference build. The `Setup` byte, `PCI_COMMON` and `LastCheckSum` persisted, and were still set when last read on 2026-10-07, about a month later and after the GPU, RAM and SSD had been changed. Nothing later than that date has been checked.
- **CMOS clear:** varies by board. The Dell T3620 edit survived a battery pull. The X299 board's `Setup` byte writes were recoverable by CMOS clear. The Dell G5 helper says NVRAM settings are not reset by a CMOS jumper. Do not count on it either way.
- **BIOS update:** not tested. An update may reset or rewrite the store, and offsets belong to a BIOS build. The offsets read from the M1UKT77A image held on a machine running M1UKT72A (6 of 6 matched, verified), but a different BIOS family (M90q) is a different layout. Re-verify after any flash.
- **Will the route stay open?** CERT/CC [VU#758382](https://kb.cert.org/vuls/id/758382) (CVE-2014-2961) described UEFI implementations that did not protect the `Setup` variable from OS writes, listing AMI, Dell, Insyde, Intel and Lenovo as affected. CERT/CC classed an OS-writable `Setup` variable as a vulnerability, so newer firmware may close this path (inference).

## 14. Why not flash a modified BIOS, or use ReBarUEFI?

Because on these machines flashing is the harder and riskier path.

- It needs an SPI programmer or a vendor flash tool, and the vendor tools resist. On the M720q a flash write returned "Error 167" ([thread](https://winraid.level1techs.com/t/38139)). A Dell T3620 user says Boot Guard "will not let my system POST" with a modified BIOS, and an HP user got "BIOS signature verification failed" (both reported).
- A variable write changes no firmware code, so those checks do not apply (inference).
- ReBarUEFI is a DXE driver that must be built into a modified firmware image. It solves Resizable BAR, not hidden Above 4G, though its [wiki](https://github.com/xCuri0/ReBarUEFI/wiki/Enabling-hidden-4G-decoding) documents the hidden-4G method this repo builds on. If you want real ReBAR on a locked board, flashing is the route, and its risk is separate from this repo's.

## 15. Can an AI agent do this?

Yes. On the reference build an AI coding agent did everything except the physical steps. It dissected the vendor BIOS image, extracted the IFR and found the checksum guard on its own computer. It then measured the delta, wrote and verified the variables with live reads and writes over SSH. The owner only installed the card and took it out again.

The agent was also wrong twice. It guessed the `PCI_COMMON` byte from the AMI "first field" convention, and it took a grep hit for the `Setup` offset. That cost one full cycle of pulling and reinstalling the card. Several guardrails in this repo target exactly those two mistakes.

If you hand this to an agent:

- Read-only stages (dump the variables, extract the IFR, scan for guards) are low risk to hand over.
- Require a human "yes" before the write, after a dry run shows exactly which bytes differ.
- Keep the agent to offsets from a verified IFR of your exact BIOS build. No guessing from other boards.
- No deleting variables, no changing attributes, no writes to fields that are not on the verified list.
- Physical steps stay with you. An agent cannot fit the card, and a reboot without the card is a step to confirm yourself.
- Point it at [04-procedure.md](04-procedure.md) and [05-safety-and-recovery.md](05-safety-and-recovery.md) so it has the stop conditions.
