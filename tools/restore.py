#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""restore.py - put variables back from a backup made by uefivar.py or above4g_unlock.py.

  python3 restore.py BACKUP_DIR                                   dry run: shows what would change
  python3 restore.py BACKUP_DIR --apply --i-understand-the-risk   restores

What it does, in order:
  1. loads manifest.json and checks it and every backup file against MANIFEST.sha256 (and
     each file against the manifest); a malformed or tampered backup is refused
  2. refuses unless the backup records a BIOS version and a board or product name, this
     machine's BIOS version can be read, and the two are equal; also refuses if the board
     name, product name or system vendor the backup recorded differ from this machine's
  3. refuses if a live variable's attributes differ from the backup's (attributes cannot be
     changed by overwriting a variable), or its data length does (--allow-length-change
     skips only the length check, for a variable a failed write left at another size)
  4. writes each variable that differs, in manifest order, one whole-blob os.write each with
     nothing printed in between and SIGINT, SIGTERM, SIGHUP and SIGQUIT ignored. The guided
     workflow lists Setup first, then its checksum variable, then PCI_COMMON, so Setup and
     the checksum are written back to back, then the rest.
  5. reads every restored variable back and compares it with the backup byte for byte

A backup that holds Setup without its checksum variable (or the reverse) is refused, because
restoring one of them alone can leave the pair disagreeing and the next boot raises Error 0162;
--no-checksum-companion is the escape, for boards with no checksum guard. Every target is opened
write-only first, so a variable that refuses writing is found before anything is written. A
successful whole-backup restore also clears a report.json that still says "writing".

Variables saved "for reference only" (flags, counters) are never written.
A dry run is the default. Exit codes: 0 ok, 2 refused, 3 STOP (a write or the read-back failed).
"""

import argparse
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import uefivar as uv  # noqa: E402


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--self-test" in argv:
        return uv.run_selftest()
    p = argparse.ArgumentParser(prog="restore.py", description=__doc__.split("\n\n")[0])
    p.add_argument("backup_dir", metavar="BACKUP_DIR", help="an efivar-backup-<timestamp> directory")
    p.add_argument("--efivars", default=uv.DEFAULT_EFIVARS, metavar="PATH",
                   help="efivarfs directory (default %(default)s)")
    p.add_argument("--dmi", default=uv.DEFAULT_DMI, metavar="PATH",
                   help="DMI directory (default %(default)s)")
    p.add_argument("--apply", action="store_true", help="really write; default is a dry run")
    p.add_argument("--i-understand-the-risk", dest="risk", action="store_true", help="required with --apply")
    p.add_argument("--allow-length-change", action="store_true",
                   help="do not refuse a live variable whose data length differs from the backup's; "
                        "only for repairing a variable that a failed write left at another size "
                        "(the BIOS and platform checks still apply)")
    p.add_argument("--no-checksum-companion", action="store_true",
                   help="allow restoring a Setup variable without its checksum variable, or the reverse: "
                        "only for boards with no checksum guard; on a guarded board it can leave the "
                        "pair disagreeing")
    a = p.parse_args(argv)
    return uv.restore_backup(a.efivars, a.backup_dir, apply=a.apply, risk_ok=a.risk, dmi_dir=a.dmi,
                             allow_length_change=a.allow_length_change,
                             no_checksum_companion=a.no_checksum_companion)


if __name__ == "__main__":
    sys.exit(main())
