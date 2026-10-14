# Safety and scope

This project documents an **owner-side change to your own, out-of-warranty computer's BIOS settings**.
It is not a security exploit and has no "vulnerability" to report. It turns on a setting the OEM already
ships but leaves out of the menu, on hardware the manufacturer no longer supports.

## What this is

- A method and **guarded tools** for turning on a hidden firmware option, **Above 4G Decoding** /
  **Above 4GB MMIO**, by editing the firmware's own UEFI setup variables, so that a GPU with a large
  memory window (a datacenter card such as the NVIDIA A2 used for the reference build) can boot instead of
  failing with *"PCI Out of Resources"*.
- Where the vendor adds an integrity checksum over those variables, the tools measure it live and
  write a matching value, so the board keeps the change instead of reloading defaults.

## What this is not

- Not a way to touch hardware you do not own, bypass a firmware password, or defeat anyone else's
  security control. Above 4G Decoding is a standard PCIe setting; this makes a hidden copy of it
  reachable on your own board.
- Not malware or persistence. The tools read and write **your** machine's own UEFI variables, take a
  backup first, refuse authenticated (signed) variables, and ship no vendor firmware.

## The real risk is a board that will not boot

A wrong variable write can make the board refuse to POST or reload BIOS defaults. Recovery is not
guaranteed. The ways back are **Load Setup Defaults**, a **CMOS clear** and the variable backup the tools
make (`tools/restore.py`), and the evidence behind them is thin:

- Nothing on the reference build needed recovery. The outside evidence is two reports: a board that looked
  dead until a battery pull, and a laptop whose owner was told to use an SPI programmer after a
  wrong-field write.
- `tools/restore.py` needs a board that still boots to an OS.
- The reference board had no out-of-band recovery (reported, from an earlier probe).
- On some boards a CMOS clear does **not** reset these variables.

Read `docs/05-safety-and-recovery.md` before you write, keep the backup, and treat the first write as a
test with the card removed.

## Reporting a problem with the repo

If a **tool or document here is wrong** in a way that could strand someone's board (a bad offset, a
guard that passes when it should stop, a missing warning), open an issue. That is the kind of report
this project wants.
