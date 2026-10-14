# Safety and recovery

Read this before you write anything. It covers what can go wrong when you edit hidden setup variables, how
to get back out, and the checks a write has to pass first. The step-by-step is in
[04-procedure.md](04-procedure.md); the worked example is in [06-reference-build.md](06-reference-build.md).

This procedure changes **settings data**, not firmware code. The usual failure is a board that loops, drops
into BIOS, or reloads its defaults, and that is normally fixed with the card out and a restore from your
backup. The exception is a bad value in a setting the firmware consumes very early
(memory configuration is the reported case), because a CMOS clear may not undo that. Section 2.1 explains
how to find out, before you write, which kind of board you have.

The scripts in [`tools/`](../tools/README.md) implement the safe-write properties in section 3 and the
restore in section 2.2: `above4g_unlock.py` for the write and `restore.py` for the way back.

How claims are tagged here:

- **Verified** means measured on the reference build (a Lenovo ThinkCentre M720q Tiny with a 16 GB NVIDIA A2).
- **Reported** means one forum or issue-tracker post, linked, not reproduced here.
- **Inference** means reasoning from the above or from source code; it is labelled so you can discount it.

## 1. What can go wrong

| Failure | What you see | Cause | Usual way out |
|---|---|---|---|
| Board loops or will not POST | "PCI OUT OF RESOURCES" loop, blank screen, beep codes, or dropping into BIOS on every boot | The write did not open the 64-bit window, CSM was left on, or the card went in before the window was proven with it out | Card out, boot, restore backup |
| Defaults reloaded silently | Your change is gone and other BIOS settings are back to factory; on Lenovo, "Error 0162" | A board-side checksum of the Setup store did not match what you wrote | Redo with a correct checksum (section 3.3) |
| Wrong-size write | Garbage in fields next to your target, or a Setup variable that is shorter than it should be | A tool wrote more or fewer bytes than the field or variable holds | Restore the whole variable from backup |
| Wrong byte in the wrong question | No POST, no display, a memory or error LED code | A byte that belongs to a memory, CPU or voltage question was changed | Often not a CMOS clear (section 2.1) |

**Board will not POST.** Three separate causes show up in the sources.

- Enabling 4G decoding with CSM on can give a black screen or lock you out of setup
  ([ReBarUEFI wiki](https://github.com/xCuri0/ReBarUEFI/wiki/Enabling-hidden-4G-decoding)).
- On a Dell Precision T3620, the first boot after writing `PCI_COMMON` byte 3 "appeared dead". Pulling the
  CMOS battery fixed it, and the edit was still in place afterwards. With the GPU installed the board dropped
  into BIOS on every boot; with the GPU removed it was fine
  ([reported](https://github.com/xCuri0/ReBarUEFI/discussions/211)).
- On the reference build, the first attempt wrote the wrong field and the board kept booting without the card
  (verified; see [06](06-reference-build.md)). The loop returned only when the card went back in, because the real problem was
  still there.

**Silent reload of defaults.** On the reference Lenovo, an early-boot (PEI) module, `SetupCheckSumPei`, adds up
every byte of the `Setup` variable as a 16-bit sum and compares it for exact equality with a stored value, the
`LastCheckSum` variable. On a mismatch a later stage shows "Error 0162 Setup data integrity check failure" and
the board loads defaults. The PEI module's logic was decoded from the vendor image; the later stages come from
earlier notes and were not re-verified, and the reference board never took this path. The details, including
the boot-mode skip and what is not established, are in [04, 2.3](04-procedure.md#23-what-the-reference-guard-does).
Lenovo documents the same code and wording for its ThinkSystem ST50 servers
([Lenovo](https://pubs.lenovo.com/st50/POST_ERROR_0162)); that page describes the message, not this ThinkCentre
mechanism. A related symptom is a ThinkStation P330 owner reporting that a tool "accepts the enable flag but then
disables it at restart" ([reported](https://winraid.level1techs.com/t/104196));
a checksum guard would explain that, but the cause there is unconfirmed.

**Wrong-size write.** There are two ways to get this wrong.

1. In-place tools. `setup_var.efi` takes a value size in bytes
   ([0.2.x README](https://github.com/datasone/setup_var.efi/tree/0.2.x)). A size wider
   than the field would overwrite the neighbours with zeros, little-endian (inference from that definition).
   A forum post about the M720q uses `-s 0x08` on a field whose IFR dump says `Size: 8`. The IFR header
   constants suggest a 1-byte field there (the dump's flags decode to size code 0), so the 8 is most likely a
   width in bits, and passing it as bytes would hit seven neighbours
   ([post](https://winraid.level1techs.com/t/104420),
   [IFR constants](https://github.com/tianocore/edk2/blob/master/MdePkg/Include/Uefi/UefiInternalFormRepresentation.h)).
   That decoding is inference; the post is an unanswered question with no reported result.
2. Whole-variable tools such as Linux `efivarfs`. A write hands the firmware the *entire new value*. Write
   only the changed bytes and you have asked for a Setup variable a few bytes long. Split a blob across two
   `write()` calls and the second replaces the first. This is an inference from the kernel's
   [write path](https://github.com/torvalds/linux/blob/master/fs/efivarfs/file.c); it was not
   observed.

**Wrong byte in the wrong question.** A Dell G5 15 5590 owner edited memory frequency with `setup_var_3`,
made a typo, and got "no RAM detected", no display, and RTC resets that changed nothing. A reply said the
settings are stored in NVRAM inside the same SPI flash chip and "won't get reset by CMOS jumper"; the advice
was a programmer, a valid dump, and a reflash
([reported](https://winraid.level1techs.com/t/103135)).
Nothing in this procedure touches a memory, CPU or voltage question, and your guardrails should make sure it
cannot.

### Why this is usually recoverable, and how thin the evidence is

A variable write leaves the stock firmware in place. The board's own recovery paths still exist (inference).
Some boards, Lenovo's among them, are built to detect a damaged Setup store and fall back to defaults. The
first-line fixes (card out, Load Setup Defaults, CMOS clear) cost nothing.

That is the case for "usually". The case against over-trusting it: nothing on the reference build needed
recovery. The outside reports are two: a board that looked dead until a battery pull, and a laptop that
needed a programmer after a memory-setting typo. Read "usually" as "in the cases reported", not as a promise.

**Unlike the SPI-flash route.** Flashing a modified image to the SPI chip (the route used for things like
adding CPU support) replaces the code that runs first. A bad image can leave no display and no setup menu to
recover with, and boards with Boot Guard or signed-update checks may refuse a modified image or refuse to POST
it. Reported: a Dell Precision T3620 "will not let my system POST if a modified BIOS is flashed"
([discussion](https://github.com/xCuri0/ReBarUEFI/discussions/211)); an HP EliteDesk 800 G5 gave "BIOS
signature verification failed"
([thread](https://winraid.level1techs.com/t/96245));
an M720q owner got "FPT write returns Error 167"
([thread](https://winraid.level1techs.com/t/38139)). The fix in that class
is an external programmer and a good dump. A variable edit does not change firmware code, so those checks have
nothing to object to (inference).

## 2. Recovery ladder

Work down. Each rung costs more than the one above.

| Rung | Action | Needs | Notes |
|---|---|---|---|
| 0 | Remove the GPU and anything else you added, boot, restore the variables from your backup | A board that POSTs without the card | The rung you will use most; card-out was the reference build's resting state between tests |
| 1 | BIOS **Load Setup Defaults**, then Save and Exit | Access to the BIOS menu | See notes below |
| 2 | **CMOS clear**: jumper, or coin cell with the machine unplugged | Case open | Works on some boards, not all; read 2.1 first |
| 3 | **External SPI programmer** | Programmer, clip or soldering, a valid dump | Rare last resort |

**Rung 1 notes.** Load Setup Defaults resets what the firmware's default table says. No source found
confirms that it resets the hidden `PCI_COMMON` bytes, so read them back afterwards. If the board already
reloaded defaults itself (Error 0162), the `Setup` store is back to default but `PCI_COMMON` is a separate
variable that the checksum check does not read, so it may still hold your edit (inference). On Lenovo,
selecting Save Changes and Exit clears the 0162 entry from the POST event list
([Lenovo](https://pubs.lenovo.com/st50/POST_ERROR_0162), server page, same code).

**Rung 3 notes.** In the one thread that got this far the advice was: programmer, valid dump, revert the
NVRAM, reflash ([reported](https://winraid.level1techs.com/t/103135)).
If you own a programmer, a full dump taken *before* you start is the strongest backup you can have. The
reference machine had none, and no out-of-band recovery at all, so it relied on the guardrails in section 3
instead of a safety net. Many vendors also ship their own BIOS-recovery procedure (a key combination or a
recovery file on USB); this repo has not tested any, so check your manual.

### 2.1 Will a CMOS clear reset these variables? Do not assume yes

The variables are non-volatile, but non-volatile does not mean battery-backed. On at least some boards the
setup variables live in the NVRAM area of the same SPI flash chip as the firmware, and a CMOS jumper does not
erase that (the G5 reply above). The T3620 shows a subtler split: the battery pull got the board booting, and
the edit survived it. Booting again and reverting are different outcomes. Always read the variables back
after a clear instead of trusting that the board came up.

How to anticipate which kind of board you have, cheapest first:

1. **Plan around the backup, not the clear.** Treat a CMOS clear as a bonus. Your actual recovery is writing
   the backed-up variables back (section 2.2).
2. **Run a canary before the real write.** This is a suggested check; the reference build did not do it. With the
   card out, change one harmless visible BIOS option (a numlock default, a boot-splash toggle) to a
   non-default value and Save. Do the clear you would use for recovery. Boot and look: if the option
   reverted, a clear reloads defaults for the Setup store on this board. If it stayed, a clear will not undo
   your edit. Either way you have found the jumper or battery before you needed it. A pass does not prove the
   hidden `PCI_COMMON` bytes reset, so still read those back.
3. **Read the service manual for what the jumper does.** Some vendors document separate jumpers or key
   combinations for clearing CMOS, clearing NVRAM, and clearing passwords. This repo has not surveyed them.
4. **Rank the risk by what you touch.** A PCI-resource toggle is a different class from memory timing or CPU
   voltage, which are consumed early in boot and are the reported way of stranding a board.

### 2.2 Restoring from backup

Restore needs a board that boots to an OS. Run it as root, with the backup directory that
`above4g_unlock.py unlock --apply` or `uefivar.py backup` printed (`efivar-backup-<timestamp>`). The tools keep
each variable as a `<Name>-<guid>.bin` file with its four attribute bytes, plus a manifest, so the restore puts
the attributes back too.

```bash
python3 tools/restore.py /mnt/usb/backups/efivar-backup-<timestamp>                                  # dry run
python3 tools/restore.py /mnt/usb/backups/efivar-backup-<timestamp> --apply --i-understand-the-risk
```

`restore.py` refuses a backup without a valid manifest, a BIOS version or board that differs from this
machine, and a live variable whose attributes or data length differ. `--allow-length-change` skips only the
length check, to repair a variable that a failed write left at another size. It writes the variables in
manifest order (`Setup`, then its checksum, then `PCI_COMMON`), reads each back, and prints the exact command to
run again if it stops part way. Like `above4g_unlock.py`, it has been tested against a synthetic directory only
([tools/README](../tools/README.md)).

Restoring the backed-up Setup *and* the backed-up LastCheckSum returns the pair that booted before, so no
checksum arithmetic is needed. If Error 0162 appears anyway, the guard is doing its job: the board reloads
defaults and you are at a clean state, minus any BIOS settings you had customised. Write those down before you
start.

**Manual fallback, only if the tools cannot run.** It was not exercised on the reference build. It needs the
`<Name>-<guid>.bin` files from a tools backup or from the manual backup in 3.1. A manual backup has no
manifest, so `restore.py` would refuse it, and this loop is how you put it back. It writes with `dd` and a block
size larger than the file, so the blob goes out in one `write()`. A tool that copies in small blocks would split
`Setup` over several writes, and the last write replaces the earlier ones (section 1).

```bash
# As root, inside the backup directory. List the files in restore order: Setup, its checksum, then the rest.
E=/sys/firmware/efi/efivars
for f in Setup-<guid>.bin <checksum name>-<guid>.bin PCI_COMMON-<guid>.bin; do
  v=${f%.bin}
  [ -f "$E/$v" ] && [ -f "$f" ] || { echo "STOP: $v or $f not found"; break; }
  chattr -i "$E/$v" && dd if="$f" of="$E/$v" bs=1M && chattr +i "$E/$v" && cmp "$f" "$E/$v" \
    && echo "restored $v" || { echo "STOP: restoring $v failed; do not reboot"; break; }
done
```

The `cmp` is the re-read check: it compares the backup file with the variable as the firmware now reports it.
Use the names and GUIDs from your own backup and your own `/sys/firmware/efi/efivars`.

## 3. Guardrails a safe write must have

These are the checks the reference run met in whole or in part, plus the ones a review of that run found
missing. The last column is what the reference run actually did, so you can see which gaps were closed after
the fact.

`tools/above4g_unlock.py` and `tools/restore.py` implement these properties: the board fingerprint, the
verified backup, the size and attribute checks, one whole-blob write per variable, the live delta, the dry run
with a typed confirmation, the whole-blob diff, the automatic rollback and the scripted restore. Row 7 below,
the physical outcome, is yours to check after the reboot; `above4g_unlock.py verify` helps.

### 3.1 Non-negotiable

| # | Guardrail | Why | Reference run |
|---|---|---|---|
| 1 | **Fingerprint the board first.** Read `board_name`, `bios_version`, `product_name` from `/sys/class/dmi/id/` and refuse unless they match an offset-table entry, or you hold an IFR extraction from your exact BIOS build | Offsets move with each BIOS build, and matching variable *sizes* alone is a weak proxy: a build with the same sizes and shifted offsets would pass | Partly: variable lengths only (`0x1388`, 2, 7). The BIOS version was known by hand |
| 2 | **Back up every variable you touch**, with the 4-byte attribute prefix, before any write; re-read to verify; abort if a backup fails | The backup is your recovery when a CMOS clear does not help (2.1). Also copy any guard bookkeeping variables you found (on the reference: `LastSetupdataSum` and three flag variables) | Yes for the three touched variables; no for the bookkeeping ones |
| 3 | **Refuse a write whose size differs** from the variable length or the IFR field width | Section 1: neighbours overwritten with zeros, or a truncated Setup | Partly: blob lengths asserted; no field-width check |
| 4 | **Refuse authenticated or locked variables.** Decode the attribute bits: `0x1` NV, `0x2` boot-service, `0x4` runtime, `0x10` authenticated write, `0x20` time-based authenticated write. Anything with `0x10` or `0x20`: stop | Tools that "blindly" set a value fail with a security violation on those; a variable's attributes cannot be changed by overwriting ([Microsoft](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setfirmwareenvironmentvariableexa), [reported](https://winraid.level1techs.com/t/103324)) | Partly: `07 00 00 00` was read and printed, not asserted |
| 5 | **One whole-blob write per variable**, original attributes, checked return length | Section 1, second mechanism | Yes, with a weakness: the script used Python's buffered writer, which issued a single syscall only because 5,004 bytes fit its 8 KiB buffer. Use `os.write` and check the return value |
| 6 | **Measure the checksum delta live; never hard-code it** | The stored value and the OS-visible sum are not related by a constant (3.3) | Yes, bounded to 0..2 |
| 7 | **Verify by physical outcome**, not by reading the byte back (3.4) | A value can read back correctly and change nothing | Yes: card-out boot, 64-bit window, then card |
| 8 | **Stop on any non-zero write status and restore** (3.5) | A partial write is the worst state to leave a board in | Partly: the script aborted on error but had no scripted restore |

Make the backup with the tools: `python3 tools/uefivar.py backup Setup-<guid> <checksum name>-<guid>
PCI_COMMON-<guid> --dir /mnt/usb/backups`, as in [04, Phase 1.2](04-procedure.md#12-back-up-keeping-the-attribute-prefix), or let
`above4g_unlock.py unlock --apply` make it ([04, Phase 3.1](04-procedure.md#31-route-a-linux-efivarfs-preferred)).
Both save the attribute prefix, re-read the files and compare them with the live variables. On a live USB, put
the backup directory on a USB stick or a disk partition you have mounted, not on the live system's own
filesystem. That one lives in RAM and is gone after the reboot. The tool refuses it.

**Manual fallback, only if the tools cannot run.** It uses the tools' file names (`<Name>-<guid>.bin`) and
re-reads each copy with `cmp`. It stops at the first failure without closing your shell. The same rule applies
to `B`: it must be on a disk or USB stick that survives the reboot.

```bash
# As root. Setup first, then its checksum, then the rest.
E=/sys/firmware/efi/efivars
B=/mnt/usb/backups/efivar-backup-$(date +%Y%m%d-%H%M%S); mkdir -p "$B"      # /mnt/usb: a mounted USB stick or disk
for v in Setup-<guid> <checksum name>-<guid> PCI_COMMON-<guid>; do     # exact names from ls "$E"
  if cat "$E/$v" > "$B/$v.bin" && cmp "$E/$v" "$B/$v.bin"; then echo "ok    $v"; else echo "FAIL  $v: stop"; break; fi
done
```

The reference variables measured 5,004, 6 and 11 bytes on disk (5000, 2 and 7 data bytes, attributes
`07 00 00 00`). Yours come from your own board's table entry.

### 3.2 Also required

- **Preconditions asserted, not assumed.** Boot is UEFI with CSM off. The attributes are what you expect. Any
  guard flags are zero, and you have noted the event-log counter to compare against later. The old value at the
  target offset equals the expected default. Report Secure Boot and CSM state. Keep AC power stable during
  writes and stop other firmware writers such as `fwupd` (a precaution, not an observed failure).
- **Fail closed on visibility.** If the store is not listed in `efivarfs`, or its attributes lack runtime
  access, you are on the other route (a one-time UEFI-shell step) and nothing here applies unchanged.
- **Diff the whole blob.** Exactly the intended bytes differ between old and new; everything else is
  byte-identical. The reference script asserted three values and did not diff. `above4g_unlock.py` does.
- **Dry run by default.** Print the planned diff and the measured delta; require an explicit apply flag and a
  typed confirmation of board, BIOS version and offsets. `above4g_unlock.py` requires `--apply`,
  `--i-understand-the-risk` and a `--confirm` token made of the BIOS version and the offsets.
- **Scripted restore, tested before you need it.** The reference run had none. `tools/restore.py` is one. Run
  its dry run on your backup now, so you know it can read the backup before you need it.
- **Keep the group of writes tight** (3.3), and if the second write fails, immediately re-write the original
  Setup so the stored checksum and Setup match again.
- **Never delete a variable and never recreate one with different attributes.** The kernel marks
  non-standard variables immutable by default to prevent accidental removal, because removing them can make
  firmware fail to POST ([kernel docs](https://docs.kernel.org/filesystems/efivarfs.html)). A variable is
  deleted only by a write that carries the 4 attribute bytes and no data, and the tools refuse such a write.
  They open the file with `O_WRONLY|O_TRUNC` and no `O_CREAT`, and send the attributes plus the whole value in
  one `write()`. Opening or truncating the file does not by itself change the firmware variable: the change
  happens inside `write()` ([kernel source](https://github.com/torvalds/linux/blob/master/fs/efivarfs/file.c)).
- **Never offer a guard bypass.** The decoded PEI check returns quietly when `LastCheckSum` cannot be read, but
  that was observation only, never tested, and nothing is known about the stage that might recreate it.
- **Keep your backups private.** A Setup blob can carry system data; this repo has not inspected one for
  password hashes or asset tags. Do not post it.

### 3.3 Measure the checksum, do not assume it

The stored `LastCheckSum` and the OS-visible `sum16(Setup)` are **not equal by a constant**. The difference was
+1 on the day of the unlock and 0 about a month later, same machine, clean boots, no Error 0162 either time. The
mechanism is unknown; candidates (unverified) are a Setup byte the firmware maintains, or a rewrite triggered by
a hardware change. So you measure it before every write:
`python3 tools/uefivar.py checksum --setup Setup-<guid> --stored <checksum name>-<guid>`, and the new stored value is
`(sum(new_setup) + delta) & 0xFFFF`. The guard itself (the PEI sum, the exact compare, the Lenovo GUID) is
described in [03](03-how-it-works.md), section 3, and in [04, 2.3](04-procedure.md#23-what-the-reference-guard-does).

Accept a delta of 0 or 1 by default. Anything else means your model of the guard is wrong for this board, so
stop and re-analyse rather than widen the bound. The tools let you raise `checksum.max_delta` in the profile up
to 4 and refuse above that; raising it contradicts this advice. The reference script allowed 0 to 2; that bound
was a heuristic, not derived, and so is this one.

**Order of writes.** Three variable writes are not atomic. If power drops or the script dies after `Setup` and
before `LastCheckSum`, the next boot sees a mismatch and runs the 0162 reload. So write `Setup` and
`LastCheckSum` back to back with nothing between them, then `PCI_COMMON`, which the check does not read. That
keeps the window as short as it can be.

### 3.4 Verify by physical outcome

Reading a byte back proves the firmware accepted the write. It does not prove the setting does anything. A
ThinkStation P310 owner reads `PCI_COMMON` byte 3 as 1 while GPU-Z still reports 4G decoding off
([reported](https://winraid.level1techs.com/t/94916)), and the
[Hidden BIOS settings lesson](https://github.com/Kostr/UEFI-Lessons/tree/master/Lessons_uncategorized/Lesson_Hidden_BIOS_settings)
warns the option may not be used by the driver code at all.

Reboot **with the card out**, then check:

```bash
# 1. the board did not reload defaults (names are Lenovo's; other vendors differ or have none)
for v in SetupDataChecksumError RestoreSetupDataFlag BIOSSetupDataChangedFlag; do
  printf '%s=' "$v"; tail -c +5 /sys/firmware/efi/efivars/$v-* | xxd -p
done                                            # all 00; SysEventLogCount should not have increased

# 2. the edit persisted: re-read the bytes you changed and compare with your plan

# 3. the 64-bit root window exists, with no card installed
dmesg | grep -i 'root bus resource'             # expect a window above 4 GB
```

On the reference build step 3 showed `[mem 0x4000000000-0x7fffffffff window]` (256 GB) before the card was
ever installed. Only then did the card go in, and `lspci -vv` showed its 16 GB BAR inside that window at
`0x4400000000`. `nvidia-smi -q -d MEMORY` reports the BAR1 total for NVIDIA cards.

### 3.5 Stop conditions

Any non-zero status from a write is a stop. Restore all three variables, in the order in 2.2. Do not retry with
different attributes and do not delete anything. Linux folds many UEFI statuses together, so the errno alone
cannot tell a policy denial from a malformed buffer
([kernel source](https://github.com/torvalds/linux/blob/master/drivers/firmware/efi/efi.c)).

| errno | UEFI status | Meaning |
|---|---|---|
| `EROFS` | `EFI_WRITE_PROTECTED` | The variable is locked (VariablePolicy, VarCheck or VariableLock) |
| `EACCES` | `EFI_SECURITY_VIOLATION` | Authenticated or protected |
| `EINVAL` | `EFI_INVALID_PARAMETER`, and the default for anything unmapped, including `EFI_ACCESS_DENIED` | Bad buffer or a policy denial; you cannot tell which |
| `EPERM` | None: the filesystem refused to open the file | Not root, or the immutable flag is still set (`lsattr` shows `i`; the tools clear it themselves). Nothing was written. Fix it, then re-run the dry run |
| `ENOSPC` | `EFI_OUT_OF_RESOURCES` | Variable store full |
| `EIO` | `EFI_DEVICE_ERROR` | The flash write failed |
| `ENOENT` | `EFI_NOT_FOUND` | Wrong name or GUID |
| `EINTR` | `EFI_ABORTED` | The call was aborted |

A locked or authenticated Setup variable is the platform telling you no. It is not an obstacle to defeat. One
reported case: an `ACCESS_DENIED` write on an HP EliteDesk 800 G5 SFF
([thread](https://winraid.level1techs.com/t/96245)).
The reasons that thread leaves open (a variable policy, HP Sure Start, an SMM write path, authenticated
attributes) are unknown.

## 4. Do not

| Do not | Because |
|---|---|
| Copy another board's offsets | The reference offsets belong to one BIOS family. The M90q is a different family, and the "`PCI_COMMON` byte 3 is Above 4G" pattern across boards is a hypothesis to verify, not a lookup |
| Grep for an offset number or a prompt string and trust the hit | On the reference, a `grep` for the option name landed on the last line of the *previous* question's help text, whose own offset (`0x993`) is "Aperture Size", not the gate. `grep 0xA49` also matches an unrelated question ID. Read each question's own `VarOffset` |
| Write a whole Setup blob with the wrong attributes or length | Section 1. In `efivarfs` the first four bytes of what you write are the attributes. A blob without the prefix turns your data into attributes (my reading of the write path) |
| Skip the backup | It is the recovery that does not depend on a CMOS jumper |
| Leave `PCI_COMMON` byte 0 toggled on a guess | The reference build's first attempt set it, on the AMI "first field is byte 0" convention. It stayed set. What it controls is unknown (an earlier disassembly, not re-run, shows the PciBus driver registering a ReadyToBoot callback when it is 1) |
| Write bytes whose meaning you have not established | Same reason. A byte you cannot name is a byte you cannot predict |
| Touch memory, CPU, or voltage questions | The reported stranded board involved a memory setting, and a CMOS clear did not help |
| Write `Setup` on a checksum-guarded board with a tool that knows nothing about the checksum | `setup_var.efi`'s README documents no checksum handling, and `LastCheckSum` is not declared in the Setup IFR on the reference, so the tool cannot address it by name. That combination would be expected to trip 0162 (inference) |
| Use the Windows API with a size of zero | `SetFirmwareEnvironmentVariable` with `nSize` zero deletes the variable ([Microsoft](https://learn.microsoft.com/en-us/windows/win32/api/winbase/nf-winbase-setfirmwareenvironmentvariableexa)) |
| Install the card before the window is proven with it out | With the card in and the window closed, the board loops at POST and you cannot boot an OS to look. With it out you can read the window from Linux. The T3620 report matches: dead or looping with the card, fine without |
| Run a headless UEFI-shell script that does not end in `reset` | A script that stops at the shell prompt leaves a machine with no screen attached sitting there until someone power-cycles it. End every shell-route script with an unconditional `reset` |
