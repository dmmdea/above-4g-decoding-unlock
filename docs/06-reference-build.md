# Reference build: ThinkCentre M720q Tiny and an NVIDIA A2 16 GB

A Lenovo ThinkCentre M720q Tiny (a 1-litre office PC) would not boot with a 16 GB NVIDIA A2 installed. Every start ended at *"PCI OUT OF RESOURCES CONDITION: Insufficient PCI Resources Detected"*, and the BIOS menu had no Above 4G Decoding option to switch on. This page follows the fix from that first failed boot to a card running under load, including the attempt that did not work and why.

Two hidden firmware settings were changed, together with the vendor checksum that guards one of them. Which of the two settings was necessary is not proven (section 4). The firmware analysis and every variable write were done remotely by an AI coding agent: the image analysis on its own machine, the live reads and writes over SSH. The owner's hands-on part was installing the card and taking it out again.

> **This edits firmware settings on out-of-warranty, end-of-life hardware.** The method writes UEFI variables only. It does not touch the flash chip or firmware code. A bad write can still reload BIOS defaults or stop the board from POSTing. Read [05-safety-and-recovery.md](05-safety-and-recovery.md) before you write anything.

## How to read the evidence labels

- **Verified**: read from a live system or from the firmware image during this work.
- **Reported**: taken from a public thread, or from earlier analysis that was not repeated when this page was written.
- **Unproven**: a plausible explanation that nobody tested.

The console logs of the first attempt were not kept. Its details come from notes written the same day, and the exact command cannot be recovered. The second attempt is documented from the script that ran.

## 1. The machine and the symptom

| | |
|---|---|
| Machine | Lenovo ThinkCentre M720q Tiny |
| BIOS on the machine | M1UKT72A |
| BIOS image analysed | M1UKT77A, the newest at the time. For 73A to 77A the vendor readme lists security fixes, a CPU microcode update, BIOS-update fixes and a new TFTP setting, and nothing PCI-related |
| Boot configuration | UEFI only (CSM off), Secure Boot disabled |
| OS and driver | Ubuntu Linux, `nvidia-open` 610.57.04 |
| Card | NVIDIA A2, 16 GB, passive heatsink, PCI ID `10de:25b6`, one 16 GB BAR |
| Slot | PCIe, x8 electrical |
| Symptom | POST halts with "PCI OUT OF RESOURCES CONDITION". With the card out, the machine boots normally |

A 16 GB BAR cannot fit in the 32-bit MMIO space below 4 GB, so the firmware has to place it above that line. Boards that show the option keep it on AMI's "PCI Subsystem Settings" page. On this machine, Devices > PCI Express Configuration shows only ASPM and the PCIe slot speed, and the page with Above 4G Decoding is not exposed (from the notes made at the time).

## 2. Who did what

| Step | Who |
|---|---|
| Install the A2 (boot loop), remove it | Owner |
| Download and dissect the firmware image, find the checksum guard | Agent, on its own machine |
| Read the live variables | Agent, over SSH |
| First variable write | Agent, over SSH |
| Install the A2 again (same boot loop), remove it | Owner |
| Second variable write, reboot, verification | Agent, over SSH |
| Install the A2 a third time (boots) | Owner |
| Driver check, load tests, power limit | Agent, over SSH |

No boot stick, jumper or flash programmer was involved. The cooling hardware fitted after the unlock was a separate physical job and is not part of it.

## 3. Taking the firmware apart

### Getting an image

No SPI dump was needed. Lenovo publishes BIOS updates as a bootable ISO (`m1uj977usa.iso` for this family), and the image inside it, `IMAGEM1U.ROM`, is the whole flash image: 12,582,912 bytes. The readme in the ISO says the same image serves the M720t/s/q, M920t/s/q/x and ThinkStation P330 Tiny (verified).

The image was newer than the BIOS on the machine. A read-only audit afterwards compared six offsets from the analysis with the variables on the running M1UKT72A, and they agreed (verified). Do that comparison yourself before writing anything. Matching sizes alone do not prove matching layouts.

### Dissecting it

The tools are [UEFITool / UEFIExtract](https://github.com/LongSoft/UEFITool) and [IFRExtractor-RS](https://github.com/LongSoft/IFRExtractor-RS).

```sh
UEFIExtract IMAGEM1U.ROM report     # text listing of every volume, file and GUID (about 3,000 lines here)
ifrextractor Setup.bin              # readable list of every Setup question in one extracted module
```

Dumping the whole tree with UEFIExtract's `all` mode failed on Windows with a path-length error (reported, project notes). Run it from a directory near the drive root, or on Linux.

The modules that mattered:

- `Setup`: every Setup question with its store and byte offset. The `Setup` variable is `0x1388` (5,000) bytes.
- `PciDynamicSetup`: the static `PCI_COMMON` questions only, Hot-Plug at offset `0x2` and BME DMA Mitigation at `0x5`. Above 4G Decoding is not among them.
- `PciBus`: the driver that consumes `PCI_COMMON`. It was disassembled to see which bytes it reads.
- `SetupCheckSumPei`: the checksum guard (section 5).

### Reading the IFR

A question the vendor hides from the menu is still in the IFR, with its offset, unless the vendor removed it from the image. Here the entry was present and the page selection hid it. The method is in [04, step 5 of 0.5](04-procedure.md#05-derive-your-own-offsets).

Two searches produce wrong offsets on this image. Both were hit during the work:

| Search | What it returns | Why it misleads |
|---|---|---|
| `grep "Above 4GB MMIO"` | A line whose `VarOffset` is `0x993` | That line is the last line of the help text of **Aperture Size** (QuestionId `0x2746`), and it carries Aperture Size's own `VarOffset`. The question you were looking for comes next |
| `grep 0xA49` | Four unrelated hits | `0xA49` is also a QuestionId, for "Intel Speed Shift Technology", whose `VarOffset` is `0x6EE` |

Parse `VarOffset` per question and read the `Prompt:` that owns it. Do not search for a phrase and take the nearest number.

### What the image said

| Store | GUID | Size | Offset | Question | Default |
|---|---|---|---|---|---|
| `Setup` | `EC87D643-EBA4-4BB5-A1E5-3F3E36B20DA9` | `0x1388` | `0xA49` | "Above 4GB MMIO BIOS assignment" (QuestionId `0x54`), Enabled = 1, Disabled = 0 | Disabled |
| `Setup` | same | `0x1388` | `0x993` | "Aperture Size" (QuestionId `0x2746`): 0 = 128 MB, **1 = 256 MB**, 3 = 512 MB, 7 = 1024 MB, 15 = 2048 MB (read from the IFR) | 256 MB |
| `PCI_COMMON` | `ACA9F304-21E2-4852-9875-7FF4881D67A5` | 7 | 3 | "Above 4G Decoding": dynamic question, no static entry | 0 |

The `0xA49` option is hidden from the menu whenever Aperture Size is 2048 MB. The Aperture Size help text mentions the Above-4GB option and says CSM must be disabled to use it. The help text requirement is verified in the IFR; CSM was already off on the reference machine (from the notes made at the time).

### Finding byte 3 of PCI_COMMON

Nothing in the static IFR points at it, so the offset was triangulated. It is an inference, not a lookup:

- Public dumps of boards using the same store GUID put "Above 4G Decoding" at byte 3: a Dell Precision T3620 ([discussion](https://github.com/xCuri0/ReBarUEFI/discussions/211)) and a Lenovo P310 ([thread](https://winraid.level1techs.com/t/94916)) (reported).
- When `PCI_COMMON` does not exist, the PCI bus driver builds its own defaults with byte 3 set to 1, while Lenovo's stored default for the byte is 0 (reported: earlier disassembly, not re-run).
- A Lenovo SMM module in the image (GUID `B6E17DD5-5CA0-4E3A-858C-A14345AE0B83`) reads `PCI_COMMON`, flips byte 3 and writes it back. It is the only PCI_COMMON byte that Lenovo's own settings interface toggles. The module's presence in the image is verified; what it does is reported: earlier disassembly, not re-run.

## 4. Two gates

| Gate | Where | What it appears to do |
|---|---|---|
| **System Agent**: "Above 4GB MMIO BIOS assignment" | `Setup[0xA49]` | Gives the root bridge a 64-bit MMIO window at all (inferred from the name and from the result in section 7) |
| **AMI PCI**: "Above 4G Decoding" | `PCI_COMMON[3]` | The setting that boards exposing the option show in the menu |

The second attempt changed both gates and the checksum in one go. Which gate was necessary was therefore never isolated.

What the evidence says:

- The new 64-bit root window appeared on the first boot after the writes, **before any card was installed**. A root window is a host-bridge resource. That points at the Setup gate.
- The disassembly of the PCI bus driver found it reads only two bytes of `PCI_COMMON`: byte 0 and byte 5. It does not read byte 3 (reported: earlier disassembly, not re-run). Another consumer, such as the Lenovo SMM interface above, could still read it.

Byte 3 caused no visible harm on this build and is the setting reported on other boards. Whether it was necessary here is **unproven**. The test that would settle it is to set byte 3 back to 0 with a backup, reboot with the card out, and check whether the window survives. It has not been run.

## 5. Lenovo's Setup checksum guard

### Finding it

PEI modules are stored uncompressed in the image, so a byte search finds their strings. DXE modules are compressed and do not appear:

```sh
LC_ALL=C grep -obUaP 'L\x00a\x00s\x00t\x00C\x00h\x00e\x00c\x00k\x00S\x00u\x00m\x00' IMAGEM1U.ROM
```

On this image there was exactly one hit, near the end of the flash. Mapping that offset onto the UEFIExtract report identifies the module: a PEI module named **SetupCheckSumPei**, GUID `76D1059D-2E22-40EF-A31F-C49DBBC0DBCE` (verified).

### What it does

Hand-decoded from the 32-bit x86 bytes, without a disassembler. The structure is solid and the instruction-level detail is moderate-confidence:

1. Reads the `Setup` variable (`0x1388` bytes) through what appears to be the PEI read-only variable service.
2. Adds every byte into a 16-bit accumulator. No carry fold, no complement.
3. Reads the 2-byte variable `LastCheckSum`.
4. Compares the two for **exact equality**.
5. On a mismatch, if a Lenovo PPI with signature `0x55AA` is present, calls it to set a flag (the call passes 5, 1 and 0x55; what those arguments mean is inferred). DXE modules identified from the report (an error-message module, a setup-defaults loader and a driver named for a Setup data checksum error) turn that flag into **Error 0162** and a **reload of BIOS defaults**. The PEI side and the module names are verified. The DXE behaviour is from earlier analysis, and the message text lives in compressed DXE volumes and was not read.
6. The only skip in the decoded bytes is for boot mode `0x20`, which the UEFI PI specification assigns to recovery. The constant is from memory and was not re-checked against the specification or EDK2's [PiBootMode.h](https://github.com/tianocore/edk2/blob/master/MdePkg/Include/Pi/PiBootMode.h). Whether the check also behaves differently on S3 resume was not determined.

No Error 0162 was ever shown on the reference machine. The failure path comes from the firmware analysis, not from observation.

`LastCheckSum` is a Lenovo variable under GUID **`E102C8AA-0F10-4A0E-90A7-A6F816089BB7`**, 2 data bytes, little-endian, attributes `0x7`. It is not under the Setup GUID (verified: ROM bytes and the live variable listing).

### The relation is not constant

The obvious plan is to write `LastCheckSum = sum16(new Setup)`. On the day that would very likely have been off by one, because the checksum the OS computes from the `Setup` file and the stored `LastCheckSum` were **not equal on a healthy box**:

| When | Stored `LastCheckSum` minus OS-visible `sum16(Setup)` | Error 0162? |
|---|---|---|
| Day of the unlock, before the write | +1 | No |
| Day of the unlock, after the write | +1 (preserved on purpose) | No |
| About a month later, after the GPU, RAM and SSD had been changed | 0 | No |

The mechanism is unknown. Candidates, none tested: a byte in `Setup` that the firmware itself maintains, or the PEI view of `Setup` differing from the OS view. The only safe reading is that **the relation is a property of the machine at that moment**. Measure it live, preserve it, and never call it constant. The rule used was:

```
delta       = (stored LastCheckSum - sum16(current Setup)) mod 65536
new stored  = sum16(new Setup) + delta
```

The original one-off script refused to run unless the delta was 0 to 2. That bound was a heuristic, not derived. The tools in this repo accept 0 or 1 by default. Anything outside the range seen on your own board means your model of the guard is wrong, so stop.

A second 2-byte variable, `LastSetupdataSum`, exists on the machine. The reference script never touched it and the box booted clean, so it was not part of the exercised path. Its role is unknown.

### Writes are not atomic

Setup, LastCheckSum and PCI_COMMON are three separate writes. If the process dies after the first and before the second, the next boot sees a mismatch, raises Error 0162 and reloads defaults. So the order is Setup and LastCheckSum **back to back**, then PCI_COMMON. That keeps the window short. It does not close it.

A defaults reload loses custom BIOS settings and also resets CSM and Secure Boot to their defaults, so re-check them. It is expected to be recoverable, but it was never observed on this build. The machine had no out-of-band recovery path (reported: earlier probe) and was being worked on remotely, so a failure that needed hands would have meant a trip to the machine.

## 6. First attempt: the wrong offset

The first notes recorded two offsets from the image:

- `PCI_COMMON[0]` as "Above 4G Decoding", on the AMI convention that the first field of the store is the feature. This was a guess and was marked as one.
- `Setup[0x993]` as "Above 4GB MMIO BIOS assignment". This came from the first search in section 3. `0x993` is Aperture Size.

The agent read `Setup[0x993]`, saw `1`, and took it as the option already being enabled. A `1` at that offset means "256 MB aperture", the factory default. So `Setup` was never written. The only change in the first attempt was one byte:

```
PCI_COMMON  00 00 01 00 00 00 00  ->  01 00 01 00 00 00 00
```

The write was done carefully. Both stores were visible in efivarfs with attributes `0x7`. The immutable flag was cleared, the whole blob went out in one write, the read-back matched, the value survived a power cycle, and the machine still booted normally without the card. Every one of those checks passed, and none tested what mattered.

The owner then reinstalled the card. Same boot loop. The card came out again. That cost a full physical round trip.

What would have caught it without touching the hardware:

1. A live read of the real gate. A read taken afterwards showed `Setup[0xA49] = 00`: the real gate had been off the whole time.
2. The root-bridge windows with the card out. They were all below 4 GB, and the note at the time recorded that as inconclusive because only small devices were present. In hindsight it was the answer. After the second attempt, the 256 GB window showed up with no card at all.

Byte 0 stayed at 1 on the reference machine, and was still 1 on 2026-10-07. Nothing broke, but nobody knows what it controls. The PCI bus driver registers a ReadyToBoot callback when it is 1 (reported: earlier disassembly, not re-run). **Do not write PCI_COMMON byte 0.**

## 7. Second attempt: from Linux, card out

The card was out, and the box was booting Linux. The steps below are what ran, with the commands made generic.

### Preconditions (checked)

- Both gates and the guard identified from the vendor image for this machine, with the live values read and matching what the image predicted.
- Secure Boot disabled and boot mode UEFI only.
- All three variables visible in efivarfs. `ls` shows `Setup` at 5,004 bytes, `LastCheckSum` at 6 and `PCI_COMMON` at 11: the 4 attribute bytes plus 5,000, 2 and 7 data bytes.

```sh
ls -l /sys/firmware/efi/efivars | grep -E ' (Setup|PCI_COMMON|LastCheckSum)-'
```

An efivarfs file is the four attribute bytes (little-endian) followed by the data. Writing one needs the attributes first, and the whole blob must go out in a single `write()` ([kernel documentation](https://docs.kernel.org/6.12/filesystems/efivarfs.html), reported). Most variables are created immutable, to protect against accidental deletion, so `chattr -i` clears the flag first.

### How the writes were made

The reference run used a one-off script, not the tools in this repo. It was 35 lines of shell with embedded Python. It copied the three variables to a backup directory first and aborted if a copy failed, measured the delta, refused to run unless the delta was 0 to 2, and then wrote `Setup`, `LastCheckSum` and `PCI_COMMON` in that order after `chattr -i`. Each write was the attribute bytes plus the whole value in one call, through Python's `open(path, 'wb')`, which opens with `O_WRONLY|O_TRUNC`. It asserted three read-back values. It had no BIOS-version check, no rollback and no scripted restore, and the restore from its backup was never exercised. It is not published here.

Do not copy it. The tools in [`tools/`](../tools/README.md) add the BIOS gate, a verified backup, the guard flags, the delta bound, the whole-blob diff and the rollback. Use them as described in [04, Phase 3.1](04-procedure.md#31-route-a-linux-efivarfs-preferred).

### Reboot with the card still out

The reboot command was not recorded (a plain reboot over SSH, as far as the notes show). About three minutes after the write, these checks were done:

```sh
E=/sys/firmware/efi/efivars
dmesg | grep 'root bus resource'
tail -c +5 "$E/PCI_COMMON-aca9f304-21e2-4852-9875-7ff4881d67a5" | xxd -p        # expect 01000101000000
tail -c +5 "$E/Setup-ec87d643-eba4-4bb5-a1e5-3f3e36b20da9" | xxd -s 0xA49 -l 1 -p   # expect 01
for f in "$E"/SetupDataChecksumError-* "$E"/RestoreSetupDataFlag-*; do
  echo "$(basename "$f" | cut -d- -f1): $(tail -c +5 "$f" | xxd -p)"              # expect 00
done
```

What came back (verified on the reference build):

```
pci_bus 0000:00: root bus resource [mem 0x4000000000-0x7fffffffff window]
```

That is a **new 256 GB window above 4 GB**. It came from the ACPI `_CRS`, so no `pci=nocrs` kernel parameter was needed. `Setup[0xA49]` and `PCI_COMMON` had persisted. `SetupDataChecksumError` and `RestoreSetupDataFlag` were both 0, and the BIOS event-log count variable was unchanged. There was no Error 0162 and no defaults reload.

The card was not in the machine for any of this. Only after this check did the owner power off and install it.

## 8. Card in

The first boot with the A2 passed POST. Checks from Linux (verified):

| Check | Result |
|---|---|
| `lspci -vv` | A2 (`10de:25b6`) present. Its 16 GB BAR is a 64-bit prefetchable region at `0x4400000000`, inside the new window. Its 32 MB BAR is at `0x5000000000` and a 16 MB BAR is below 4 GB |
| Driver | `nvidia-open` bound |
| `nvidia-smi` | Healthy: 15,356 MiB reported, P8 at idle, about 32 C, 5 to 6 W |
| Link | 2.5 GT/s x8 at idle, Gen3 x8 under load. The slot is x8 electrical |
| Other devices | On-board devices and an M.2 Edge TPU now received BARs above 4 GB. The TPU's driver kept working |

The kernel log also reported failures to assign the A2's SR-IOV virtual-function BARs. They did not affect use of the card.

## 9. Before and after

| Item | Before | After |
|---|---|---|
| `Setup[0xA49]`, "Above 4GB MMIO BIOS assignment" | 0 (Disabled, the default) | 1 |
| `PCI_COMMON[3]`, "Above 4G Decoding" | 0 | 1 |
| `PCI_COMMON[0]` | 0 | 1. Left over from the first attempt, never reverted, function unknown |
| `PCI_COMMON` as stored | `00 00 01 00 00 00 00` | `01 00 01 01 00 00 00` |
| `LastCheckSum` minus `sum16(Setup)` | +1 | +1, by construction. Stored value recomputed |
| Variable attributes | `0x7` | `0x7` |
| Root-bridge windows, card out | Only below 4 GB, for example `[mem 0x9f800000-0xefffffff]` | Adds `[mem 0x4000000000-0x7fffffffff window]`, 256 GB |
| Boot with the A2 | "PCI OUT OF RESOURCES" loop, twice | POST completes |
| A2 16 GB BAR | Never assigned | At `0x4400000000`, inside the new window |
| Driver | Not reachable: the machine never got past POST | `nvidia-open` bound, `nvidia-smi` healthy |
| Error 0162 / defaults reload | Not applicable | None: flags 0, event-log count unchanged |
| On-board devices and M.2 Edge TPU | BARs below 4 GB | BARs above 4 GB, drivers unaffected |

Every value in the right-hand column was observed on the reference build. The table cannot tell you which of the two gates was necessary.

## 10. After the unlock: a power cap, then another job

The A2 has a passive heatsink in a 1-litre case. Even with a cooler fitted, a 100 s synthetic CUDA loop at the stock 60 W limit reached 85 C and was still rising. A 40 W cap (`nvidia-smi -pl 40`, which does not persist across a reboot) plateaued at 85 to 86 C, the card's 86 C maximum operating temperature, for about 10% less throughput and about 20% less power (verified), so airflow was the limit.

About a month later the A2 left this machine for another job, and a smaller 6 GB consumer card took its place. A read-only check of the variables after that swap showed the unlock still in place on 2026-10-07: `Setup[0xA49]` = 1, `PCI_COMMON` = `01 00 01 01 00 00 00`, and no Setup checksum flags raised. Nothing later has been checked. The settings belong to the machine, not the card: they stayed in place when the card changed.

## 11. What is still unproven

1. **Whether `PCI_COMMON[3]` was needed.** The two gates were changed together. The test is described in section 4.
2. **Why the checksum relation moved from +1 to 0** without an Error 0162. Section 5 lists untested candidates, including the hardware changes made in between.
3. **What `LastSetupdataSum` is for.**
4. **What `PCI_COMMON[0]` controls** (it was still 1 on 2026-10-07), and whether the checksum check does anything different on S3 resume. The decoded bytes show a skip only for boot mode `0x20`.
5. **Whether other models sharing the image have the same layout.** The vendor readme says one image serves the M720t/s/q, M920t/s/q/x and ThinkStation P330 Tiny, but whether the layout matches is unknown, so derive your own offsets from your own image.
6. **The exact command of the first attempt** and of the reboots. They were not recorded.
7. **A dead first boot is a documented failure mode on other boards.** A Dell Precision T3620 user reports the first boot after enabling Above 4G Decoding looked dead and was cured by pulling the CMOS battery ([discussion](https://github.com/xCuri0/ReBarUEFI/discussions/211), reported, unconfirmed). Nothing like it happened on the reference build.
8. **A P330 tower thread reports** that a variable tool "will accept the 'enable' flag fine, but then disables it at restart" ([thread](https://winraid.level1techs.com/t/104196), reported). A Setup checksum guard reloading defaults would produce exactly that. That explanation is an inference and is not confirmed.
9. **A shell-route recipe for a board with a checksum guard and no runtime access.** None exists in this repo, and the shell route (see [04, 3.2](04-procedure.md#32-route-b-uefi-shell-with-setup_varefi)) is untested for that case.
10. **Dell OptiPlex, HP Mini/SFF and Intel NUC boards.** No primary evidence that the option is hidden or that the variables are writable. One HP EliteDesk 800 G5 report has the write refused with `ACCESS_DENIED` ([thread](https://winraid.level1techs.com/t/96245), reported).

## 12. What carries over to your board

The procedure in [04-procedure.md](04-procedure.md) is the part that carries over. The offsets and the checksum delta on this page belong to one machine and one BIOS build. For the general method see [03-how-it-works.md](03-how-it-works.md). For failure modes and recovery see [05-safety-and-recovery.md](05-safety-and-recovery.md). To judge whether your board is a candidate, start at [02-does-my-hardware-qualify.md](02-does-my-hardware-qualify.md).
