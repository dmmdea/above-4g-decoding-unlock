# Tools

Three scripts, an example profile and a self-test. They read, back up, diff and (only when you ask) write the
UEFI variables that hold a hidden "Above 4G Decoding" / "Above 4GB MMIO" setting, through Linux `efivarfs`. They
are for your own out-of-warranty machine. The tools contain no board data: every offset, GUID and length
comes from a profile file that you fill in from your own firmware.

| File | What it is |
|---|---|
| `uefivar.py` | Guarded variable helper: `read`, `list`, `backup`, `checksum`, `diff`, `write`. Also the library the other scripts use. |
| `above4g_unlock.py` | Guided workflow on top of it: `unlock` (back up, measure the checksum, change the bytes, verify) and `verify` (after the reboot). |
| `restore.py` | Puts variables back from a backup. Dry run by default. |
| `profile.example.json` | **Example only: verify against YOUR firmware.** Values for one reference build. |
| `selftest.py` | Tests against a synthetic `efivars` directory, including every refusal path. |

Python 3.8 or newer, standard library only. Run the commands that touch real variables as root on the target
machine, from this directory. `diff`, the self-test and anything pointed at a synthetic directory need no root.

**What has been tested.** The self-test passes on Windows with Python 3.14 and on Linux with Python 3.12,
against a synthetic directory. This code has not been run against a real `efivarfs` mount or real firmware:
the immutable-flag handling (`chattr`/`lsattr`) and firmware behaviour are outside what a synthetic directory can
show. The write mechanics (one `write` call, original attributes, `chattr -i` first) follow the script that worked
on the reference build, verified there; the checks around it are new.

## Before you run anything

1. Boot Linux in UEFI mode with `efivarfs` mounted read-write (`mount -t efivarfs none /sys/firmware/efi/efivars`
   if it is not; [kernel docs](https://docs.kernel.org/filesystems/efivarfs.html)). The firmware menu must be
   set to UEFI-only boot with CSM off. The OS cannot read the CSM state, so the tools only remind you.
2. Run the self-test: `python3 selftest.py`. It must end with `SELF-TEST PASSED`.
3. Read [docs/05-safety-and-recovery.md](../docs/05-safety-and-recovery.md). Know the recovery ladder before
   the first write, not after.
4. Write down any BIOS settings you have customised. A checksum mismatch makes the firmware reload defaults.
5. Keep AC power stable and stop other firmware writers such as `fwupd` for the duration (a precaution, not an
   observed failure).
6. Test with the target card **physically absent**. The writes need no card, and a bad outcome is easier to
   recover from without one.

## Offsets must come from your own firmware

Offsets move between BIOS builds, and variables of the same size are not evidence that the layout matches. A
profile for one board is wrong for the next, and a wrong offset writes a different setting.

- Get the IFR dump of the exact BIOS build you run, with
  [UEFITool](https://github.com/LongSoft/UEFITool) and
  [IFRExtractor-RS](https://github.com/LongSoft/IFRExtractor-RS). Procedure:
  [docs/04-procedure.md](../docs/04-procedure.md); qualification:
  [docs/02-does-my-hardware-qualify.md](../docs/02-does-my-hardware-qualify.md).
- Read the `VarOffset` of the question that owns the setting. Do not grep for the label. On the reference
  build, a search for "Above 4GB MMIO" hit the last line of the help text of the previous question, which is
  Aperture Size at offset `0x993`, where the value 1 means a 256 MB aperture, not "Enabled". The first attempt
  read it as already enabled and so never wrote the real gate, which is the question at `0xA49`.
- "Above 4G Decoding" in `PCI_COMMON` is usually a dynamic question that the PCI bus driver appears to inject
  (inference), so it has no static IFR entry. Byte 3 is the common position on boards where people have reported it (see
  [ReBarUEFI discussion 211](https://github.com/xCuri0/ReBarUEFI/discussions/211) and the
  [P310 thread](https://winraid.level1techs.com/t/94916), both
  *reported, unconfirmed*). Treat that as a hypothesis for your board, not a lookup.
- **Do not write byte 0 of `PCI_COMMON`.** On the reference build the first attempt set it on a wrong guess
  about the "first field" convention. It did not fix the problem, and it stayed set afterwards because the
  second attempt kept it. What byte 0 controls is unknown.

`profile.example.json` is labelled as an example in three places and ships with `"verified_by_user": false`.
`above4g_unlock.py unlock --apply` refuses until you have checked every value against your own IFR and set it
to `true`.

## Guardrails

| Guardrail | Enforced by |
|---|---|
| Dry run by default; nothing is written without `--apply` | `uefivar.py write`, `above4g_unlock.py unlock`, `restore.py` |
| A real write also needs `--i-understand-the-risk` | all three |
| The workflow also needs `--confirm <BIOS version>:<offsets>` (the dry run prints it) and a profile with `"verified_by_user": true` | `unlock` |
| Mandatory backup, re-read and compared with the live variable. The re-read can be served from the page cache, so it checks the copy, not the disk; the `fsync`s below are what make it durable. `write` refuses unless the backup equals the variable as it is now, lists it as restorable (not reference-only) and was made on this BIOS version | `backup`, `write --backup`, `unlock` |
| The backup is durable: an `fsync` error is a refusal (every file, every directory the backup path needed created, and the directory above them). A backup directory on tmpfs, ramfs, aufs, squashfs, rootfs, or an overlay whose upper layer is RAM (or cannot be traced) is refused unless you pass `--allow-volatile-backup`. A live USB's root is such an overlay, so on a live system put the backup on a USB stick or a disk partition. The check reads `/proc/mounts` and is skipped where that file does not exist | `backup`, `unlock` |
| A backup needs a machine name as well as a BIOS string: `backup` and `unlock --apply` refuse when neither DMI `board_name` nor `product_name` can be read, and a restore refuses a manifest that recorded neither. The dry run of `unlock` only warns | `backup`, `unlock`, `restore.py`, `write` |
| A backup is verified before it is used: `MANIFEST.sha256` covers `manifest.json` and every blob and must match; every manifest field is type-checked; each entry's `file` must be exactly `<Name>-<guid>.bin`; `restore` must be present and a boolean; blob length, attributes and hash must match the manifest. Anything else is a refusal (exit 2), not a traceback | `restore.py`, `write`, `unlock` |
| Refuse a field write whose width is not 1, 2, 4 or 8 bytes, does not fit the value, runs past the end, or is wider than 1 byte without `--allow-multibyte` (the "write 8 bytes" mistake) | `write`, profile loader |
| Refuse if the data length is not what you stated (`--expect-var-len`, profile `data_length`) | `write`, `unlock` |
| Refuse authenticated variables (attribute bits `0x10`, `0x20`, `0x80`), hardware-error and append-write variables, unknown bits, a missing `RUNTIME_ACCESS` or `NON_VOLATILE` bit | `write`, `unlock`, `restore.py` |
| Refuse if the BIOS version (and board/product name, if the profile has them) differs from the profile; fail closed if DMI cannot be read | `unlock` |
| Restore and `write` refuse unless the backup recorded a BIOS version, this machine's can be read and the two are equal. The board name, product name and system vendor are recorded when readable, and a restore refuses if any recorded one differs. The same rule applies to a synthetic `--efivars` directory, using the `--dmi` directory you give | `restore.py`, `write`, automatic restore |
| Restore refuses a live variable whose data length differs from the backup's. `restore.py --allow-length-change` skips that one check, for a variable a failed write of these tools left at another size; the BIOS and platform checks still apply | `restore.py` |
| A whole-backup restore refuses a backup that holds a Setup variable without a checksum variable, or the reverse, because restoring one alone can leave the pair disagreeing (the Error 0162 state) and still exit 0. `restore.py --no-checksum-companion` is the escape, for boards with no checksum guard. The automatic rollback restores only what the run wrote and is not subject to this check. The printed restore command carries the flag when the backup needs it, and `backup` warns when it is asked to save a lone Setup | `restore.py`, `backup` |
| `write --apply` also needs `--expect-old`, `--expect-var-len` and `--expect-attrs` (a dry run notes it). Writing a Setup-family variable (any name containing `Setup`, any case) or any variable whose name contains `CheckSum` is refused unless you pass `--no-checksum-companion`, which is for boards with no checksum guard. The match is deliberately broad: matching too much only adds a flag requirement. On a guarded board use `above4g_unlock.py` | `write` |
| Every write target is opened write-only (without truncating) before the first write, so a variable that refuses writing is found with nothing written. The automatic rollback uses the backup the run already verified instead of reading it and the DMI again | `unlock`, `write`, `restore.py` |
| Refuse unless the target byte holds its expected old value | `write --expect-old`, `unlock` |
| Measure the checksum delta live and refuse outside the profile bound (default 0 to 1); a negative delta wraps and is refused | `unlock` |
| Refuse if a guard flag variable is non-zero | `unlock` |
| One whole-blob `os.write` per variable, return value checked, original attributes, never creates or deletes a variable | `uefivar.raw_write` |
| Write order: Setup, then the checksum immediately, then `PCI_COMMON` | `unlock`; `restore.py` follows the manifest order |
| Nothing is printed between two writes, and `SIGINT`, `SIGTERM`, `SIGHUP` and `SIGQUIT` are ignored for the whole write window (and, in `unlock`, the STOP handling that follows). If a handler cannot be installed (off the main thread) a warning says so before the first write. Output errors such as a closed pipe are swallowed, and text the terminal cannot encode is printed again with the odd characters escaped, so a `STOP` or `DO NOT REBOOT` line is never lost. Any exception, `KeyboardInterrupt` included, ends the loop and goes to the STOP path. `SIGKILL` and a power cut cannot be caught, so `unlock` leaves a mark (next row) | `write`, `unlock`, `restore.py` |
| Before the first write `unlock` writes `report.json` in the backup directory with status `writing` (temp file, `fsync`, rename, `fsync` of the directory) and rewrites it at the end. `unlock` (dry run or apply, when given `--backup-dir`) and `verify --report` stop with exit 3 and the restore command if a report still says `writing`: a run was killed or lost power inside the write window, and Setup and its checksum may disagree even though a fresh look shows nothing to do. A whole-backup `restore.py` that succeeds clears the mark; to keep the live variables as they are, move that backup directory away | `unlock`, `verify`, `restore.py` |
| After the writes, diff the whole blob against the backup. Fail if any byte outside the intended ones changed | `write`, `unlock` |
| Any non-zero write result or interrupt prints `STOP`, the errno meaning, what each variable holds now, and the exact restore command (absolute path of `restore.py`, absolute backup path, the `--efivars` and `--dmi` in use). Whenever Setup and its checksum may disagree it also prints `DO NOT REBOOT` | `write`, `unlock`, `restore.py` |
| Rollback is decided from the live variables, not from the record of writes. If Setup and the checksum disagree, or one cannot be read, the backup is written back, Setup first and the checksum right after. If they are still a consistent pair (for example `PCI_COMMON` failed after both were written) nothing is rolled back, because a rollback is two more writes and a failure between them is the one state that costs BIOS settings. `write` restores only the variable it wrote | `unlock`, `write` |
| `chattr` is called only when `--efivars` is a real efivarfs mount, and the immutable flag is set again afterwards | `uefivar.Unlocked` |

What the tools cannot do. They cannot see a variable lock before the write: it shows up as an errno when the write
is refused, which is why the backup and restore come first. They cannot know a field's true width, so you state it.
They cannot read CSM. And a value that reads back correctly proves only that the firmware accepted the write, not that
the setting does anything.

## uefivar.py

Every subcommand takes `--efivars PATH` (default `/sys/firmware/efi/efivars`). Variables are named
`NAME-GUID`, exactly as in the directory.

```bash
S=Setup-ec87d643-eba4-4bb5-a1e5-3f3e36b20da9               # use the names and GUIDs from YOUR efivars directory
L=LastCheckSum-e102c8aa-0f10-4a0e-90a7-a6f816089bb7
P=PCI_COMMON-aca9f304-21e2-4852-9875-7ff4881d67a5

sudo python3 uefivar.py list --grep 'sum|crc|hash|integrity|error|restore|changed'   # look for guard variables
sudo python3 uefivar.py read $S --offset 0xa40 --length 32     # attributes, length, sum16, hex dump (or --hex)
sudo python3 uefivar.py backup $S $L $P --dir ./backups --reference-only SomeFlagVariable-<guid>
sudo python3 uefivar.py checksum --setup $S --stored $L        # sum16, stored value, and the DELTA
python3 uefivar.py diff ./backups/efivar-backup-<timestamp>/$S.bin $S    # byte diff; exit 1 if different
```

`backup` writes `<Name>-<guid>.bin` files (attribute prefix included, so they restore as they are), a
`manifest.json` and a `MANIFEST.sha256` (which lists the manifest as well as every blob), into a new
`efivar-backup-<timestamp>` directory with mode 0700. Each file is `fsync`ed, then every directory the backup path
needed created and the directory above them; a failed `fsync` is a refusal. The backup is then re-read and compared with
the live variables, but that read can come from the page cache, so it checks the copy, not the disk. A `--dir` on tmpfs,
ramfs, aufs, squashfs, rootfs or an overlay with a RAM (or untraceable) upper layer is refused, because the backup would
be gone after a reboot: a live USB's root is such an overlay, so on a live system put `--dir` on a USB stick or a disk
partition. `--allow-volatile-backup` accepts it anyway. The manifest records the BIOS version and the board name,
product name and system vendor from `--dmi` (default `/sys/class/dmi/id`) when readable; `backup` refuses if neither
the board name nor the product name can be read, and warns if no BIOS version could
be read, because `restore.py` refuses a backup that records none. List the Setup variable first, then its checksum
variable, then the rest: that is the restore order. `backup` warns if you list a Setup variable without a checksum
variable (or the reverse): `restore.py` refuses such a backup unless you pass `--no-checksum-companion`.
`--reference-only` saves variables for the record (flags,
counters) that restore never writes, and `write` refuses to treat such an entry as a restore point. The Setup blob
can hold system data; this project has not inspected one for password hashes or asset tags, so keep backups private
and do not post them.

`write` changes one field and is a dry run until `--apply --i-understand-the-risk`. A real write also needs
`--expect-old`, `--expect-var-len` and `--expect-attrs`, and a `--backup` that equals the variable now, lists it as
restorable and was made on this machine's BIOS version:

```bash
# The offset 0x3 and the length 7 are the reference build's PCI_COMMON byte and size.
# Use the offset and length from YOUR IFR and YOUR efivars listing; do not copy these.
sudo python3 uefivar.py write $P --offset 0x3 --value 1 --expect-old 0 \
     --expect-var-len 7 --expect-attrs 0x7 --backup ./backups/efivar-backup-<timestamp>
```

`write` refuses a Setup-family variable (any name containing `Setup`, any case) and any variable whose name contains
`CheckSum`, unless you also pass `--no-checksum-companion`. That flag is only for a board with no checksum guard: on a guarded board a Setup-only write
is expected to make the next boot raise Error 0162 and reload BIOS defaults, and this subcommand writes no checksum. On a
guarded board use `above4g_unlock.py`, which writes the checksum immediately after Setup. If a write fails or is
interrupted, `write` puts back only the variable it wrote.

The dry run prints the attributes, the length, `old -> new` for the field and every refusal it found. `--size`
is the field width in bytes (default 1). A wider field needs `--allow-multibyte`, after you read the width in the
IFR: the low two bits of `Flags` are 0 for 1 byte, 1 for 2, 2 for 4, 3 for 8
([EDK2 header](https://github.com/tianocore/edk2/blob/master/MdePkg/Include/Uefi/UefiInternalFormRepresentation.h)).
This is the guard against the "write 8 bytes" mistake: a forum command for the M720q passes `-s 0x08`, and in the
`setup_var.efi` 0.2.x syntax `-s` is a byte count (see [docs/09](../docs/09-tooling-and-prior-art.md#setup_varefi-datasone)).

For reading the dump, the data offsets in `read`, `write` and the profile are offsets into the variable's data,
not into the file. The file has four extra attribute bytes in front.

### Write errors

On any non-zero result the tools print `STOP`, the errno name and its meaning, what each variable holds now, and the
exact restore command; they restore automatically when that is the safe thing (see the next section). The meanings of
`EROFS`, `EACCES`, `EINVAL`, `ENOSPC` and `EIO` are in the table in
[docs/05-safety-and-recovery.md, section 3.5](../docs/05-safety-and-recovery.md#35-stop-conditions). Linux folds several
UEFI statuses into one errno, so the errno alone cannot always tell a policy denial from a malformed buffer. One more
is specific to these tools: `EPERM` means you are not root or the immutable flag is still set. Fix that, then re-run the
dry run. A locked Setup variable is the platform refusing. It is not an obstacle for this tool to defeat.

## The checksum and the write order

How the Lenovo guard works, with its evidence labels, is in
[docs/03, section 3](../docs/03-how-it-works.md#3-the-vendor-integrity-guard). In short, the stored sum is a separate
variable, `LastCheckSum`, under a Lenovo GUID, and it is **not** `sum16(Setup)` as the OS sees it. The difference was +1
on the day of the unlock and 0 on a later date, on the same machine. What the tools do about it:

- They measure the difference every time and write `new LastCheckSum = sum16(new Setup) + delta`, modulo 65536. The bound
  in the profile (`max_delta`, default 1, hard cap 4) is a safety limit, not the expected value.
- **Raising `max_delta` above 1 contradicts docs [04](../docs/04-procedure.md) and
  [05](../docs/05-safety-and-recovery.md)**, which say to stop and re-analyse rather than widen the tolerance. The
  original one-off script on the reference build accepted a delta of 0 to 2; these tools accept 0 or 1 by default.

The three writes are not atomic. If something stops the run after `Setup` and before `LastCheckSum`, the next boot is
expected to see a mismatch and reload defaults (from the firmware analysis; never provoked on the reference build). The
tools write `Setup` and `LastCheckSum` back to back with nothing between them, not even a line of output, with
`SIGINT`, `SIGTERM`, `SIGHUP` and `SIGQUIT` ignored, then `PCI_COMMON`, which the check does not read. What the tools do
when something goes wrong depends on where the pair stands, read from the live variables:

- **Setup written, `LastCheckSum` not (a failed or interrupted write, or a failed check of either).** The pair
  disagrees. The tools print what each variable holds, say `Do NOT reboot`, and write the backup back, Setup first
  and the checksum right after. If that restore also fails they print `DO NOT REBOOT` and the exact command to run.
- **`PCI_COMMON` failed, or only its check failed, after Setup and `LastCheckSum` were written.** The two are a
  consistent pair, so a reboot should not raise Error 0162. The tools do **not** roll back: a rollback is two more
  writes, and a failure between them would create the mismatch. They print `STOP`, the errno meaning, a statement
  that the pair is consistent, and the exact restore command for you to run when you choose.
- **The Setup write itself failed and nothing changed.** Both still hold their original values; nothing is rewritten.

Every `STOP` ends with the exact restore command, for example
`python3 /path/to/tools/restore.py /path/to/efivar-backup-<timestamp> --efivars /sys/firmware/efi/efivars --dmi /sys/class/dmi/id --apply --i-understand-the-risk`.

The tools never delete a variable, and `unlock` writes the checksum whenever the profile declares one (`checksum.model`
`none` is for firmware with no guard and needs your written evidence). `uefivar.py write` can change Setup or a
checksum variable on its own only when you pass `--no-checksum-companion`, which exists for boards that have no
checksum guard; it is refused otherwise.

## above4g_unlock.py

```bash
# 1. dry run: reads everything, changes nothing, prints the plan and the confirmation token
sudo python3 above4g_unlock.py unlock --profile my-board.json --only setup

# 2. real run, with every confirmation the dry run asked for
sudo python3 above4g_unlock.py unlock --profile my-board.json --only setup \
     --apply --i-understand-the-risk --backup-dir ./backups --confirm <token from the dry run>

# 3. reboot with the card absent, then:
sudo python3 above4g_unlock.py verify --profile my-board.json --only setup --report ./backups/efivar-backup-<timestamp>
```

`unlock` runs these steps and stops at the first failure:

1. Validate the profile (fail closed on anything missing or unknown).
2. Fingerprint: BIOS version, variable names, lengths, attributes, and the expected old byte values.
3. Read the guard flag variables (must be zero) and record the watch counters.
4. Measure `delta`, refuse outside the bound.
5. Print the plan. A dry run ends here, with the post-reboot steps.
6. With `--apply`: back up the three variables plus the flag, counter and extra variables, `fsync` the backup and
   re-read and compare it (a page-cache read: it checks the copy, not the disk). A backup directory on tmpfs, ramfs
   or a live system's RAM-backed root is refused unless `--allow-volatile-backup`, and so is a machine whose DMI
   shows neither a board name nor a product name (the dry run only warns about that).
7. Write `report.json` into the backup directory with status `writing`, `fsync` it, and prove every target can be
   opened for writing. Then write `Setup`, `LastCheckSum`, `PCI_COMMON` in that order. Diff the whole blob against the
   backup. Compare the stored checksum with `sum16(live Setup) + delta`. Any failure, difference or interrupt: `STOP`,
   exit 3, and the restore command. Whether the backup is written back automatically depends on the state of Setup and
   `LastCheckSum`; see the checksum section.
8. Rewrite `report.json` with the outcome and print the steps for after the reboot. If the process is killed or the
   power goes before this step, the report still says `writing`, and the next `unlock --backup-dir` or
   `verify --report` stops on it instead of reporting that there is nothing to do.

`--only` names a field the profile has. `--only pci_common` on a profile without `fields.pci_common` is a refusal
(exit 2), not a crash.

`--only setup` first, then `--only pci_common` if the root window did not appear, is the cautious order. The
reference build changed both together, so whether byte 3 of `PCI_COMMON` was needed there is **unproven**. The
64-bit root window appeared before any card was installed, which points at the Setup gate. That is a reading of
the evidence, not a result.

`verify` is read-only. Run it after the reboot, with the card still absent. It re-reads the bytes, the checksum
relation, the flags and the watch counters, and lists any `PCI Bus` range above 4 GiB in `/proc/iomem` (`--iomem PATH`;
read as root, otherwise the addresses read as zero). A byte that reads back correctly shows only that the firmware
accepted the write. What to check next, and the physical outcome that proves the unlock, is in
[docs/04, phase 4](../docs/04-procedure.md#phase-4-reboot-without-the-card) and
[phase 5](../docs/04-procedure.md#phase-5-install-the-card). Do not assume a CMOS clear resets the variable store on your
board ([docs/05](../docs/05-safety-and-recovery.md)).

### Profile file

JSON, so it runs on any Python 3 that Linux ships. Keys that start with `_` are comments.

| Key | Meaning |
|---|---|
| `profile_version` | Must be `1` |
| `verified_by_user` | `true` only after you checked every value against your own IFR. `--apply` refuses otherwise |
| `firmware.bios_version` | String, or list of strings, that `/sys/class/dmi/id/bios_version` must equal. Optional `board_name`, `product_name` |
| `variables.setup / checksum / pci_common` | `name`, `guid`, `data_length` (data bytes, not file bytes), optional `attrs` |
| `fields.setup / pci_common` | `offset`, `width` (default 1), `expect_old`, `new`, `label`, optional `allow_multibyte` |
| `checksum.model` | `sum16-le-live-delta`, or `none` with an `evidence` string saying how you know there is no guard |
| `checksum.max_delta` | Largest accepted delta, 0 to 4 (default 1). Raising it above 1 contradicts docs 04 and 05: stop and re-analyse instead |
| `guard_flags` | Variable names (or `NAME-GUID`) that must be zero before and after |
| `watch` | Variable names recorded before the write and compared after the reboot |
| `also_backup` | Variable names saved for the record, never restored |

A name that is missing on your firmware produces a warning. A name that matches more than one variable is refused
until you give `NAME-GUID`.

## restore.py

```bash
sudo python3 restore.py ./backups/efivar-backup-<timestamp>                                  # dry run
sudo python3 restore.py ./backups/efivar-backup-<timestamp> --apply --i-understand-the-risk
```

It checks `manifest.json` and every backup file against `MANIFEST.sha256` and the manifest, and refuses a malformed
or tampered backup. It refuses unless the backup recorded a BIOS version, this machine's BIOS version can be read, and
the two are equal; it also refuses if the board name, product name or system vendor that the backup recorded differ.
It also refuses a manifest that recorded neither a board name nor a product name, and a whole-backup restore of a
backup that holds a Setup variable without a checksum variable (or the reverse), since restoring one alone can leave
the pair disagreeing while the command still exits 0; `--no-checksum-companion` overrides that, for boards with no
checksum guard. It refuses if a live variable's attributes differ from the backup's (attributes cannot be changed by
overwriting) or its data length does. It opens every variable it is about to write, write-only, before the first
write, and refuses with nothing written if one will not open. It then writes the variables that differ in manifest order (Setup, checksum, then `PCI_COMMON`
when the backup was listed that way), printing nothing between writes and ignoring `SIGINT`, `SIGTERM`, `SIGHUP` and
`SIGQUIT` until they are done, and reads each back to compare byte for byte. If a restore stops part way, it prints
which variables were restored and which were not, the exact command to run again, and `DO NOT REBOOT` when Setup and
its checksum may disagree.

`--efivars` and `--dmi` default to the real paths. `--allow-length-change` skips only the length check, for a variable
that a failed write of these tools left at another size; the BIOS and platform checks still apply, and the printed
restore command adds the flag by itself when a live length differs from the backup's. `--no-checksum-companion`
skips only the Setup-without-checksum check, and the printed command adds it when the backup needs it. A whole-backup
restore that succeeds also clears a `report.json` that still says `writing` (see `unlock`).

If the board will not boot at all, restoring needs a working OS, so see the recovery ladder in
[docs/05-safety-and-recovery.md](../docs/05-safety-and-recovery.md).

## Self-test

```bash
python3 selftest.py            # or: python3 uefivar.py --self-test     or: python3 -m unittest selftest
```

It builds a synthetic `efivars` directory in a temporary folder, runs every command against it, and deletes it. It
fails if `chattr` is ever called. It covers read, list, backup (including a tampered backup), checksum with deltas of
+1, 0 and -1, diff, dry run, apply, restore, the write order, the errno messages for `EROFS`, `EACCES` and `EINVAL`,
a short write, automatic restore when the checksum write fails, a `PCI_COMMON` failure that must leave the new pair
alone, a rollback that itself fails, and a stray byte appearing after a write. It injects a `KeyboardInterrupt` and a
`BrokenPipeError` raised by `print` between the Setup write and the checksum write, and checks that the STOP path
runs, nothing is printed between writes, signals are ignored during every write and put back after. Refusals
covered: wrong size or width, missing or stale backup, delta out of range (including a wrapped negative delta),
authenticated attribute bits, wrong length, wrong old value, wrong BIOS version, unreadable DMI, non-zero guard flag,
missing confirmations, an unverified profile, a restore onto a variable of another length, a backup with no recorded
BIOS version, another board, product or vendor, a swapped or tampered manifest, a malformed manifest, a failed
`fsync`, a backup on tmpfs, `write --apply` without its expectations, a Setup write without
`--no-checksum-companion`, and a reference-only backup entry used as a restore point. The second review round adds a
restore of a Setup-only (or checksum-only) backup, live-USB overlay roots, a run killed with `os._exit` inside the write
window (the `writing` report, and what `unlock`, `verify` and `restore.py` do with it), Setup-family names, a machine
with no readable board or product name, directories created for `--dir`, text the terminal cannot encode, `--only`
without the field, an unwritable target found before the first write, the rollback surviving the DMI vanishing, and the
signal warning.

The tests replace the function that reads `/proc/mounts`, so a temporary directory on tmpfs does not trip the
volatile-backup refusal.

## Flag reference

Every flag from `--help`, by command. `-h` and `--help` work everywhere, and `--self-test` runs the self-test from any of
the three scripts. Offsets are offsets into the variable's data, not the file.

| Command | Flag | Meaning |
|---|---|---|
| `uefivar.py read NAME-GUID` | `--efivars PATH` | efivarfs directory (default `/sys/firmware/efi/efivars`); a synthetic directory of `<Name>-<guid>` files works for testing. Every subcommand takes it |
| | `--offset OFFSET`, `--length LENGTH` | start and length of the slice to show |
| | `--hex` | print only the data, as one hex string |
| `uefivar.py list` | `--grep REGEX` | case-insensitive filter on the file name, for looking for guard variables (see the example above) |
| `uefivar.py backup NAME-GUID ...` | `--dir DIR` | parent directory for the backup (required) |
| | `--dmi PATH` | DMI directory (default `/sys/class/dmi/id`). Also on `write`, `unlock`, `verify` and `restore.py` |
| | `--reference-only NAME-GUID ...` | variables saved for the record that restore never writes (flags, counters) |
| | `--allow-volatile-backup` | accept a `--dir` that is gone after a reboot (tmpfs, ramfs, aufs, squashfs, rootfs, or an overlay with a RAM upper layer such as a live USB's root). On a live system put `--dir` on a USB stick or a disk partition instead |
| `uefivar.py checksum` | `--setup NAME-GUID`, `--stored NAME-GUID` | the Setup variable and the stored checksum variable (both required) |
| `uefivar.py diff FILE_A FILE_B` | `--raw` | the files hold plain data, no 4-byte attribute prefix |
| | `--max-lines N` | most differing offsets to list (default 200) |
| `uefivar.py write NAME-GUID` | `--offset OFFSET`, `--value VALUE` | byte offset into the data, and the new value of the field (both required) |
| | `--size SIZE` | field width in bytes, from your IFR (default 1) |
| | `--allow-multibyte` | confirm a field wider than 1 byte; check the width in your IFR first |
| | `--expect-old`, `--expect-var-len`, `--expect-attrs` | refuse unless the field, the data length and the attributes match; all three are required with `--apply` (reference build attributes: `0x7`) |
| | `--backup DIR` | backup directory from `backup`; its copy must equal the live variable now, and its BIOS version must match this machine's |
| | `--no-checksum-companion` | allow writing a Setup-family or checksum variable on its own: only for boards with no checksum guard; on a guarded board use `above4g_unlock.py` |
| | `--apply`, `--i-understand-the-risk` | really write (the default is a dry run); the second flag is required with `--apply` |
| `above4g_unlock.py unlock` | `--profile FILE` | your per-board profile, JSON (required). `profile.example.json` is an example only |
| | `--only LIST` | `setup`, `pci_common` or both (default: every field the profile defines). `--only setup` first is the cautious order. Naming a field the profile lacks is a refusal |
| | `--backup-dir DIR` | parent directory for the backup (required with `--apply`). Given on a dry run too, it makes `unlock` stop if a report in that directory still says `writing` |
| | `--allow-volatile-backup` | accept a `--backup-dir` that is gone after a reboot (tmpfs, ramfs, a live system's RAM-backed root, as for `backup --dir`) |
| | `--confirm TOKEN` | the token the dry run prints (`<BIOS version>:<offset>:<offset>`) |
| | `--apply`, `--i-understand-the-risk` | as for `uefivar.py write` |
| | `--efivars PATH`, `--dmi PATH` | as above |
| `above4g_unlock.py verify` | `--profile FILE`, `--only LIST`, `--efivars PATH`, `--dmi PATH` | as for `unlock` |
| | `--report DIR_OR_FILE` | the backup directory (or its `report.json`) from the `unlock` run. A report that still says `writing` stops `verify` with exit 3 |
| | `--iomem PATH` | memory map to read (default `/proc/iomem`) |
| `restore.py BACKUP_DIR` | `--efivars PATH`, `--dmi PATH` | as above |
| | `--apply`, `--i-understand-the-risk` | really write (the default is a dry run); the second flag is required with `--apply` |
| | `--allow-length-change` | do not refuse a live variable whose data length differs from the backup's; only for repairing a variable that a failed write left at another size (the BIOS and platform checks still apply) |
| | `--no-checksum-companion` | allow restoring a backup that holds a Setup variable without a checksum variable, or the reverse: only for boards with no checksum guard; on a guarded board it can leave the pair disagreeing |

## Exit codes

| Code | Meaning |
|---|---|
| 0 | Done, or a dry run found nothing wrong |
| 1 | `diff` found differences |
| 2 | Refused, bad input, or (for `verify`) a failed check. Nothing was written |
| 3 | `STOP`: a write or its verification failed, the run was interrupted, or (`unlock`, `verify`) a report still says `writing`. Read the output: it says what each variable holds, whether a restore ran, and prints the exact restore command |

## Not known

The open questions are listed once, in [docs/06, section 11](../docs/06-reference-build.md#11-what-is-still-unproven):
whether `PCI_COMMON` byte 3 is required, what byte 0 controls, why the checksum delta moved, what `LastSetupdataSum` is
for, whether the early check skips S3 resume, and whether sibling models share the offsets. For other makes and models
see the tables in [docs/02](../docs/02-does-my-hardware-qualify.md#known-platforms). `PCI_COMMON` byte 0 was still 1
on 2026-10-07, the last time the reference machine was read.

## Reference build

A Lenovo ThinkCentre M720q running BIOS M1UKT72A, with a 16 GB NVIDIA A2. An AI coding agent did the work: the image
analysis ran on its own computer, and the live variable reads and writes ran over SSH. The owner's only hands-on steps
were physical: installing the card and removing it again. The values in `profile.example.json` come from that build,
verified there. The worked example is in [docs/06-reference-build.md](../docs/06-reference-build.md).
