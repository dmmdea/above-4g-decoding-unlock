# The procedure: enabling Above 4G Decoding through the UEFI variables

Read [05-safety-and-recovery.md](05-safety-and-recovery.md) before you write anything. This page is the
sequence. That page is what to do when a step goes wrong.

This is for the owner of a machine, working on their own out-of-warranty hardware, using techniques the
resizable-BAR community has published for years. It changes UEFI variables and nothing else: no flash chip
is written and no firmware code is modified. The change is two bytes plus, on some vendors, a 16-bit checksum.

The writes themselves are made by the scripts in [`tools/`](../tools/README.md). This page is the sequence
around them: derive and check your offsets, build a profile, run the tools, then check the result.

Each phase ends in a gate. Do not start the next phase until the gate passes.

## Evidence labels

Every non-obvious statement carries one of these.

| Label | Meaning |
|---|---|
| **[verified]** | Observed on the reference build between 2026-09-04 and 2026-10-07. |
| **[reported]** | Someone else's report, or a document we read. Not reproduced here. |
| **[unverified]** | Inference, or not checked. Do not treat it as fact. |

Third-party claims were taken from the linked pages as summarised. Re-open the page before you rely on a
detail that matters to you.

## The reference build

A Lenovo ThinkCentre M720q Tiny running BIOS M1UKT72A, with an NVIDIA A2 16 GB card. The card made POST
loop on "PCI OUT OF RESOURCES CONDITION: Insufficient PCI Resources Detected", because a 16 GB BAR cannot
be placed below 4 GB and Lenovo hides the menu that would fix it.

An AI coding agent did everything except the physical steps. It ran the image analysis on its own computer
and made the live variable reads and writes over SSH. The owner only installed the card and removed it
again. **[verified]**

Result: with the card still out, the first reboot after the writes showed a new 256 GB root window above
4 GB. With the card in, its 16 GB BAR landed inside that window and the NVIDIA open kernel module bound
normally. **[verified]**

### Reference values

Every call-out in this page uses these. They belong to this one board and BIOS family.

| Store | GUID | Data size | Offset | Meaning | Before | After |
|---|---|---|---|---|---|---|
| `Setup` | `EC87D643-EBA4-4BB5-A1E5-3F3E36B20DA9` | 0x1388 (5000) | 0xA49 | "Above 4GB MMIO BIOS assignment" (QuestionId 0x54) | 0 | 1 |
| `LastCheckSum` | `E102C8AA-0F10-4A0E-90A7-A6F816089BB7` | 2, little-endian | n/a | Lenovo's 16-bit checksum of `Setup` | 0xDC24 | 0xDC25 |
| `PCI_COMMON` | `ACA9F304-21E2-4852-9875-7FF4881D67A5` | 7 | 3 | "Above 4G Decoding" | 0 | 1 |

All three variables had attributes `07 00 00 00` (non-volatile, boot-service, runtime). **[verified]**

> **Do not copy these offsets to another board.** Offsets move with every BIOS build and every model. The
> reference build's own first attempt used the wrong ones and wasted a full pull-card, boot, write,
> reinstall cycle (Phase 0, step 5). The tools in `tools/` have no built-in offsets for that reason: every
> offset comes from a profile file that you write from your own firmware (Phase 1.4 and 3.1).
> `tools/profile.example.json` holds the values above, is marked as an example, and ships with
> `verified_by_user` set to `false`.

## Phase map

| Phase | You do | Gate to pass |
|---|---|---|
| 0 | Set firmware options, fingerprint the board, take the card out, derive your own offsets | A filled-in offset table with a source line for every entry |
| 1 | Read and back up every related variable | Backups re-read and identical; baseline PCI windows recorded |
| 2 | Check attributes, find guards, measure the checksum relation | Every variable writable and unauthenticated; guard modelled or absent |
| 3 | Write the bytes (efivarfs, or UEFI shell) | Read-back matches the plan exactly |
| 4 | Reboot with no card | A new root window above 4 GB; values persisted; no integrity-error flags |
| 5 | Install the card | The BAR sits inside the new window and the driver binds |

## Global stop rules

1. A value, size or attribute that differs from what you expected is a stop, not a nuisance to work around.
2. Any non-zero result from a write is a stop. Restore from backup before anything else. Never retry with
   different attributes or a different length. Never delete or recreate a variable.
3. Do not skip the no-card reboot in Phase 4. It is the one point where a mistake cannot be blamed on the
   card.
4. If you cannot say where an offset came from, you do not have an offset.

---

## Phase 0. Prerequisites

### 0.1 Firmware options: UEFI only, Secure Boot off

Set these in the BIOS menu before anything else.

- **Boot mode: UEFI only, CSM off.** The reference IFR help text for the aperture option says to disable
  CSM to use it. **[verified]** The ReBarUEFI wiki warns that enabling 4G decoding with CSM on can give a
  black screen or lock you out of setup. **[reported]**
  ([wiki](https://github.com/xCuri0/ReBarUEFI/wiki/Enabling-hidden-4G-decoding))
  A Windows install booted in legacy/MBR mode may stop booting once CSM is off. This page assumes Linux
  installed in UEFI mode and does not cover that conversion.
- **Secure Boot: disabled.** The UEFI-shell route runs unsigned binaries and needs it. The efivarfs route
  runs no unsigned EFI code; whether Secure Boot state affects the write itself is untested. **[unverified]**
  The reference build had it disabled throughout. Leave it off until you are finished.

If a later step makes the firmware reload defaults (Error 0162, below), these settings go back to their
defaults too. Check them again after any reload.

### 0.2 Confirm a UEFI boot and fingerprint the board

Boot Linux from the installed system or a live USB, in UEFI mode, and get a root shell.

```bash
test -d /sys/firmware/efi && echo "UEFI boot" || echo "legacy boot: STOP"
E=/sys/firmware/efi/efivars
mount | grep -q efivarfs || sudo mount -t efivarfs none "$E"
mount | grep efivarfs
```

Record the board and BIOS build. These files are safe to paste; the serial and UUID files in the same
directory are not, so do not run `cat /sys/class/dmi/id/*`.

```bash
for f in sys_vendor product_name board_name bios_vendor bios_version bios_date; do
  printf '%-13s %s\n' "$f" "$(cat /sys/class/dmi/id/$f 2>/dev/null)"
done
```

The `bios_version` string is what your offsets must be tied to. The reference board reported `M1UKT72A`.

### 0.3 Take the card out and keep it out

Test the whole procedure with the GPU removed. If the first reboot after a write misbehaves, you then
know the write is the cause and not the card. The reference build's first attempt went straight from a
write to reinstalling the card: it got the same POST loop as before and the cycle explained nothing. A
look at `/proc/iomem` with the card out would have shown in minutes that no new window had appeared.
**[verified]**

With the card in and its BAR unplaceable, POST on the reference board never finished, so there was no
Linux to run any of this from.

### 0.4 Power and other firmware writers

- Use a stable AC supply. Variables normally live in the board's flash chip, so a write is a flash write.
  A laptop must be on charge. No power-loss failure was observed on the reference build; this is
  insurance, not a measured risk.
- Stop anything else that writes firmware variables, for example the firmware updater:

```bash
sudo systemctl stop fwupd.service 2>/dev/null; true
```

- Have a display attached for the first reboots if you can. POST messages such as "PCI OUT OF
  RESOURCES" and Error 0162 appear on screen. The reference run was fully remote and relied on variable
  flags instead (Phase 4), which worked, but a screen shortens any diagnosis.
- Know your recovery options before you start. The reference board had no out-of-band recovery path
  **[reported]** (earlier probe, not repeated). Yours may not either. See [05-safety-and-recovery.md](05-safety-and-recovery.md).

### 0.5 Derive your own offsets

The method is: take the exact firmware image, extract the setup module, turn its IFR (the compiled
description of the BIOS menus) into text, and read the offset from the question that owns it. You need
UEFITool / UEFIExtract and IFRExtractor-RS from LongSoft ([IFRExtractor-RS](https://github.com/LongSoft/IFRExtractor-RS)).
The same steps appear in the ReBarUEFI wiki and the `setup_var.efi` README. **[reported]**
([wiki](https://github.com/xCuri0/ReBarUEFI/wiki/Enabling-hidden-4G-decoding),
[setup_var.efi](https://github.com/datasone/setup_var.efi))

**1. Get the image for the version you are running.** Vendor update packages often unwrap to a ROM. On
the reference build, Lenovo's BIOS update ISO contained a 12,582,912-byte `IMAGEM1U.ROM`. On Intel
platforms you can also dump your own image with Intel's Flash Programming Tool; the `setup_var.efi`
README lists that as an option. **[reported]** Dumping reads the chip. Writing flash is outside this
procedure. If the vendor only offers a different version from the one you run, see step 7.

**2. Dump the module list.**

```bash
UEFIExtract IMAGE.ROM report
```

This writes `IMAGE.ROM.report.txt` listing every firmware volume, file, GUID and offset. Run
`UEFIExtract` with no arguments to see the syntax of your build. On the reference image the full `all`
dump died on Windows with Error 35 (path length) while `report` mode worked; if you need the full dump,
run it from a short path. **[reported]** (project notes)

**3. Find the setup module.** In UEFITool NE, search the image for Unicode text (Ctrl+F, Unicode tab):
`Above 4G` (also try `4G Decod`, `MMIO`, `64-Bit Resource`). The hit sits inside the module that holds the
BIOS menu strings. Extract the body of that module's PE32 image section to a file, for example
`Setup.bin`. The exact clicks differ by image; the goal is the module that carries the setup forms.

**4. Convert to readable IFR.**

```bash
ifrextractor Setup.bin
ls *.ifr.txt
```

The output layout differs between IFR extractor versions. The fields you need are the same in all: the
question's prompt, its `VarStore` id, its `VarOffset`, its option values and which one is the default.

**5. Read the question that owns the offset.** Find the prompt, then read the lines of that same
question.

```bash
grep -n -i -E 'above 4g|above 4gb|mmio' *.ifr.txt
grep -n -i 'varstore' *.ifr.txt | grep -i -E 'setup|pci'
```

The first command finds candidates. The second lists the variable stores with their ids, GUIDs and sizes.
Match the question's `VarStore` id to a store record to get the variable name and its size. On the
reference image the entry for the working gate reads as follows (a paraphrase; your tool will lay it out
differently):

| Field | Reference value |
|---|---|
| Prompt | Above 4GB MMIO BIOS assignment |
| QuestionId | 0x54 |
| VarStore id | 0x1, which is the `Setup` store, size 0x1388 |
| VarOffset | 0xA49 |
| Options | Enabled = 1, Disabled = 0 (default) |
| Visibility | Hidden by a suppress-if condition: Aperture Size (QuestionId 0x2746) equal to 0xF |

"Hidden by Lenovo's menu selection" is a different thing from "absent from the firmware". The option was
in the image all along. **[verified]**

> **Read the `Prompt:` that owns the `VarOffset`. Never grep for an offset number, and never take an offset
> from a line you have not tied to its own prompt.** On the reference dump, a search for "Above 4GB MMIO"
> matched the last help line of the question above it ("Aperture Size", `VarOffset` 0x993, where 1 means
> 256 MB), and `grep 0xA49` matches an unrelated `QuestionId`. The first attempt lost a full pull-card, boot,
> write, reinstall cycle to this. **[verified]** The full story is in [06](06-reference-build.md). A wrong byte
> in a memory-timing or voltage question costs far more: a public report describes a laptop left with no RAM
> detected and no display after a mistyped `setup_var` offset, recoverable only by reprogramming the flash
> chip. **[reported]** ([thread](https://winraid.level1techs.com/t/103135))

**6. The `PCI_COMMON` byte is harder, because it has no static question.** On the reference board the
`PCI_COMMON` store has size 7 and its static IFR lists only two items, BME DMA Mitigation at byte 5 and
Hot-Plug at byte 2. "Above 4G Decoding" is not in that list. **[verified]** It appears to be a dynamic
question that the PCI bus driver adds at boot. **[unverified]** Three ways to find its byte, cheapest first:

- *Same GUID on other boards.* Byte 3 is reported as Above 4G Decoding for `PCI_COMMON`
  (`ACA9F304-21E2-4852-9875-7FF4881D67A5`) on a Dell Precision T3620 (BIOS 2.33.0) and a Lenovo
  ThinkStation P310 **[reported]**
  ([T3620](https://github.com/xCuri0/ReBarUEFI/discussions/211),
  [P310](https://winraid.level1techs.com/t/94916)),
  and the author saw the same on a second board, an ASUS ROG Strix X299-E Gaming II (BIOS 2103); that is
  the author's own measurement, not the reference build. Treat all of this as a hypothesis, not as an
  offset.
- *Dump the live HII database* from a UEFI shell with `ShowHIIext.efi save`, then run IFRExtractor on each
  package list; dynamic questions appear there. **[reported]**
  ([lesson](https://github.com/Kostr/UEFI-Lessons/tree/master/Lessons_uncategorized/Lesson_Hidden_BIOS_settings))
- *Disassemble the PCI bus driver.* The reference build did this: with `PCI_COMMON` missing, the driver
  initialises byte 3 to 1, while Lenovo's stored default is 0, which singled byte 3 out. **[reported]**
  (earlier disassembly, not re-run) It is the slowest route.

If you cannot support a `PCI_COMMON` byte with evidence from your own firmware, **leave `PCI_COMMON`
alone**. Do not set byte 0, and do not guess. The ReBarUEFI wiki itself says the `PCI_COMMON` store
usually does not enable 4G decoding even though it contains the setting. **[reported]** On the reference
build, whether byte 3 was needed at all is unproven (section 3.3).

**7. Check the offsets against the live variable before you trust them.** Compare what the image says
with what the running firmware holds:

- The live `Setup` variable must be exactly `VarStore size + 4` bytes long (the 4 is the attribute
  prefix). On the reference board, 5004 bytes.
- The byte at your offset must equal the IFR default for the option.
- Pick four or five other questions from the same IFR and read their bytes. They must agree with the
  values you see in the BIOS menu.

If the image version differs from the running version, this step is mandatory, not optional. The
reference build ran M1UKT72A while the only image available to dissect was M1UKT77A. A later read-only
audit compared six offsets from the 77A analysis against the live 72A variable and all six matched.
**[verified]** That is a measurement on one board, not a rule that adjacent versions share offsets.

**8. Fill in this table.** If any row has no source, stop.

| Item | Your value | Where it came from (file and line of the IFR text) |
|---|---|---|
| `Setup` GUID, size | | |
| MMIO question offset | | |
| Its default and enabled values | | |
| Checksum variable present? name, GUID | | (found in Phase 2) |
| `PCI_COMMON` GUID, size | | |
| `PCI_COMMON` Above-4G byte (or "not touched") | | |

> **Reference-build call-out.** Sibling models share one BIOS package: the Lenovo update that serves the
> M720q also lists the M720t/s, M920t/s/q/x and ThinkStation P330 Tiny. Whether they share these offsets
> is **[unverified]**; nobody has reported it. Derive your own.

---

## Phase 1. Read and back up

Gate: every related variable is copied to files, and each copy re-reads identical to the live variable.

### 1.1 List the candidates

```bash
E=/sys/firmware/efi/efivars
ls -l "$E" | grep -i -E 'setup|pci_common|sum|crc|hash|integrity|restore|changed|error|eventlog'
```

An efivarfs file is 4 attribute bytes followed by the variable's data, so its size is the data length + 4.
Several variables share the `Setup` GUID with other names (for example `SetupCpuFeatures`); pick by name.
An early Dell XPS 8930 BIOS carries two variables both named `Setup` with different GUIDs, one of them a
small dummy. **[reported]** ([grub-mod-setup_var README](https://github.com/datasone/grub-mod-setup_var))

Set the names and the paths. File names are `<Name>-<guid in lowercase>`. The tools take the name (`S`, `P`,
`L`); the `od` and `ls` commands in this page take the path.

```bash
S="Setup-<guid>"
P="PCI_COMMON-<guid>"
L="<checksum variable name>-<guid>"          # only if Phase 2 finds one; leave unset otherwise
SETUP="$E/$S"; PCIC="$E/$P"; LCS="$E/$L"
```

> **Reference-build call-out.**
> ```bash
> S="Setup-ec87d643-eba4-4bb5-a1e5-3f3e36b20da9"
> P="PCI_COMMON-aca9f304-21e2-4852-9875-7ff4881d67a5"
> L="LastCheckSum-e102c8aa-0f10-4a0e-90a7-a6f816089bb7"
> ```

### 1.2 Back up, keeping the attribute prefix

`uefivar.py backup` copies each variable raw, attribute prefix included, so a copy can be written straight
back. It writes the files to disk, re-reads them and compares them with the live variables. Put the backup
on storage that survives a reboot, and copy it off the machine before the first reboot. On a live USB, put
the backup directory on a USB stick or a disk partition you have mounted, not on the live system's own
filesystem. That one lives in RAM and is gone after the reboot. The tool refuses it.

```bash
mkdir -p backups
python3 tools/uefivar.py backup "$S" "$L" "$P" --dir /mnt/usb/backups
```

List the variables in restore order: `Setup` first, then its checksum variable, then the rest. Leave `"$L"`
out if 1.1 showed no checksum candidate. The tool creates `/mnt/usb/backups/efivar-backup-<timestamp>` (mode 0700)
with one `<Name>-<guid>.bin` file per variable, a `manifest.json` and a `MANIFEST.sha256`, and prints the
path. Set `BACKUP` to that path.

Add `--reference-only NAME-GUID ...` after the list for the flag and counter variables that 1.1 and 2.2 turn
up. They are saved for the record and never restored. On the reference Lenovo they are `LastSetupdataSum`,
`SetupDataChecksumError`, `RestoreSetupDataFlag`, `BIOSSetupDataChangedFlag` and `SysEventLogCount`; other
vendors use other names. The reference writes backed up only the first three variables. This procedure also
saves the flags, because they tell you afterwards what the firmware thought happened.

Any refusal or error from `backup` is a stop. The backup directory holds your machine's full setup data. Keep
it private, never attach it to an issue, and share only specific offset and value pairs. Whether the blob
contains secrets such as password hashes was not inspected. **[unverified]** If the tools cannot run on your
system, [05-safety-and-recovery.md](05-safety-and-recovery.md#31-non-negotiable) has a manual fallback.

### 1.3 Record the baseline you will compare against

```bash
dmesg | grep -i 'root bus resource' | tee "$BACKUP/baseline-root-windows.txt"
grep -E '^ *[0-9a-f]{9,}-[0-9a-f]+ : PCI Bus' /proc/iomem | tee "$BACKUP/baseline-iomem-above4g.txt"
```

Run these as root: a normal user sees zeros in `/proc/iomem`. Before the change you expect no PCI bus
window above 4 GB. Phase 4 compares against this.

### 1.4 Get the tools and start your profile

The writes are made by the scripts in [`tools/`](../tools/README.md), not typed by hand. Copy the `tools/`
folder to the machine. It needs Python 3.8 or newer and nothing else.

```bash
python3 tools/selftest.py          # the last line must start with SELF-TEST PASSED
cp tools/profile.example.json my-board.json
```

The self-test runs against a synthetic directory and touches no real variable. Then edit `my-board.json` so
that every value is the one in your Phase 0 table: the exact `bios_version` from 0.2, the name, GUID and data
length of each store, and the offset, expected old value and new value of each field. The example holds the
reference build's values. `unlock` refuses it on any other BIOS version, but a profile you did not derive
from your own IFR is not a profile. Leave `"verified_by_user": false` for now. Phase 2 adds the rest and 3.1
sets it.

---

## Phase 2. Detect guards

Gate: every variable you will write is runtime-writable and not authenticated, and any integrity check
is either modelled or absent. If you cannot say which, stop.

### 2.1 Attributes: writable, and not authenticated

```bash
for v in "$SETUP" "$PCIC" "$LCS"; do printf '%s: ' "$(basename "$v")"; od -An -tx1 -N4 "$v"; done
```

Read the first byte as a bit field ([Microsoft's definition](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setfirmwareenvironmentvariableexa)):

| Bit | Meaning |
|---|---|
| 0x01 | Non-volatile |
| 0x02 | Boot-service access |
| 0x04 | Runtime access |
| 0x10 | Authenticated write |
| 0x20 | Time-based authenticated write |

| What you see | What it means | Do |
|---|---|---|
| `07 00 00 00` | Non-volatile, boot and runtime. Writable from the OS. | Continue. |
| The variable is not listed in efivarfs | No runtime access (attributes `03`, boot-service only). The OS cannot see it. | UEFI-shell route only (3.2). |
| Bit 0x10 or 0x20 set | Authenticated. | **STOP.** |

The reference board's `Setup` and `PCI_COMMON` were both `07 00 00 00`. **[verified]** For contrast, the
firmware updater's attempts to update the Secure Boot KEK on the same board were refused with
"Permission denied" on two separate dates. **[verified]** Authenticated variables are a different class
from `Setup`, and they are not touched here.

Attributes cannot tell you whether the firmware has locked a variable. EDK2-based firmware can mark
variables read-only through its variable-policy and variable-lock mechanisms, and a locked variable still
shows `07 00 00 00`. **[reported]**
([VariablePolicy](https://github.com/tianocore/edk2/blob/master/MdeModulePkg/Include/Protocol/VariablePolicy.h),
[VariableLock](https://github.com/tianocore/edk2/blob/master/MdeModulePkg/Include/Protocol/VariableLock.h))
The only test is a write, which then fails with `EROFS`, `EACCES` or `EINVAL` (see the
[error table](05-safety-and-recovery.md#35-stop-conditions)). That is why the tools write `Setup` first: a refusal
there changes nothing. Do not try to defeat a lock.

Do not try to change a variable's attributes. The specification's method is to delete and recreate it,
and deleting non-standard variables can leave a board that will not POST (the kernel's efivarfs
documentation says so, which is why those files are created immutable). **[reported]**
([efivarfs](https://docs.kernel.org/filesystems/efivarfs.html))

### 2.2 Look for an integrity guard

Some firmware checks `Setup` against a stored sum at boot and reloads defaults when they disagree. Lenovo's
does. Three searches, all read-only.

**Live variables.** Look for names next to `Setup` that contain a sum, hash or error flag:

```bash
ls "$E" | grep -i -E 'sum|crc|hash|integrity|restore|changed|error'
```

**The image.** PEI modules are stored uncompressed, so their string names are visible in the raw image as
UTF-16. DXE modules are compressed, so error message text is not. This script lists UTF-16 hits with
their file offsets; match them to the modules in the UEFIExtract report from Phase 0.

```bash
cat > u16scan.py <<'EOF'
import re, sys
img = open(sys.argv[1], "rb").read()
for word in sys.argv[2:]:
    for m in re.finditer(re.escape(word.encode("utf-16-le")), img, re.I):
        print(f"{word}: file offset {m.start():#x}")
EOF
python3 u16scan.py IMAGE.ROM checksum crc hash integrity
```

Also scan the module names in the report for `CheckSum`, `Crc`, `Hash`, `Integrity`, `Validate`,
`SetupData`. This is how the reference guard was found: one hit for `LastCheckSum` in the raw image,
mapped to a PEI module named `SetupCheckSumPei`. **[verified]**

**Differential observation (proposed; not used on the reference build).** Snapshot all variables, change
one harmless visible option in the BIOS menu, Save and Exit, snapshot again, and compare. Any variable
other than `Setup` that changed alongside it is bookkeeping or a guard. Expect noise from boot-order
variables.

```bash
mkdir -p snap-a && for f in "$E"/*; do cat "$f" > "snap-a/$(basename "$f")" 2>/dev/null; done
# reboot, change one harmless option in the BIOS menu, Save and Exit, boot Linux again, then:
mkdir -p snap-b && for f in "$E"/*; do cat "$f" > "snap-b/$(basename "$f")" 2>/dev/null; done
for f in snap-a/*; do cmp -s "$f" "snap-b/$(basename "$f")" || echo "changed: $(basename "$f")"; done
```

The snapshots contain your boot entries and machine data. Delete them when finished.

### 2.3 What the reference guard does

Found by hand-decoding the module's x86 bytes (no disassembler was available; confidence is high on
structure and moderate on instruction detail). The full account is in [03](03-how-it-works.md), section 3.

- A PEI module, `SetupCheckSumPei`, adds every data byte of `Setup` into a 16-bit value (no carry fold, no
  complement) and compares it **for exact equality** with the 2-byte variable `LastCheckSum`. **[verified]**
  It skips the check for boot mode 0x20, which the PI specification calls recovery (constant not re-checked
  against the specification text). Whether it also skips S3 resume is **[unverified]**: the decoded bytes show
  no S3 comparison.
- On a mismatch it signals a Lenovo PPI. A later stage reports Error 0162 ("Setup data integrity check
  failure") and loads default settings, which also resets every other BIOS option you changed. **[reported]**
  The modules involved are present in the image **[verified]**, but the message text sits in a compressed
  stage and was not read, and the reference board never took this path. Lenovo documents the same code on a
  ThinkSystem server ([page](https://pubs.lenovo.com/st50/POST_ERROR_0162)); that page documents the message,
  not this ThinkCentre mechanism.
- `LastCheckSum` lives under the **Lenovo** GUID `E102C8AA-0F10-4A0E-90A7-A6F816089BB7`: 2 data bytes,
  little-endian, attributes `07 00 00 00`. It does not live under the `Setup` GUID. **[verified]**
- A related Lenovo variable, `LastSetupdataSum`, exists on the reference board. Its role is unknown, and the
  reference writes never touched it. **[unverified]** for its purpose.

### 2.4 Measure the relation between the stored sum and the OS-visible sum

Do not assume the stored value equals the sum you can compute. On the reference board it did not, and the
difference changed over time.

```bash
python3 - "$SETUP" "$LCS" <<'PY'
import sys
d = open(sys.argv[1], "rb").read()[4:]
s = int.from_bytes(open(sys.argv[2], "rb").read()[4:], "little")
print(f"sum16(Setup) = {sum(d) & 0xFFFF:#06x}   stored = {s:#06x}   delta (stored - sum16) = {(s - sum(d)) & 0xFFFF}")
PY
```

`python3 tools/uefivar.py checksum --setup "$S" --stored "$L"` prints the same sum16, stored value and delta.
`unlock` measures it again at write time.

| When (reference board) | Stored `LastCheckSum` | sum16 of `Setup` seen from the OS | Delta |
|---|---|---|---|
| 2026-09-04, before the write | 0xDC24 | 0xDC23 | +1 |
| 2026-09-04, after the write | 0xDC25 | 0xDC24 | +1 |
| 2026-10-07, read-only audit | 0xDC25 | 0xDC25 | 0 |

All three **[verified]**. No Error 0162 occurred at any boot in between. The delta went from +1 to 0
while the machine had a RAM, SSD and GPU change on 2026-10-06. What moved it is unknown; candidate
causes are a byte that the firmware rewrites after the early check, or a rewrite triggered by the
hardware change. Neither was tested. **[unverified]**

The consequence is the rule the tools use: **new stored value = sum16 of the new `Setup` + the delta you
measured just now.** That preserves whatever relation the firmware currently holds. A rule of "write the
plain sum" would have been wrong on 2026-09-04 and right by accident a month later.

Accept a delta of 0 or 1. The profile key `checksum.max_delta` is 1 by default. Anything else means the model
does not fit your board: stop and re-analyse rather than widen the tolerance. The tools do let you raise
`checksum.max_delta` up to 4 and refuse above that. Raising it contradicts this advice; only 0 and +1 have been
observed.

### 2.5 Pre-flight flags

```bash
for n in SetupDataChecksumError RestoreSetupDataFlag BIOSSetupDataChangedFlag SysEventLogCount; do
  for f in "$E/$n"-*; do [ -e "$f" ] && { printf '%s: ' "$n"; od -An -tx1 -j4 "$f"; }; done
done
```

On the reference board the three error and restore flags read `00` and `SysEventLogCount` read 6 before
and after the write. **[verified]** Require your equivalents to be clean before writing. If a flag is
already set, find out why first.

### 2.6 Go or stop

| Finding | Action |
|---|---|
| All variables `07 00 00 00`, checksum model fits (delta 0 or 1, 2-byte variable) | Continue to 3.1. |
| Variables not in efivarfs, and no checksum guard found | 3.2 (UEFI shell), attended. Nothing on that route shows a guard afterwards except a reverted byte; read its last step. |
| Variables not in efivarfs, and a checksum guard exists | **STOP.** No verified recipe exists (see 3.2). |
| Any variable authenticated (0x10 or 0x20) | **STOP.** |
| An integrity module or variable exists but you cannot model it | **STOP.** Do not guess. |
| No guard evidence anywhere | Continue, but absence of evidence is not proof. Phase 4 is the real test. |

---

## Phase 3. Write

Gate: the read-back matches the plan, and exactly the intended bytes differ.

### 3.0 Rules for any write

The tools in 3.1 implement these rules. They are written out so that you can check any other tool against them.

- **Write the whole variable, at its original length, with its original attributes.** The first 4 bytes of
  the efivarfs file are the attributes; copy exactly what you read. A UEFI `SetVariable` replaces the
  entire value. The kernel's efivarfs write path takes the bytes after the 4-byte prefix as the new value
  and rejects a buffer shorter than 4 bytes or one with invalid attribute bits. **[reported]**
  ([file.c](https://github.com/torvalds/linux/blob/master/fs/efivarfs/file.c))
  So a short write is not "patch one byte". It is "replace `Setup` with a shorter `Setup`", and a write
  that carries the 4 attribute bytes and no data deletes the variable (same source). **Refuse any write
  whose length differs from the length you read.**
- **One `write()` call for the whole blob**, with the returned count checked against the length. A
  buffered writer may split a large blob into several calls; the tools use `os.write` for that reason.
- **Order: `Setup`, then the checksum, then `PCI_COMMON`**, back to back, in one process. The three
  writes are not atomic. If something stops after `Setup` and before the checksum, the next boot raises
  Error 0162 and loads defaults. Keeping the two writes adjacent makes that window as short as it can be.
- **Never create, delete or recreate a variable.** A variable is deleted only by a write that carries the 4
  attribute bytes and no data, so refuse any write without data. The tools open the file with
  `O_WRONLY|O_TRUNC` and no `O_CREAT`, and send the attributes plus the whole value in that one `write()`.
  Opening or truncating the file does not by itself change the firmware variable: the change happens inside
  `write()`. **[reported]** (same source)
- **Change only the intended bytes.** The tools diff each written variable against the backup afterwards and
  fail if any byte outside the intended ones differs.

> **Never pass a size to a one-byte write.** A circulating forum command for this option uses `-s 0x08`
> ([thread](https://winraid.level1techs.com/t/104420)). In
> `setup_var.efi` 0.2.x syntax `-s` is a byte count, so on a one-byte field it would write `01` and zero the
> seven bytes after it. That is our reading and we did not run the command **[unverified]**. Details:
> [05](05-safety-and-recovery.md), section 1.

### 3.1 Route A: Linux efivarfs (preferred)

Use this when Phase 2 showed the variables in efivarfs with attributes `07 00 00 00`. The write is made by
[`tools/above4g_unlock.py`](../tools/README.md), which implements the rules in 3.0 and refuses anything that
does not match your profile.

> **These tools have never run against a real efivarfs mount.** Their self-test uses a synthetic directory,
> and the reference writes came from a different, one-off script ([06](06-reference-build.md)). Treat your
> first `--apply` as the first live test: the card stays out, the backup is verified, and you hold the
> restore command before you start.

**1. Finish the profile** (`my-board.json` from 1.4) with the results of Phase 2.

| Profile key | Fill in with |
|---|---|
| `variables.checksum` | The checksum variable's name, GUID and data length (2) from 2.2 and 2.4. If Phase 2 found no guard, set `checksum.model` to `"none"` and put in `checksum.evidence` how you know. `unlock` then writes `Setup` alone and cannot check that claim. |
| `checksum.max_delta` | 1, as in 2.4. |
| `guard_flags`, `watch`, `also_backup` | The flag variables from 2.5 (a non-zero one stops the run, before and after), counters to compare after the reboot, and bookkeeping variables saved for the record. |
| `variables.pci_common`, `fields.pci_common` | Leave both out unless you hold evidence for a `PCI_COMMON` byte (0.5, step 6). Then `PCI_COMMON` is not touched. |
| `verified_by_user` | `true`, only after you checked every BIOS version, length, offset and old value against your own IFR (0.5, step 7) and can point to the IFR line for each. The tool cannot check this. |

Remove the `_example_warning` line and set your own `title`.

**2. Dry run.** It checks everything, prints the plan and a confirmation token, and writes nothing.

```bash
python3 tools/above4g_unlock.py unlock --profile my-board.json
```

**STOP** on any `[FAIL]` line or `VERDICT: refused`: a BIOS version or board that differs from the profile, a
different length or attribute, a target byte that is not at its expected old value, a non-zero guard flag, a
checksum delta outside the bound. Do not edit the profile to get past a refusal. The profile or your reading
of the board is wrong: go back to Phase 0 or 2.

Example output (abridged), with the reference build's values:

```
Mode    : DRY RUN (nothing is written)
  [ ok ] BIOS version 'M1UKT72A' matches the profile
  [ ok ] sum16(Setup)=0xdc23, stored LastCheckSum=0xdc24, live delta = stored - sum16 = 1 (bound 0..1)
  [ ok ] guard flag SetupDataChecksumError is zero
PLAN  (write order: Setup, then the checksum immediately, then PCI_COMMON)
  1. Setup offset 0xa49 width 1: 0x0 -> 0x1   Above 4GB MMIO BIOS assignment (...)
  2. LastCheckSum: 0xdc24 -> 0xdc25   (= sum16(new Setup) 0xdc24 + live delta 1)
  3. PCI_COMMON offset 0x3 width 1: 0x0 -> 0x1   Above 4G Decoding (...)
DRY RUN complete: every check passed, nothing was written.
To apply:  --apply --i-understand-the-risk --backup-dir DIR --confirm M1UKT72A:0xa49:0x3
```

**3. Apply.** Run it again with the confirmations the dry run asked for.

```bash
python3 tools/above4g_unlock.py unlock --profile my-board.json \
  --apply --i-understand-the-risk --backup-dir /mnt/usb/backups --confirm <token from the dry run>
```

It refuses without all four of `--i-understand-the-risk`, `--backup-dir`, the exact token and
`"verified_by_user": true`. On a live USB, put `--backup-dir` on a USB stick or a disk partition you have
mounted, not on the live system's own filesystem. That one lives in RAM and is gone after the reboot. The tool
refuses it. The tool then makes its own verified backup under `/mnt/usb/backups/efivar-backup-<timestamp>` (the format
and checks of 1.2), clears the immutable flag, writes `Setup`, the checksum (`sum16(new Setup)` plus
the delta it measured) and `PCI_COMMON` in that order, and sets the flag again
([efivarfs](https://docs.kernel.org/filesystems/efivarfs.html)). Each write is the 4 attribute bytes plus the
whole value in one `write()`. It reads everything back and fails unless each variable differs from the backup
only at the intended bytes and the stored checksum equals `sum16(Setup)` plus the delta. On success it prints
`VERIFIED` and writes `report.json` into the backup directory.

Set `WRITE_BACKUP` to that directory. It was taken seconds before the write, so it is the one to restore from.
The copy from 1.2 is an earlier one, and `restore.py` reads both the same way.

**If it stops.** Any failure or interrupt prints `STOP`, exits with code 3, shows what each variable holds now
and prints the exact restore command. If `Setup` and the checksum disagree, the tool writes the backup back
itself, `Setup` first and the checksum right after. If they are still a consistent pair (for example
`PCI_COMMON` failed after both were written), it changes nothing more, because a rollback is two more writes,
and leaves the restore command to you. If the output says `DO NOT REBOOT`, do not reboot until the restore has
run. The restore needs a board that boots to an OS, and refuses a backup without a valid manifest, a BIOS
version or board that differs, and a live variable whose length changed
([05](05-safety-and-recovery.md#22-restoring-from-backup)):

```bash
python3 tools/restore.py "$WRITE_BACKUP"                                   # dry run: shows what would change
python3 tools/restore.py "$WRITE_BACKUP" --apply --i-understand-the-risk
```

**One store at a time.** `--only setup` or `--only pci_common` writes one store per run, and
[tools/README](../tools/README.md) calls `--only setup` first the cautious order. The reference build changed
both stores at once, so what each did is unproven (3.3). If you split the run, do Phase 4 after each round, and
go on to `pci_common` after a window-less setup-only round only when you hold evidence for that byte
(0.5, step 6). Otherwise 4.3 applies: stop.

> **Reference-build call-out.** `tools/profile.example.json` holds the values from the Reference values table.
> Its confirmation token is `M1UKT72A:0xa49:0x3` for both fields, or `M1UKT72A:0xa49` with `--only setup`.

When `VERIFIED` appears, go to Phase 4. Do not reboot before it does.

### 3.2 Route B: UEFI shell with `setup_var.efi`

Use this only when Phase 2 shows the variables are **not** visible in efivarfs. This is the route for
boards whose `Setup` and `PCI_COMMON` are boot-services-only. It is attended: you need a keyboard and a
display at the machine.

**This route has no verified recipe for a board with a checksum guard.** `setup_var.efi` addresses a
variable by its HII name and offset, and on the reference image `LastCheckSum` is not declared in the
`Setup` IFR (no hits), so the tool cannot reach it. **[unverified]** inference from the README and that
search. The UEFI shell's own `setvar` command with an explicit GUID is the only candidate for writing the
checksum, and it is untested. If Phase 2 found a guard, stop here.

No shell-route write was performed on the reference build; its variables were runtime-visible. The route
did work on the second board mentioned in Phase 0, where `Setup` and `PCI_COMMON` returned error 203 from
the Windows firmware-variable API (not runtime-visible), the changes were made from the shell, and there
was no checksum guard: the bytes survived a full power cycle. That is the author's own measurement.

1. Prepare a FAT32 USB stick. Put an x86-64 UEFI Shell binary at `EFI/BOOT/BOOTX64.EFI` and
   [`setup_var.efi`](https://github.com/datasone/setup_var.efi) at the root. Secure Boot must be off.
2. Boot it from the firmware's one-time boot menu.
3. **No saved dump, no write.** Save the variables before you write. The shell's `dmpstore` can save a
   variable to a file. The syntax below follows the UEFI Shell specification and is **[unverified]** here;
   run `help dmpstore` on your shell first.

   ```
   fs0:
   dmpstore -s fs0:\setup.dmp -guid <SETUP-GUID> Setup
   dmpstore -s fs0:\pci_common.dmp -guid <PCI_COMMON-GUID> PCI_COMMON
   ls fs0:\
   ```

   If `dmpstore` is missing, a save printed an error, or the two dump files are not listed, **STOP**. Do not
   write. The dump is a record, not a tested restore path (see step 6).

4. Read before writing. `setup_var.efi` 0.3.x syntax is
   `<VAR_NAME>[(VAR_ID)]:<OFFSET>[(VALUE_SIZE)][=VALUE]`, and the 0.2.x syntax is
   `<OFFSET> [<VALUE>] [-s <VALUE_SIZE>] [-n <VAR_NAME>] [-i <VAR_ID>]`.
   In both, `VALUE_SIZE` is a count of bytes and defaults to 1. **Never pass a size.** Check which
   version you downloaded. **[reported]**
   ([0.3.x README](https://github.com/datasone/setup_var.efi),
   [0.2.x README](https://github.com/datasone/setup_var.efi/tree/0.2.x))

   | Version | Read one byte | Write one byte |
   |---|---|---|
   | 0.3.x | `setup_var.efi Setup:0x<OFFSET>` | `setup_var.efi Setup:0x<OFFSET>=0x1` |
   | 0.2.x | `setup_var.efi 0x<OFFSET> -n Setup` | `setup_var.efi 0x<OFFSET> 0x1 -n Setup` |

   The same forms apply to `PCI_COMMON`. The current value must match your Phase 0 table before you write.
   Write the current value of every byte you will change on paper or take a photo. It is your revert value.

5. Write each byte, read it back, then reset (0.3.x syntax shown).

   ```
   setup_var.efi Setup:0x<OFFSET>=0x1
   setup_var.efi Setup:0x<OFFSET>
   setup_var.efi PCI_COMMON:0x<OFFSET>=0x1
   setup_var.efi PCI_COMMON:0x<OFFSET>
   reset
   ```

   End every shell session with `reset`. A headless one-shot script that lacked a final reset once left a
   test machine dark for over ten minutes, until the power button was used. That was the author's
   observation on a board other than the reference.

6. **Undo and check.** To undo a write, boot the stick again and write the old byte back with the same form
   as step 5, for example `setup_var.efi Setup:0x<OFFSET>=0x<OLD>`, read it back, and end with `reset`.
   Loading a saved dump with `dmpstore -l` is **[unverified]**: this repo has not tried it, so prefer the
   one-byte write. Nothing on this route detects a checksum guard for you. After the reset, boot Linux for
   Phase 4 (the window check does not need efivarfs), then boot the stick once more and read both bytes
   back with the read form from step 4. A byte that has gone back to its old value means something undid
   the write, most likely a guard you could not see (the P330 symptom in Troubleshooting).

The project README carries one warning, and it applies: accessing the wrong variable or offset may
completely brick your computer. **[reported]**

### 3.3 What the reference build proved, and what it did not

The reference writes changed `Setup[0xA49]` and `PCI_COMMON[3]` in the same run. The new 64-bit root window
appeared before any card was installed, which points at the `Setup` gate. Whether `PCI_COMMON[3]` was needed
is **unproven**. An earlier disassembly of this build's PCI bus driver found it reading only bytes 0 and 5 of
that store, which would suggest byte 3 is not consumed there **[reported]** (earlier disassembly, not
re-run). A Lenovo SMM module that toggles byte 3 is present in the image **[verified]**; what it does comes
from the same earlier analysis **[reported]**. Other boards use byte 3. This procedure changes both because
that is the only combination known to work. If you want to learn which one matters, do that after you have a
working system, not before.

`PCI_COMMON` byte 0 reads 1 on the reference build. A first attempt set it, and it was still 1 on 2026-10-07.
What it controls is unknown. This procedure never writes it. The other open questions are listed once, in
[06](06-reference-build.md#11-what-is-still-unproven).

---

## Phase 4. Reboot without the card

Gate: the board boots; a 64-bit root window exists that did not exist before; the values persisted; no
integrity-error flag is set.

```bash
sudo systemctl reboot
```

The card stays out. After it boots, as root:

### 4.1 Values persisted

```bash
ls -l "$SETUP" "$PCIC" "$LCS"                        # sizes unchanged (data length + 4)
od -An -tx1 -N4 "$SETUP"                             # attributes unchanged
od -An -tx1 -j $((4 + <OFFSET>)) -N1 "$SETUP"        # your Setup byte
od -An -tx1 -j4 "$PCIC"                              # PCI_COMMON data
od -An -tx1 -j4 "$LCS"                               # stored checksum, little-endian
```

Re-run the sum and delta script from 2.4. Expect the offset byte and the `PCI_COMMON` byte to read what you
wrote, and the delta to be what you measured in Phase 2. If the `Setup` byte reads the old value, the
firmware reloaded defaults or refused the change; see Troubleshooting.

If you wrote with the tools, `verify` re-reads the bytes, the checksum relation, the guard flags and the watch
counters, and lists any PCI bus range above 4 GiB in `/proc/iomem`. Add the same `--only` value you used for
`unlock`, if any.

```bash
python3 tools/above4g_unlock.py verify --profile my-board.json --report "$WRITE_BACKUP"
```

> **Reference-build call-out.** `Setup[0xA49]` = `01`, `PCI_COMMON` = `01 00 01 01 00 00 00`,
> `LastCheckSum` = `25 dc` against `sum16(Setup)` = 0xDC24. **[verified]**

### 4.2 No integrity error

Re-run the flag loop from 2.5. On the reference board `SetupDataChecksumError` and `RestoreSetupDataFlag`
stayed `00` and `SysEventLogCount` stayed at 6, meaning no Error 0162 and no defaults reload. **[verified]**
A reload would have reset the option to its default, so a persisted value is itself evidence.

### 4.3 The new window

```bash
dmesg | grep -i 'root bus resource'
grep -E '^ *[0-9a-f]{9,}-[0-9a-f]+ : PCI Bus' /proc/iomem
```

Compare with `baseline-*.txt` from Phase 1. You are looking for a root bus resource window that starts
above 4 GB (an address of 0x100000000 or higher).

> **Reference-build call-out.** After the writes, with no card installed:
> `pci_bus 0000:00: root bus resource [mem 0x4000000000-0x7fffffffff window]`, a 256 GB window taken from
> the ACPI `_CRS` method. `pci=nocrs` was not needed. Several on-board and add-in devices then received
> BARs above 4 GB and kept working. **[verified]**

**No new window means stop.** Either the byte you changed is not the gate, or the firmware does not use
it. A value that reads back correctly proves only that the variable store accepted it. A report on a
ThinkStation P310 has `PCI_COMMON` byte 3 reading 1 while GPU-Z still shows 4G decoding off.
**[reported]** ([thread](https://winraid.level1techs.com/t/94916))
The Kostr lesson makes the same warning: a setting found in the IFR may not be used by the driver code.
**[reported]** Restore from backup, do not install the card, and go back to Phase 0.

### 4.4 Persistence

Power off completely, wait half a minute, power on, and repeat 4.1 to 4.3. The reference board's values
survived the reboot, and a later read-only audit on 2026-10-07 found them still set after a RAM, SSD
and GPU change. **[verified]**

---

## Phase 5. Install the card

Gate: by physical outcome, not by variable value. The large BAR sits inside the new window and the driver
is bound.

Power off, install the card, boot. Then:

```bash
lspci -nn | grep -i -E 'vga|3d controller|display'      # note the card's <bus:dev.fn>, for example 01:00.0
sudo lspci -vv -s <bus:dev.fn> | grep -E 'Region|Kernel driver'
sudo grep -i '<bus:dev.fn>' /proc/iomem
sudo dmesg | grep -i -E 'NVRM|BAR |can.t assign|no space|failed to assign'
```

For an NVIDIA card with the driver loaded, `nvidia-smi -q -d MEMORY` also reports the BAR1 total.
**[reported]**

Success is all three of these: the card's large region is listed at an address inside the window from
Phase 4; a kernel driver is bound; the driver's own tool runs.

> **Reference-build call-out.** `lspci` showed the A2 (GA107GL, 10de:25b6) with Region 1 as a 16 GB
> 64-bit prefetchable region at `0x4400000000`, inside the `0x4000000000-0x7fffffffff` window. The
> `nvidia-open` module (version 610.57.04) bound and `nvidia-smi` reported normally. The kernel logged
> "failed to assign" lines for the card's SR-IOV virtual-function BARs; those were noise and the card
> worked. **[verified]**

### If the window exists but the card's BAR is not placed in it

Try one kernel parameter at a time. Neither was needed on the reference build. **[verified]**

| Parameter | Effect |
|---|---|
| `pci=realloc` | Lets the kernel reallocate PCI bridge resources when the firmware's allocation was too small. Introduced by a 2011 patch. **[reported]** ([patch](https://lkml.iu.edu/hypermail/linux/kernel/1106.3/02173.html)) Current kernel-parameters wording not re-read. |
| `pci=nocrs` | Ignores the host-bridge windows ACPI `_CRS` reports, so the kernel assigns from its own view. Defined in `arch/x86/pci/common.c`. **[reported]** ([source](https://github.com/torvalds/linux/blob/master/arch/x86/pci/common.c)) |

Try it once without making it permanent: at the GRUB menu press `e`, append the parameter to the line
that starts with `linux`, and boot with Ctrl+X. To make one stick on Debian or Ubuntu, add it to
`GRUB_CMDLINE_LINUX_DEFAULT` in `/etc/default/grub` and run `sudo update-grub`.

### Driver notes

Driver branches by GPU generation are in [02](02-does-my-hardware-qualify.md), section "The GPU side". Only the
A2's 16 GB BAR was observed, so BAR sizes for T4, L4, P4 and P40 are **[unverified]**.

---

## Troubleshooting

### Symptoms

| Symptom | Likely cause | What to do |
|---|---|---|
| POST shows **Error 0162**, defaults loaded, your byte reads the old value | The checksum guard saw `Setup` and the stored sum disagree and reloaded defaults. | The change is lost; your other BIOS options reset too. Re-check UEFI-only and Secure Boot. Redo Phase 1 and 2, re-measure the delta. If it happens twice, your checksum model is wrong: STOP. |
| Reboot, value reverted, no message seen (headless) | Same as above. A report on a ThinkStation P330 tower describes a tool that accepts the enable flag and then has it disabled again at restart ([thread](https://winraid.level1techs.com/t/104196)). **[reported]**, cause **[unverified]** | Read the flags in 2.5. A non-zero `SetupDataChecksumError` or `RestoreSetupDataFlag` would indicate a reload; they were never seen set on the reference board, so this is **[unverified]**. The value itself reverting is the firmer evidence. |
| Still looping on "PCI OUT OF RESOURCES" with no card change | The window never appeared (wrong gate, or the firmware ignores the byte), or you installed the card before Phase 4. | Remove the card. Check `/proc/iomem` per 4.3. No window: restore and go back to Phase 0. |
| Window appears, card still fails or the driver reports `BAR0 is 0M @ 0x0` | The window is too small, or the kernel did not use it. | `pci=realloc` or `pci=nocrs` (Phase 5). If the firmware has an MMIO size option, a larger window may be needed. **[unverified]** |
| Values read back as written, but no new window | Wrong gate, or a value the driver code never consumes. | Stop. Restore. Re-derive the offset from your own IFR. |
| No display after the write, or the board drops into setup every boot with the card in | Reported on a Dell Precision T3620 after a `PCI_COMMON` byte 3 write; cured by removing the CMOS battery. **[reported]** ([thread](https://github.com/xCuri0/ReBarUEFI/discussions/211)) | See the recovery ladder in [05-safety-and-recovery.md](05-safety-and-recovery.md). Variable stores often live in the flash chip, not in CMOS, so a CMOS clear is not guaranteed to help. |
| The variable is missing from efivarfs | No runtime access (attributes `03`). | UEFI-shell route, 3.2, if there is no guard. |
| `chattr: Operation not permitted`, or efivarfs not mounted | Not root, or efivarfs mounted read-only. | `mount \| grep efivarfs`; run as root; do not remount over a failing firmware. |
| `lspci` shows the card with its BAR above 4 GB, but no driver binds | A driver problem, not a firmware problem. | Check the driver's supported architectures (Phase 5, driver notes). |

### Write errors

Any non-zero result from a write is a stop. The errno table (`EROFS`, `EACCES`, `EINVAL`, `EPERM`, `ENOSPC`,
`EIO`, `ENOENT`, `EINTR`) is in [05, section 3.5](05-safety-and-recovery.md#35-stop-conditions).

If the first write fails, nothing has changed. If a later write fails, the tools decide from the live
variables. When `Setup` and the checksum disagree, they write the backup back. When they are still a
consistent pair (for example `PCI_COMMON` failed), they change nothing more and print the exact `restore.py`
command. Either way, read what each variable holds before you reboot, and run the restore command if the
output tells you to.

---

## What this procedure does not establish

The open questions are listed once, in [06, section 11](06-reference-build.md#11-what-is-still-unproven):
whether `PCI_COMMON[3]` was needed, what `PCI_COMMON` byte 0 and `LastSetupdataSum` do, why the checksum delta
moved from +1 to 0, whether the early check skips S3 resume, whether sibling models share the offsets, and
which other boards are unknown.
