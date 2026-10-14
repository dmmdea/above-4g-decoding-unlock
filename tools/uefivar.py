#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""uefivar.py - a guarded helper for UEFI variables exposed through efivarfs.

Subcommands:
  read      show attributes, length and a hex dump of one variable
  list      list the variables in the efivarfs directory (optionally filtered)
  backup    copy raw variables into a timestamped directory, with a manifest
  checksum  compute sum16 over a Setup variable, read the stored checksum
            variable and print the DELTA between them (measured, never assumed)
  diff      byte diff of two variable dumps
  write     change one field of one variable. DRY RUN unless --apply. A real write
            needs --expect-old, --expect-var-len and --expect-attrs, a backup made on
            this BIOS version, and, for Setup or a checksum variable,
            --no-checksum-companion (only for boards with no checksum guard).

Every subcommand takes --efivars PATH (default /sys/firmware/efi/efivars), so the
whole tool can be exercised against a synthetic directory of <Name>-<guid> files
(4-byte little-endian attribute prefix + data). chattr is only ever called when
PATH is a real efivarfs mount.

Run "python3 selftest.py" (or "python3 uefivar.py --self-test") before trusting it
on a real machine. Python 3.8 or newer, standard library only.

This module is also the library used by above4g_unlock.py and restore.py.

Exit codes: 0 ok / dry run clean, 1 diff found differences, 2 refused or bad
input, 3 STOP (a write or its verification failed; read the output).
"""

import argparse
import datetime
import errno
import hashlib
import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))

DEFAULT_EFIVARS = "/sys/firmware/efi/efivars"
DEFAULT_DMI = "/sys/class/dmi/id"

EXIT_OK = 0
EXIT_DIFF = 1
EXIT_REFUSED = 2
EXIT_STOP = 3

ATTR_BITS = (
    (0x01, "NON_VOLATILE"),
    (0x02, "BOOTSERVICE_ACCESS"),
    (0x04, "RUNTIME_ACCESS"),
    (0x08, "HARDWARE_ERROR_RECORD"),
    (0x10, "AUTHENTICATED_WRITE_ACCESS"),
    (0x20, "TIME_BASED_AUTHENTICATED_WRITE_ACCESS"),
    (0x40, "APPEND_WRITE"),
    (0x80, "ENHANCED_AUTHENTICATED_ACCESS"),
)
AUTH_BITS = 0x10 | 0x20 | 0x80
KNOWN_BITS = 0xFF

GUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

# Linux maps several EFI statuses onto the same errno. EINVAL is also the default for
# any status it does not list (EFI_ACCESS_DENIED included), so the errno alone cannot
# separate a policy denial from a malformed buffer. Source for the mapping: the
# efi_status_to_err() function in drivers/firmware/efi/efi.c of the Linux kernel.
ERRNO_TEXT = {
    errno.EROFS: "write-protected (EFI_WRITE_PROTECTED): the variable, or the whole store, is "
                 "read-only. A lock or variable policy is the usual cause. Do not try to get around it.",
    errno.EACCES: "security violation (EFI_SECURITY_VIOLATION): the variable is authenticated or "
                  "protected. Do not try to get around it.",
    errno.EPERM: "operation not permitted: you are not root, or the immutable flag is still set "
                 "(chattr -i did not take effect).",
    errno.EINVAL: "invalid parameter (EFI_INVALID_PARAMETER), or any other status the kernel does not "
                  "map, a policy denial (EFI_ACCESS_DENIED) among them. The errno cannot tell "
                  "which. Stop and restore.",
    errno.ENOSPC: "out of resources (EFI_OUT_OF_RESOURCES): the variable store is full.",
    errno.EIO: "device error (EFI_DEVICE_ERROR): the firmware failed the write.",
    errno.ENOENT: "not found (EFI_NOT_FOUND): the variable is gone.",
    errno.EINTR: "aborted (EFI_ABORTED).",
}


def say(msg=""):
    """print() that never raises. A closed pipe or a vanished terminal (an SSH drop) must not
    abort a write sequence half way, so output errors are swallowed here."""
    try:
        print(msg)
        sys.stdout.flush()
    except UnicodeEncodeError:
        # A locale that cannot encode the text must not lose the rest of the run's output, the
        # STOP and DO NOT REBOOT lines above all. Say it again with the odd characters escaped.
        try:
            enc = getattr(sys.stdout, "encoding", None) or "ascii"
            print(str(msg).encode(enc, "backslashreplace").decode(enc))
            sys.stdout.flush()
        except (OSError, ValueError):
            _silence_stdout()
    except (OSError, ValueError):               # BrokenPipeError is an OSError; Windows reports EINVAL
        _silence_stdout()


def _silence_stdout():
    """After an output error, point stdout at the null device so the interpreter's own flush at
    exit does not fail as well (it would turn a finished run into exit status 120). Only touches
    the real process stdout."""
    try:
        if sys.stdout is sys.__stdout__:
            fd = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(fd, sys.stdout.fileno())
            finally:
                os.close(fd)
    except (OSError, ValueError, AttributeError):
        pass


class IgnoreSignals(object):
    """Ignore SIGINT, SIGTERM, SIGHUP and SIGQUIT for the length of a block, then put the
    previous handlers back. A write window needs this: Setup and its checksum must both be
    written, and a Ctrl-C or an SSH drop between the two writes leaves them disagreeing, so the
    next boot reloads defaults. Signals that do not exist on this platform are skipped, and so
    is everything when the block does not run in the main thread (signal.signal refuses there).
    Re-entrant."""

    NAMES = ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT")

    def __init__(self):
        self.saved = []

    def __enter__(self):
        failed = []
        for name in self.NAMES:
            sig = getattr(signal, name, None)
            if sig is None:
                continue
            try:
                self.saved.append((sig, signal.signal(sig, signal.SIG_IGN)))
            except (ValueError, OSError):
                failed.append(name)
        if failed:
            say("warning: could not ignore %s (handlers can only be set from the main thread). An interrupt "
                "during the write window could split Setup from its checksum." % ", ".join(failed))
        return self

    def __exit__(self, *exc):
        for sig, prev in self.saved:
            try:
                signal.signal(sig, signal.SIG_DFL if prev is None else prev)
            except (ValueError, OSError, TypeError):
                pass
        self.saved = []
        return False


class VarError(Exception):
    """A variable could not be read or parsed, or an argument is unusable."""


class ShortWrite(Exception):
    """os.write accepted fewer bytes than the blob."""


def parse_int(text, what="value"):
    try:
        return int(str(text), 0)
    except ValueError:
        raise VarError("%s %r is not a number (decimal or 0x-prefixed hex)" % (what, text))


def argint(text):
    """argparse type helper."""
    try:
        return int(str(text), 0)
    except ValueError:
        raise argparse.ArgumentTypeError("%r is not a number (decimal or 0x-prefixed hex)" % text)


# ---------------------------------------------------------------------------
# variable references and files
# ---------------------------------------------------------------------------

class VarRef(object):
    """NAME + GUID. The efivarfs file name is <Name>-<guid, lower case>."""

    def __init__(self, name, guid):
        if (not name or "/" in name or "\\" in name or "\x00" in name or name in (".", "..")):
            raise VarError("bad variable name %r" % name)
        if not GUID_RE.match(guid):
            raise VarError("bad GUID %r" % guid)
        self.name = name
        self.guid = guid.lower()

    @classmethod
    def parse(cls, text):
        t = str(text).strip()
        if len(t) < 38 or t[-37] != "-":
            raise VarError("expected NAME-GUID, for example "
                           "Setup-ec87d643-eba4-4bb5-a1e5-3f3e36b20da9; got %r" % text)
        return cls(t[:-37], t[-36:])

    @property
    def filename(self):
        return "%s-%s" % (self.name, self.guid)

    def __str__(self):
        return self.filename

    def __eq__(self, other):
        return isinstance(other, VarRef) and other.filename == self.filename

    def __ne__(self, other):
        return not self.__eq__(other)

    def __hash__(self):
        return hash(self.filename)


def resolve_by_name(efivars, name):
    """Every <name>-<guid> file in the directory (the whole name must match)."""
    found = []
    try:
        names = sorted(os.listdir(efivars))
    except OSError as exc:
        raise VarError("cannot list %s: %s" % (efivars, exc))
    prefix = name + "-"
    for n in names:
        if n.startswith(prefix) and GUID_RE.match(n[len(prefix):]):
            found.append(VarRef(name, n[len(prefix):]))
    return found


def loose_ref(efivars, text):
    """NAME-GUID if it parses, else a unique NAME lookup in the directory.
    Returns a VarRef, or None when the name is absent. Raises on ambiguity."""
    try:
        return VarRef.parse(text)
    except VarError:
        pass
    found = resolve_by_name(efivars, text.strip())
    if not found:
        return None
    if len(found) > 1:
        raise VarError("%r matches %d variables (%s); give NAME-GUID" % (
            text, len(found), ", ".join(str(f) for f in found)))
    return found[0]


class Var(object):
    """One efivarfs file: 4 attribute bytes (little-endian) + data."""

    def __init__(self, ref, path, raw):
        if len(raw) < 4:
            raise VarError("%s: file is %d bytes, shorter than the 4-byte attribute prefix"
                           % (ref, len(raw)))
        self.ref = ref
        self.path = path
        self.raw = bytes(raw)
        self.attrs = int.from_bytes(self.raw[:4], "little")
        self.data = self.raw[4:]


def var_path(efivars, ref):
    return os.path.join(efivars, ref.filename)


def read_var(efivars, ref):
    path = var_path(efivars, ref)
    try:
        with open(path, "rb") as fh:
            raw = fh.read()
    except OSError as exc:
        raise VarError("cannot read %s: %s" % (path, exc))
    return Var(ref, path, raw)


def attrs_names(attrs):
    names = [n for bit, n in ATTR_BITS if attrs & bit]
    extra = attrs & ~KNOWN_BITS
    if extra:
        names.append("UNKNOWN_BITS(0x%x)" % extra)
    return "|".join(names) if names else "(none)"


def attr_problems(attrs, expect_attrs=None):
    """Reasons this tool must not write a variable with these attributes."""
    problems = []
    if attrs & AUTH_BITS:
        problems.append("authenticated variable (attribute bits 0x%x): signed writes are needed and "
                        "this tool does not make them. Stop." % (attrs & AUTH_BITS))
    if attrs & 0x08:
        problems.append("hardware-error-record variable: not a setup value. Stop.")
    if attrs & 0x40:
        problems.append("APPEND_WRITE attribute set: not a setup value. Stop.")
    if attrs & ~KNOWN_BITS:
        problems.append("attribute bits this tool does not know (0x%x). Stop." % (attrs & ~KNOWN_BITS))
    if not attrs & 0x04:
        problems.append("no RUNTIME_ACCESS bit: the OS should not be able to write this variable.")
    if not attrs & 0x01:
        problems.append("not NON_VOLATILE: a change would not survive a reboot.")
    if expect_attrs is not None and attrs != expect_attrs:
        problems.append("attributes are 0x%x, expected 0x%x" % (attrs, expect_attrs))
    return problems


def sum16(data):
    """Sum of every byte, truncated to 16 bits. No carry fold, no complement."""
    return sum(bytearray(data)) & 0xFFFF


def hexdump(data, base=0):
    lines = []
    for i in range(0, len(data), 16):
        chunk = bytearray(data[i:i + 16])
        left = " ".join("%02x" % b for b in chunk[:8])
        right = " ".join("%02x" % b for b in chunk[8:])
        asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
        lines.append("%08x  %-23s  %-23s  |%s|" % (base + i, left, right, asc))
    return "\n".join(lines)


def diff_offsets(a, b):
    """Offsets where two byte strings differ, comparing up to the shorter length."""
    n = min(len(a), len(b))
    return [i for i in range(n) if a[i] != b[i]]


def check_field_geometry(offset, size, value, length, allow_multibyte):
    """Reasons a field write must be refused before anything is computed."""
    problems = []
    if size not in (1, 2, 4, 8):
        problems.append("field width %d is not 1, 2, 4 or 8 bytes" % size)
    elif size > 1 and not allow_multibyte:
        problems.append(
            "writing %d bytes overwrites %d neighbouring byte(s) if the field is really narrower. "
            "Most hidden toggles are 1 byte. Read the width from the IFR (Flags & 0x3: 0=1, 1=2, "
            "2=4, 3=8 bytes), then pass --allow-multibyte (profile: allow_multibyte) to confirm."
            % (size, size - 1))
    if value < 0:
        problems.append("value %d is negative" % value)
    elif size in (1, 2, 4, 8) and value >= (1 << (8 * size)):
        problems.append("value 0x%x does not fit in %d byte(s)" % (value, size))
    if offset < 0 or offset + size > length:
        problems.append("offset 0x%x + width %d runs past the end of the variable (%d data bytes)"
                        % (offset, size, length))
    return problems


def apply_field(data, offset, size, value):
    out = bytearray(data)
    out[offset:offset + size] = value.to_bytes(size, "little")
    if len(out) != len(data):
        raise VarError("internal error: field write changed the blob length")
    return bytes(out)


# ---------------------------------------------------------------------------
# real efivarfs detection, immutable flag, DMI
# ---------------------------------------------------------------------------

def _proc_mounts_lines():
    """The lines of /proc/mounts, or None where that file does not exist (not Linux)."""
    try:
        with open("/proc/mounts", "r") as fh:
            return fh.read().splitlines()
    except OSError:
        return None


def _unescape_mount(text):
    """/proc/mounts writes a space, tab, newline and backslash as \\040, \\011, \\012, \\134."""
    return re.sub(r"\\([0-7]{3})", lambda m: chr(int(m.group(1), 8)), text)


def _mount_entry(path):
    """(fstype, options) of the mount whose mount point is exactly path, else None."""
    lines = _proc_mounts_lines()
    if lines is None:
        return None
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    hit = None
    for line in lines:
        parts = line.split()
        if len(parts) >= 4 and _unescape_mount(parts[1]) == real:
            hit = (parts[2], parts[3])
    return hit


VOLATILE_FSTYPES = ("tmpfs", "ramfs")
ALWAYS_VOLATILE_FSTYPES = ("aufs", "squashfs", "rootfs")      # a live system's root, or an initramfs


def _mount_rows(lines):
    """(mount point, fstype, options) for each line of /proc/mounts."""
    rows = []
    for line in lines:
        parts = line.split()
        if len(parts) < 3:
            continue
        rows.append((_unescape_mount(parts[1]), parts[2], _unescape_mount(parts[3]) if len(parts) > 3 else ""))
    return rows


def _mount_holding(path, rows):
    """The (mount point, fstype, options) row with the longest mount point that contains path."""
    best = None
    for row in rows:
        mnt = row[0]
        prefix = mnt.rstrip("/" + os.sep)
        inside = path == mnt or path.startswith((prefix + "/", prefix + os.sep))
        if inside and (best is None or len(mnt) >= len(best[0])):
            best = row
    return best


def _volatile_kind(row, rows, depth=0):
    """Why a mount loses its contents at reboot, or None. An overlay is judged by where its upper
    layer lives; when that cannot be worked out, it counts as volatile (a live USB's root is an
    overlay with a RAM upper layer, and a backup there is gone after the reboot it exists for)."""
    mnt, fstype, opts = row
    if fstype in VOLATILE_FSTYPES or fstype in ALWAYS_VOLATILE_FSTYPES:
        return fstype
    if fstype != "overlay":
        return None
    upper = None
    for opt in opts.split(","):
        if opt.startswith("upperdir="):
            upper = opt[len("upperdir="):]
    if not upper:
        return "overlay (no upper layer is listed, so it cannot be told apart from a RAM one)"
    holder = _mount_holding(upper, rows)
    if holder is None or holder == row or depth >= 4:
        return "overlay (its upper layer could not be traced to a disk)"
    inner = _volatile_kind(holder, rows, depth + 1)
    return "overlay with its upper layer on %s" % inner if inner else None


def volatile_backup_fs(path):
    """A word for the memory-backed or live-system filesystem path lives on (a backup there is
    gone after a reboot), else None. tmpfs and ramfs, aufs, squashfs and rootfs always count; an
    overlay counts when its upper layer is on one of those or cannot be traced. Uses the longest
    mount point that contains path, so the path need not exist yet. Returns None where
    /proc/mounts does not exist: the check is skipped there."""
    lines = _proc_mounts_lines()
    if lines is None:
        return None
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    rows = _mount_rows(lines)
    holder = _mount_holding(real, rows)
    return _volatile_kind(holder, rows) if holder else None


def is_real_efivarfs(path):
    """True only for a mounted efivarfs. A synthetic directory is never real, so
    chattr is never called on it."""
    entry = _mount_entry(path)
    return bool(entry and entry[0] == "efivarfs")


def efivarfs_is_readonly(path):
    entry = _mount_entry(path)
    return bool(entry and entry[0] == "efivarfs" and "ro" in entry[1].split(","))


def is_root():
    return hasattr(os, "geteuid") and os.geteuid() == 0


def read_dmi(dmi_dir, key):
    try:
        with open(os.path.join(dmi_dir, key), "r") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _chattr(flag, path):
    exe = shutil.which("chattr")
    if not exe:
        return False
    try:
        res = subprocess.run([exe, flag, path], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    except OSError:
        return False
    return res.returncode == 0


def _lsattr_immutable(path):
    exe = shutil.which("lsattr")
    if not exe:
        return None
    try:
        res = subprocess.run([exe, "-d", path], stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             universal_newlines=True)
    except OSError:
        return None
    if res.returncode != 0 or not res.stdout.split():
        return None
    return "i" in res.stdout.split()[0]


_UNLOCK_DEPTH = {}
_WAS_IMMUTABLE = {}


class Unlocked(object):
    """Clear the immutable flag on real efivarfs files for the duration of a block and
    set it again afterwards. Re-entrant. Does nothing when real is False."""

    def __init__(self, paths, real):
        self.paths = list(paths)
        self.real = real

    def __enter__(self):
        if not self.real:
            return self
        for p in self.paths:
            depth = _UNLOCK_DEPTH.get(p, 0)
            if depth == 0:
                imm = _lsattr_immutable(p)
                _WAS_IMMUTABLE[p] = True if imm is None else imm
                if _WAS_IMMUTABLE[p] and not _chattr("-i", p):
                    say("WARNING: chattr -i failed on %s; the write will probably fail with EPERM." % p)
            _UNLOCK_DEPTH[p] = depth + 1
        return self

    def __exit__(self, *exc):
        if not self.real:
            return False
        for p in self.paths:
            _UNLOCK_DEPTH[p] = _UNLOCK_DEPTH.get(p, 1) - 1
            if _UNLOCK_DEPTH[p] <= 0:
                _UNLOCK_DEPTH.pop(p, None)
                if _WAS_IMMUTABLE.pop(p, False) and not _chattr("+i", p):
                    say("WARNING: could not set the immutable flag again on %s; run: chattr +i %s"
                        % (p, p))
        return False


def raw_write(path, blob):
    """ONE os.write of the whole blob (attribute prefix + data), return value checked.
    No O_CREAT: this can never create a variable. Binary mode matters on non-Linux
    hosts used for the synthetic self-test."""
    flags = os.O_WRONLY | os.O_TRUNC | getattr(os, "O_BINARY", 0)
    fd = os.open(path, flags)
    try:
        n = os.write(fd, blob)
    finally:
        os.close(fd)
    if n != len(blob):
        raise ShortWrite("short write on %s: %d of %d bytes" % (path, n, len(blob)))


def describe_failure(exc):
    if isinstance(exc, ShortWrite):
        return "short write: %s" % exc
    if not isinstance(exc, OSError):
        return ("%s: the operation was interrupted or failed unexpectedly; treat its outcome as "
                "unknown" % type(exc).__name__)
    code = getattr(exc, "errno", None)
    name = errno.errorcode.get(code, "errno %s" % code)
    text = ERRNO_TEXT.get(code, "unexpected error; treat the write as failed")
    return "%s (errno %s): %s" % (name, code, text)


RECOVERY_LINES = (
    "Recovery ladder, cheapest first:",
    "  1. Remove any card you added, boot the OS, run restore.py on your backup directory.",
    "  2. Firmware menu: Load Setup Defaults (then re-apply any setting you had customised).",
    "  3. CMOS clear. It does not reset the variable store on every board; see docs/05-safety-and-recovery.md.",
)


def is_checksum_name(name):
    """A variable that holds a stored checksum (any name containing CheckSum, any case)."""
    return "checksum" in name.lower()


def is_setup_name(name):
    """A Setup-family variable that a stored checksum covers (any name containing Setup, any
    case, that is not itself a checksum)."""
    low = name.lower()
    return "setup" in low and "checksum" not in low


def is_checksum_guarded(name):
    """True for a Setup-family variable or a checksum variable. Writing one of them alone can
    leave the pair disagreeing. The match is a case-insensitive substring on purpose: matching
    too much only adds a flag requirement, matching too little leaves a way to the 0162 state."""
    low = name.lower()
    return "setup" in low or "checksum" in low


def companion_problem(refs):
    """Why restoring exactly these variables could split Setup from its checksum, or None.
    refs: the restorable variables of a backup."""
    setups = [r for r in refs if is_setup_name(r.name)]
    sums = [r for r in refs if is_checksum_name(r.name)]
    if setups and not sums:
        return ("%s is restorable here without a checksum variable; restoring it alone can leave Setup and "
                "its stored checksum disagreeing, and the next boot would raise Error 0162 and reload BIOS "
                "defaults"
                % ", ".join(str(r) for r in setups))
    if sums and not setups:
        return ("%s is restorable here without a Setup variable; restoring it alone can leave Setup and "
                "its stored checksum disagreeing, and the next boot would raise Error 0162 and reload BIOS "
                "defaults"
                % ", ".join(str(r) for r in sums))
    return None


def restore_command(backup_dir, efivars, dmi_dir, allow_length_change=False, no_checksum_companion=False):
    """The exact command that restores a backup: absolute path of restore.py, absolute backup
    path, and the --efivars and --dmi values this run used."""
    parts = ["python3", os.path.join(HERE, "restore.py"), os.path.abspath(backup_dir),
             "--efivars", os.path.abspath(efivars), "--dmi", os.path.abspath(dmi_dir),
             "--apply", "--i-understand-the-risk"]
    if allow_length_change:
        parts.append("--allow-length-change")
    if no_checksum_companion:
        parts.append("--no-checksum-companion")
    return " ".join(shlex.quote(p) for p in parts)


def stop_guidance(out, backup_dir, efivars, dmi_dir, pair_may_disagree, allow_length_change=False,
                  recovered=False, bset=None):
    """The lines every STOP path ends with: the restore command, and, when Setup and its
    checksum may not agree, the order not to reboot. recovered: the automatic restore already
    succeeded, so the command is shown only for the record and the recovery ladder is left out.
    bset: the backup, so the command carries --no-checksum-companion when the backup needs it."""
    needs_companion_flag = bool(bset is not None and companion_problem([e.ref for e in bset.entries if e.restore]))
    cmd = restore_command(backup_dir, efivars, dmi_dir, allow_length_change, needs_companion_flag)
    if pair_may_disagree:
        out("DO NOT REBOOT. Setup and LastCheckSum may not agree, and the next boot would raise "
            "Error 0162 and reload BIOS defaults. Run the command below. Do NOT reboot until this succeeds:")
    elif recovered:
        out("The restore command, in case you need to run it again:")
    else:
        out("To put the variables back from the backup, run:")
    out("  " + cmd)
    if allow_length_change:
        out("(--allow-length-change is there because a variable no longer has the length the backup "
            "recorded; the BIOS and platform checks still apply.)")
    if needs_companion_flag:
        out("(--no-checksum-companion is there because this backup holds Setup and its checksum variable "
            "separately, so restore.py would otherwise refuse it.)")
    if not recovered:
        for line in RECOVERY_LINES:
            out(line)


def live_state(efivars, bset, ref):
    """How a variable compares with its backup right now, as words."""
    e = bset.entry(ref)
    try:
        live = read_var(efivars, ref)
    except VarError:
        return "cannot be read"
    if e is not None and live.raw == e.raw:
        return "equals the backup"
    return "differs from the backup"


def lengths_differ(efivars, bset, refs):
    """True when a readable live variable no longer has the data length its backup recorded,
    which is the case a restore needs --allow-length-change for."""
    for ref in refs:
        e = bset.entry(ref)
        if e is None:
            continue
        try:
            live = read_var(efivars, ref)
        except VarError:
            continue
        if len(live.data) != len(e.data):
            return True
    return False


# ---------------------------------------------------------------------------
# backups
# ---------------------------------------------------------------------------

def _write_new_file(path, data):
    """Create a new file (never overwrites), write it whole and fsync it. A failed fsync is a
    refusal: a backup that may not be on disk is not a backup."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
    try:
        n = os.write(fd, data)
        if n != len(data):
            raise VarError("short write on %s" % path)
        try:
            os.fsync(fd)
        except OSError as exc:
            raise VarError("fsync failed on %s: %s; the backup may not be on disk" % (path, exc))
    finally:
        os.close(fd)


def _fsync_dir(path):
    """fsync a directory so the new file names in it are durable. A failure is a refusal. Skipped
    on Windows, where a directory cannot be opened this way; the tools target Linux."""
    if os.name == "nt":
        return
    try:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError as exc:
        raise VarError("fsync failed on directory %s: %s; the backup may not be on disk" % (path, exc))


def write_file_atomic(path, data):
    """Replace path with data so a reader, or a crash, sees the old file or the new one and never
    half of either: write <path>.tmp, fsync it, rename it over path, fsync the directory."""
    tmp = path + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        try:
            view = memoryview(data)
            while len(view):
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    _fsync_dir(os.path.dirname(path) or ".")


def probe_writable(paths):
    """Open each path write-only (no O_TRUNC, so nothing is changed) and close it again. Returns
    [(path, exception)] for the ones that cannot be opened. A write window starts only after this
    comes back empty, so a variable that refuses writing is found before Setup is touched."""
    bad = []
    for p in paths:
        try:
            fd = os.open(p, os.O_WRONLY | getattr(os, "O_BINARY", 0))
        except OSError as exc:
            bad.append((p, exc))
            continue
        try:
            os.close(fd)
        except OSError:
            pass
    return bad


MANIFEST_NAME = "manifest.json"
HASHFILE_NAME = "MANIFEST.sha256"
PLATFORM_KEYS = ("board_name", "product_name", "sys_vendor")
HASH_LINE_RE = re.compile(r"^([0-9a-f]{64}) [ *](\S.*)$")


def _read_hash_file(path):
    """MANIFEST.sha256 as {file name: hex digest}. A missing file or a malformed line is a refusal."""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            text = fh.read()
    except (OSError, ValueError) as exc:
        raise VarError("cannot read %s: %s" % (path, exc))
    hashes = {}
    for num, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        m = HASH_LINE_RE.match(line)
        if not m:
            raise VarError("%s line %d is not '<sha256>  <file name>'" % (path, num))
        name = m.group(2)
        if "/" in name or "\\" in name or name in (".", ".."):
            raise VarError("%s line %d: %r is not a plain file name" % (path, num, name))
        if name in hashes:
            raise VarError("%s lists %s twice" % (path, name))
        hashes[name] = m.group(1)
    return hashes


def _int_field(item, key, label):
    val = item.get(key)
    if isinstance(val, bool) or not isinstance(val, int):
        raise VarError("%s: manifest field %r must be a whole number" % (label, key))
    return val


class BackupEntry(object):
    def __init__(self, ref, raw, restore, fname):
        self.ref = ref
        self.raw = raw
        self.attrs = int.from_bytes(raw[:4], "little")
        self.data = raw[4:]
        self.restore = restore
        self.file = fname


class BackupSet(object):
    """A backup directory made by make_backup(): <Name>-<guid>.bin files
    (efivarfs format, attribute prefix included), manifest.json and MANIFEST.sha256."""

    def __init__(self, path, manifest, entries, platform=None):
        self.path = path
        self.manifest = manifest
        self.entries = entries
        self.bios_version = manifest.get("bios_version")
        self.platform = platform or {}

    @classmethod
    def load(cls, path):
        """Load and verify a backup. Every hash in MANIFEST.sha256 (the manifest included) is
        checked, every field is type-checked, and anything unusable is a VarError (a refusal)."""
        try:
            return cls._load(path)
        except VarError:
            raise
        except (KeyError, TypeError, ValueError, AttributeError, OverflowError) as exc:
            raise VarError("the backup manifest in %s is malformed (%s: %s)"
                           % (path, type(exc).__name__, exc))

    @classmethod
    def _load(cls, path):
        mpath = os.path.join(path, MANIFEST_NAME)
        hashes = _read_hash_file(os.path.join(path, HASHFILE_NAME))
        try:
            with open(mpath, "rb") as fh:
                mraw = fh.read()
        except OSError as exc:
            raise VarError("cannot read %s: %s" % (mpath, exc))
        if MANIFEST_NAME not in hashes:
            raise VarError("%s does not list %s, so the manifest cannot be trusted"
                           % (HASHFILE_NAME, MANIFEST_NAME))
        if hashlib.sha256(mraw).hexdigest() != hashes[MANIFEST_NAME]:
            raise VarError("%s: sha256 differs from %s; the manifest was changed after the backup was made"
                           % (MANIFEST_NAME, HASHFILE_NAME))
        try:
            manifest = json.loads(mraw.decode("utf-8"))
        except ValueError as exc:
            raise VarError("cannot read %s: %s" % (mpath, exc))
        if (not isinstance(manifest, dict) or manifest.get("format") != 1
                or not isinstance(manifest.get("variables"), list)):
            raise VarError("%s is not a manifest this tool wrote (format 1)" % mpath)
        platform = {}
        for key in ("bios_version",) + PLATFORM_KEYS:
            val = manifest.get(key)
            if val is not None and not isinstance(val, str):
                raise VarError("%s: field %r must be text or null" % (mpath, key))
            if key != "bios_version" and val is not None:
                platform[key] = val
        entries, seen = [], set()
        for item in manifest["variables"]:
            e = cls._load_entry(path, item, hashes)
            if e.ref in seen:
                raise VarError("%s lists %s twice" % (mpath, e.ref))
            seen.add(e.ref)
            entries.append(e)
        if not entries:
            raise VarError("%s lists no variables" % mpath)
        return cls(path, manifest, entries, platform)

    @staticmethod
    def _load_entry(path, item, hashes):
        if not isinstance(item, dict):
            raise VarError("a manifest entry is not an object: %r" % (item,))
        name, guid = item.get("name"), item.get("guid")
        if not (isinstance(name, str) and isinstance(guid, str)):
            raise VarError("a manifest entry has no usable name and guid: %r" % (item,))
        ref = VarRef(name, guid)
        fname = item.get("file")
        if fname != ref.filename + ".bin":
            raise VarError("%s: the manifest points this variable at file %r, not %s.bin; a swapped "
                           "mapping would restore another variable's blob into it" % (ref, fname, ref.filename))
        restore = item.get("restore")
        if not isinstance(restore, bool):
            raise VarError("%s: manifest field 'restore' must be true or false" % ref)
        want_len = _int_field(item, "data_length", ref)
        want_attrs = _int_field(item, "attrs", ref)
        digest = item.get("sha256")
        if fname not in hashes:
            raise VarError("%s does not list %s" % (HASHFILE_NAME, fname))
        fpath = os.path.join(path, fname)
        try:
            with open(fpath, "rb") as fh:
                raw = fh.read()
        except OSError as exc:
            raise VarError("%s: cannot read %s: %s" % (ref, fpath, exc))
        have = hashlib.sha256(raw).hexdigest()
        if have != hashes[fname]:
            raise VarError("%s: sha256 differs from %s; the backup file was changed" % (ref, HASHFILE_NAME))
        if have != digest:
            raise VarError("%s: sha256 differs from the manifest; the backup file was changed" % ref)
        if len(raw) < 4 or len(raw) - 4 != want_len:
            raise VarError("%s: length differs from the manifest" % ref)
        if int.from_bytes(raw[:4], "little") != want_attrs:
            raise VarError("%s: attributes differ from the manifest" % ref)
        return BackupEntry(ref, raw, restore is True, fname)

    def entry(self, ref):
        for e in self.entries:
            if e.ref == ref:
                return e
        return None

    def platform_problems(self, dmi_dir):
        """Why this backup cannot be tied to the machine in dmi_dir, as a list. The BIOS version
        must be recorded, readable now, and equal. Board name, product name and system vendor
        are compared when the backup recorded them."""
        problems = []
        live_bios = read_dmi(dmi_dir, "bios_version")
        if not self.bios_version:
            problems.append("the backup does not record a BIOS version, so it cannot be tied to this "
                            "machine; make a new backup with a readable DMI (%s)" % dmi_dir)
        elif not live_bios:
            problems.append("cannot read the BIOS version from %s, so the backup (BIOS %r) cannot be "
                            "tied to this machine" % (dmi_dir, self.bios_version))
        elif self.bios_version != live_bios:
            problems.append("the backup was made on BIOS %r but this machine reports %r; "
                            "never restore a Setup blob from a different build"
                            % (self.bios_version, live_bios))
        if not (self.platform.get("board_name") or self.platform.get("product_name")):
            problems.append("the backup records neither a board name nor a product name, so only the BIOS "
                            "string ties it to this machine, and many machines share one; make a new backup "
                            "with a readable DMI (%s)" % dmi_dir)
        for key in PLATFORM_KEYS:
            rec = self.platform.get(key)
            if rec is None:
                continue
            live = read_dmi(dmi_dir, key)
            if live != rec:
                problems.append("the backup was made on %s %r but this machine reports %r; "
                                "never restore a Setup blob onto a different machine" % (key, rec, live))
        return problems

    def problems_vs_live(self, efivars, ref):
        """Why this backup does not prove a safe restore point for ref, as a list."""
        e = self.entry(ref)
        if e is None:
            return ["the backup in %s does not contain %s" % (self.path, ref)]
        if not e.restore:
            return ["the backup in %s holds %s for reference only, so a restore would not put it back; "
                    "make a backup that lists it as restorable" % (self.path, ref)]
        try:
            live = read_var(efivars, ref)
        except VarError as exc:
            return [str(exc)]
        if live.raw != e.raw:
            return ["the live variable %s differs from the backup in %s (it changed after the backup "
                    "was made); make a fresh backup" % (ref, self.path)]
        return []


def make_backup(efivars, refs, outdir, reference_refs=(), dmi_dir=DEFAULT_DMI, out=say,
                allow_volatile=False):
    """Copy raw variables into <outdir>/efivar-backup-<timestamp>/, re-read the files and compare
    them with the live variables, return the loaded BackupSet. The re-read is served by the page
    cache, so it checks the copy, not the disk; the fsyncs are what make the files durable.
    refs are restorable, in the order restore will write them. reference_refs are saved
    for the record and never restored.
    Refused: a directory that is gone after a reboot (tmpfs, ramfs, a live system's root) unless
    allow_volatile is set, and a machine whose DMI shows neither a board name nor a product name,
    because the BIOS string alone does not say which machine a backup belongs to. Every file is
    fsynced, then every directory this call created and the existing one that gained the first."""
    kind = volatile_backup_fs(outdir)
    if kind and not allow_volatile:
        raise VarError("%s is on %s, which is gone after a reboot, and the point of a backup is to "
                       "survive one (live system: put --backup-dir on a USB stick or a disk partition; "
                       "uefivar.py backup calls the option --dir). Or pass --allow-volatile-backup "
                       "if you accept that." % (outdir, kind))
    platform = {}
    for key in PLATFORM_KEYS:             # recorded when readable; a restore compares them
        val = read_dmi(dmi_dir, key)
        if val:
            platform[key] = val
    if not (platform.get("board_name") or platform.get("product_name")):
        raise VarError("neither board_name nor product_name can be read from %s. A backup tied to this "
                       "machine by the BIOS string alone could be restored onto another machine that "
                       "shares it; pass --dmi with a readable directory." % dmi_dir)
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S")
    base = os.path.join(outdir, "efivar-backup-" + stamp)
    bdir, n = base, 1
    while os.path.exists(bdir):
        n += 1
        bdir = "%s-%d" % (base, n)
    created, anchor = [], os.path.abspath(bdir)       # directories makedirs will make, deepest first
    while not os.path.exists(anchor):
        created.append(anchor)
        parent = os.path.dirname(anchor)
        if parent == anchor:
            break
        anchor = parent
    os.makedirs(bdir, 0o700)
    items, seen, lines = [], set(), []
    plan = [(r, True) for r in refs] + [(r, False) for r in reference_refs]
    for ref, restore in plan:
        if ref.filename in seen:
            continue
        seen.add(ref.filename)
        var = read_var(efivars, ref)
        fname = ref.filename + ".bin"
        _write_new_file(os.path.join(bdir, fname), var.raw)
        digest = hashlib.sha256(var.raw).hexdigest()
        lines.append("%s  %s" % (digest, fname))
        items.append({"name": ref.name, "guid": ref.guid, "file": fname,
                      "attrs": var.attrs, "data_length": len(var.data),
                      "sha256": digest, "restore": restore})
    manifest = {
        "format": 1,
        "tool": "uefivar.py",
        "created_utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bios_version": read_dmi(dmi_dir, "bios_version"),
    }
    manifest.update(platform)
    manifest["variables"] = items
    mbytes = (json.dumps(manifest, indent=2) + "\n").encode("utf-8")
    _write_new_file(os.path.join(bdir, MANIFEST_NAME), mbytes)
    lines.append("%s  %s" % (hashlib.sha256(mbytes).hexdigest(), MANIFEST_NAME))
    _write_new_file(os.path.join(bdir, HASHFILE_NAME), ("\n".join(lines) + "\n").encode("utf-8"))
    for d in created:                     # the new names in each new directory ...
        _fsync_dir(d)
    _fsync_dir(anchor)                    # ... and the entry of the topmost one in the directory that existed
    bset = BackupSet.load(bdir)          # re-reads every file and checks hashes (from the page cache)
    for e in bset.entries:               # and compare with what is live right now
        live = read_var(efivars, e.ref)
        if live.raw != e.raw:
            raise VarError("%s changed while it was being backed up; nothing was written, try again"
                           % e.ref)
    return bset


def mark_report_resolved(backup_dir):
    """A report.json that still says "writing" is a mark that a write window was open when a run
    ended. A restore that put the whole backup back resolves it. Best effort: returns True when
    the status was changed, never raises."""
    rp = os.path.join(backup_dir, "report.json")
    try:
        with open(rp, "r", encoding="utf-8") as fh:
            report = json.load(fh)
        if not isinstance(report, dict) or report.get("status") != "writing":
            return False
        report["status"] = "restored-by-restore.py"
        write_file_atomic(rp, (json.dumps(report, indent=2) + "\n").encode("utf-8"))
        return True
    except (OSError, ValueError, VarError):
        return False


def restore_backup(efivars, backup_dir, apply=False, risk_ok=False, dmi_dir=DEFAULT_DMI, out=say,
                   only=None, allow_length_change=False, guidance=True, no_checksum_companion=False,
                   preloaded=None):
    """Restore every restorable variable of a backup, in manifest order, one whole-blob
    write each, back to back (Setup, then its checksum, then the rest when the backup was
    listed in that order). Verifies by reading each variable back. Dry run unless apply.
    only: a list of VarRef; restore just those. allow_length_change: skip the refusal for a live
    variable whose data length differs from the backup's (for repairing damage a failed write of
    this tool left behind; the BIOS and platform checks still apply). guidance: print the restore
    command and the recovery ladder on a STOP; a caller that prints its own says False.
    no_checksum_companion: allow a whole-backup restore of Setup without its checksum variable (or
    the reverse); without it that is refused, because it can leave the pair disagreeing. The check
    does not apply to an `only` restore, which is what the automatic rollback uses.
    preloaded: a BackupSet this tool already loaded and checked, for the automatic rollback; it is
    used as it is, with no second read of the backup and no second DMI check.
    Every target is opened write-only first, so a variable that refuses writing is found before
    anything is written. Nothing is printed between two writes, and the write window ignores
    SIGINT, SIGTERM, SIGHUP and SIGQUIT. Returns an exit code."""
    if preloaded is not None:
        bset = preloaded
    else:
        try:
            bset = BackupSet.load(backup_dir)
        except VarError as exc:
            out("REFUSE: backup directory is not usable: %s" % exc)
            return EXIT_REFUSED
    real = is_real_efivarfs(efivars)
    refusals, todo = [], []
    if preloaded is None:
        refusals += bset.platform_problems(dmi_dir)
    if only is None and not no_checksum_companion:
        problem = companion_problem([e.ref for e in bset.entries if e.restore])
        if problem:
            refusals.append("%s. Pass --no-checksum-companion only for a board with no checksum guard" % problem)
    if only is not None:
        for ref in only:
            e = bset.entry(ref)
            if e is None or not e.restore:
                refusals.append("the backup does not hold %s as a restorable variable" % ref)
    out("Backup   : %s" % bset.path)
    out("Variables (restore order):")
    for e in bset.entries:
        if only is not None and e.ref not in only:
            continue
        if not e.restore:
            out("  - %-60s saved for reference only, not restored" % e.ref)
            continue
        try:
            live = read_var(efivars, e.ref)
        except VarError as exc:
            refusals.append(str(exc))
            continue
        if live.raw[:4] != e.raw[:4]:
            refusals.append("%s: live attributes 0x%08x differ from the backup's 0x%08x; "
                            "attributes cannot be changed by overwriting. Stop."
                            % (e.ref, live.attrs, e.attrs))
        elif live.attrs & AUTH_BITS:
            refusals.append("%s: authenticated variable (attribute bits 0x%x); this tool does not write "
                            "those. Take it out of the restorable list." % (e.ref, live.attrs & AUTH_BITS))
        elif len(live.data) != len(e.data) and not allow_length_change:
            refusals.append("%s: the live variable holds %d data bytes, the backup %d; a different length "
                            "means a different layout, so this is not the build the backup came from. "
                            "Stop." % (e.ref, len(live.data), len(e.data)))
        elif live.raw == e.raw:
            out("  = %-60s already identical to the backup" % e.ref)
        else:
            n = len(diff_offsets(live.data, e.data))
            out("  > %-60s will be restored (%d byte(s) differ, %d data bytes%s)"
                % (e.ref, n, len(e.data),
                   "" if len(live.data) == len(e.data) else ", live holds %d" % len(live.data)))
            todo.append((e, live))
    if apply and not risk_ok:
        refusals.append("--i-understand-the-risk is required for a real write")
    if apply and real and not is_root():
        refusals.append("not running as root")
    if apply and real and efivarfs_is_readonly(efivars):
        refusals.append("efivarfs is mounted read-only; remount it read-write first")
    if refusals:
        for r in refusals:
            out("REFUSE: %s" % r)
        return EXIT_REFUSED
    if not todo:
        out("Nothing to restore: every restorable variable already equals the backup.")
        if apply and only is None:
            mark_report_resolved(bset.path)
        return EXIT_OK
    if not apply:
        out("DRY RUN: nothing written. Add --apply --i-understand-the-risk to restore.")
        return EXIT_OK

    written, inflight, failure, bad, unwritable = [], None, None, [], []
    with IgnoreSignals():
        with Unlocked([live.path for _, live in todo], real):
            unwritable = probe_writable([live.path for _, live in todo])
            if not unwritable:
                try:
                    for e, live in todo:           # nothing but the write between two writes
                        inflight = e
                        raw_write(live.path, e.raw)
                        written.append(e)
                        inflight = None
                    for e, live in todo:           # read-back, reads only
                        try:
                            now = read_var(efivars, e.ref)
                        except VarError as exc:
                            bad.append((e.ref, str(exc)))
                            continue
                        if now.raw != e.raw:
                            bad.append((e.ref, "read-back differs from the backup"))
                except BaseException as exc:       # includes KeyboardInterrupt and a broken pipe
                    failure = (inflight, exc)

    # Everything below only prints: the writes are over.
    if unwritable:
        for p, exc in unwritable:
            out("REFUSE: cannot open %s for writing: %s" % (p, describe_failure(exc)))
        out("Nothing was written.")
        return EXIT_REFUSED
    if failure is not None:
        failed_entry, exc = failure
        out("STOP: restoring %s failed: %s" % (failed_entry.ref if failed_entry else "(read-back)",
                                               describe_failure(exc)))
        out("Writes, in order:")
        for e, _ in todo:
            if e in written:
                what = "restored"
            elif e is failed_entry:
                what = "write failed or was interrupted; its state is unknown"
            else:
                what = "not attempted"
            out("  %-60s %s" % (e.ref, what))
        pair_risk = any(is_checksum_guarded(e.ref.name) and e not in written for e, _ in todo)
        if guidance:
            stop_guidance(out, bset.path, efivars, dmi_dir, pair_risk,
                          allow_length_change or lengths_differ(efivars, bset, [e.ref for e, _ in todo]),
                          bset=bset)
        return EXIT_STOP
    if bad:
        out("STOP: read-back verification failed after the restore:")
        for ref, text in bad:
            out("  %s: %s" % (ref, text))
        pair_risk = any(is_checksum_guarded(ref.name) for ref, _ in bad)
        if guidance:
            stop_guidance(out, bset.path, efivars, dmi_dir, pair_risk,
                          allow_length_change or lengths_differ(efivars, bset, [e.ref for e, _ in todo]),
                          bset=bset)
        return EXIT_STOP
    for e in written:
        out("wrote %s" % e.ref)
    out("RESTORED and verified by read-back: %s" % ", ".join(str(e.ref) for e, _ in todo))
    if only is None:
        mark_report_resolved(bset.path)
    return EXIT_OK


# ---------------------------------------------------------------------------
# subcommands
# ---------------------------------------------------------------------------

def cmd_read(a):
    ref = VarRef.parse(a.var)
    var = read_var(a.efivars, ref)
    data = var.data
    off = a.offset or 0
    end = len(data) if a.length is None else off + a.length
    if off < 0 or off > len(data) or end > len(data):
        raise VarError("offset/length run past the end of the variable (%d data bytes)" % len(data))
    sl = data[off:end]
    if a.hex:
        say(sl.hex())
        return EXIT_OK
    say("variable : %s" % ref.filename)
    say("file     : %s" % var.path)
    say("attrs    : 0x%08x  %s" % (var.attrs, attrs_names(var.attrs)))
    say("length   : %d (0x%x) data bytes; file is %d bytes with the attribute prefix"
        % (len(data), len(data), len(var.raw)))
    say("sum16    : 0x%04x  (sum of all data bytes, 16 bits)" % sum16(data))
    if len(data) <= 8:
        say("hex      : %s" % data.hex())
    say("data%s:" % ("" if (off == 0 and end == len(data)) else " [0x%x:0x%x]" % (off, end)))
    say(hexdump(sl, off))
    return EXIT_OK


def cmd_list(a):
    try:
        names = sorted(os.listdir(a.efivars))
    except OSError as exc:
        raise VarError("cannot list %s: %s" % (a.efivars, exc))
    pat = re.compile(a.grep, re.I) if a.grep else None
    shown = 0
    for n in names:
        if pat and not pat.search(n):
            continue
        p = os.path.join(a.efivars, n)
        try:
            with open(p, "rb") as fh:
                head = fh.read(4)
            size = os.path.getsize(p) - 4
        except OSError as exc:
            say("%-80s unreadable: %s" % (n, exc))
            continue
        attrs = int.from_bytes(head, "little") if len(head) == 4 else 0
        say("%-80s attrs=0x%02x data=%d" % (n, attrs, size))
        shown += 1
    say("%d variable(s) shown" % shown)
    return EXIT_OK


def cmd_backup(a):
    refs = [VarRef.parse(v) for v in a.vars]
    refonly = [VarRef.parse(v) for v in (a.reference_only or [])]
    try:
        bset = make_backup(a.efivars, refs, a.dir, refonly, a.dmi, allow_volatile=a.allow_volatile_backup)
    except OSError as exc:
        raise VarError("backup failed: %s" % exc)
    say("Backup written and fsynced; re-read and compared with the live variables (the re-read may be "
        "served from the page cache, so it checks the copy, not the disk): %s" % bset.path)
    if not bset.bios_version:
        say("warning: no BIOS version could be read from %s. restore.py refuses a backup that does not "
            "record one, so this backup can be inspected but not restored. Pass --dmi with a readable "
            "directory and make it again." % a.dmi)
    pair_problem = companion_problem([e.ref for e in bset.entries if e.restore])
    if pair_problem:
        say("warning: %s. restore.py will refuse this backup unless you pass --no-checksum-companion; list the "
            "Setup variable and its checksum variable together unless this board has no checksum guard."
            % pair_problem)
    for e in bset.entries:
        say("  %-62s attrs=0x%08x data=%-5d %s" % (
            e.ref, e.attrs, len(e.data), "restorable" if e.restore else "reference only"))
    say("Restore order is the order listed above. List the Setup variable first, then its "
        "checksum variable, then the rest.")
    say("Keep this directory private: the Setup blob can hold system data. This repo has not "
        "inspected one for password hashes or asset tags. Do not post it.")
    return EXIT_OK


def cmd_checksum(a):
    sref, cref = VarRef.parse(a.setup), VarRef.parse(a.stored)
    s = read_var(a.efivars, sref)
    c = read_var(a.efivars, cref)
    cur = sum16(s.data)
    say("Setup variable  : %s  attrs 0x%08x  %d (0x%x) data bytes"
        % (sref, s.attrs, len(s.data), len(s.data)))
    say("sum16(Setup)    : 0x%04x" % cur)
    say("Stored variable : %s  attrs 0x%08x  %d data byte(s): %s"
        % (cref, c.attrs, len(c.data), c.data.hex()))
    if len(c.data) != 2:
        say("REFUSE: this helper models a 16-bit little-endian stored checksum; this variable holds "
            "%d data bytes. Nothing is assumed about it." % len(c.data))
        return EXIT_REFUSED
    stored = int.from_bytes(c.data, "little")
    delta = (stored - cur) & 0xFFFF
    signed = delta - 0x10000 if delta >= 0x8000 else delta
    say("stored (LE)     : 0x%04x" % stored)
    say("DELTA           : 0x%04x  (%+d)  = stored - sum16(Setup), modulo 2^16" % (delta, signed))
    say("")
    say("This is the relationship on THIS machine, right now. It is not a constant: on the reference "
        "build it measured +1 on one date and 0 on a later date, with clean boots both times. "
        "Measure it again immediately before any write, and never copy a delta from another box.")
    return EXIT_OK


def _resolve_dump(path, efivars):
    if os.path.isfile(path):
        return path
    cand = os.path.join(efivars, path)
    if os.path.isfile(cand):
        return cand
    raise VarError("no such file: %s (also looked in %s)" % (path, efivars))


def cmd_diff(a):
    pa, pb = _resolve_dump(a.file_a, a.efivars), _resolve_dump(a.file_b, a.efivars)
    with open(pa, "rb") as fh:
        ra = fh.read()
    with open(pb, "rb") as fh:
        rb = fh.read()
    differs = False
    if a.raw:
        da, db = ra, rb
    else:
        if len(ra) < 4 or len(rb) < 4:
            raise VarError("a dump is shorter than the 4-byte attribute prefix; use --raw for plain data")
        aa, ab = int.from_bytes(ra[:4], "little"), int.from_bytes(rb[:4], "little")
        say("attrs A: 0x%08x  %s" % (aa, attrs_names(aa)))
        say("attrs B: 0x%08x  %s" % (ab, attrs_names(ab)))
        if aa != ab:
            say("ATTRIBUTES DIFFER")
            differs = True
        da, db = ra[4:], rb[4:]
    say("length A: %d   length B: %d" % (len(da), len(db)))
    if len(da) != len(db):
        say("LENGTHS DIFFER (comparing the first %d bytes)" % min(len(da), len(db)))
        differs = True
    offs = diff_offsets(da, db)
    for o in offs[:a.max_lines]:
        say("  0x%04x: %02x -> %02x" % (o, da[o], db[o]))
    if len(offs) > a.max_lines:
        say("  ... %d more not shown (--max-lines)" % (len(offs) - a.max_lines))
    say("%d byte(s) differ" % len(offs))
    if offs:
        differs = True
    return EXIT_DIFF if differs else EXIT_OK


def cmd_write(a):
    ref = VarRef.parse(a.var)
    var = read_var(a.efivars, ref)
    real = is_real_efivarfs(a.efivars)
    refusals = []
    say("Variable   : %s" % ref.filename)
    say("Attributes : 0x%08x  %s" % (var.attrs, attrs_names(var.attrs)))
    say("Data length: %d (0x%x) bytes; the write is ONE whole-blob write of %d bytes "
        "(4 attribute bytes + data)" % (len(var.data), len(var.data), len(var.raw)))
    refusals += attr_problems(var.attrs, a.expect_attrs)
    if is_checksum_guarded(ref.name) and not a.no_checksum_companion:
        refusals.append("writing %s on its own is refused: it is Setup or a checksum variable, and on a "
                        "board that checksums Setup a write to one without the other makes the next "
                        "boot raise Error 0162 and reload BIOS defaults. This subcommand writes no "
                        "checksum. Use above4g_unlock.py, which writes the checksum immediately after. "
                        "Pass --no-checksum-companion only for a board with no checksum guard."
                        % ref.name)
    missing_expect = [flag for flag, val in (("--expect-old", a.expect_old),
                                             ("--expect-var-len", a.expect_var_len),
                                             ("--expect-attrs", a.expect_attrs)) if val is None]
    if a.apply:
        for flag in missing_expect:
            refusals.append("%s is required with --apply: state what you expect to find, from the "
                            "dry run or your IFR" % flag)
    if a.expect_var_len is not None and a.expect_var_len != len(var.data):
        refusals.append("variable holds %d data bytes, you stated %d; wrong variable or wrong BIOS build"
                        % (len(var.data), a.expect_var_len))
    geo = check_field_geometry(a.offset, a.size, a.value, len(var.data), a.allow_multibyte)
    refusals += geo
    noop, new_data = False, None
    if not geo:
        old_bytes = var.data[a.offset:a.offset + a.size]
        new_bytes = a.value.to_bytes(a.size, "little")
        old_val = int.from_bytes(old_bytes, "little")
        say("Field      : offset 0x%x, width %d byte(s)" % (a.offset, a.size))
        say("Change     : %s -> %s   (all %d other data bytes stay identical)"
            % (old_bytes.hex(), new_bytes.hex(), len(var.data) - a.size))
        if a.expect_old is not None and old_val != a.expect_old:
            refusals.append("the field currently holds 0x%x, you expected 0x%x" % (old_val, a.expect_old))
        noop = old_bytes == new_bytes
        new_data = apply_field(var.data, a.offset, a.size, a.value)
    bset = None
    if a.backup:
        try:
            bset = BackupSet.load(a.backup)
            refusals += bset.problems_vs_live(a.efivars, ref)
            refusals += bset.platform_problems(a.dmi)
        except VarError as exc:
            refusals.append("backup unusable: %s" % exc)
    else:
        refusals.append("no backup: make one with the 'backup' subcommand and pass --backup DIR. "
                        "A write needs a backup of this variable that equals its current contents.")
    if a.apply and not a.risk:
        refusals.append("--i-understand-the-risk is required for a real write")
    if a.apply and real and not is_root():
        refusals.append("not running as root")
    if a.apply and real and efivarfs_is_readonly(a.efivars):
        refusals.append("efivarfs is mounted read-only; remount it read-write first")
    if refusals:
        for r in refusals:
            say("REFUSE: %s" % r)
        say("VERDICT: refused%s. Nothing was written." % ("" if a.apply else " (this is what a dry run found)"))
        return EXIT_REFUSED
    if noop:
        say("NOOP: the field already holds that value. Nothing to write.")
        return EXIT_OK
    say("Reminder: if your firmware keeps a checksum of this variable, the next boot compares it. Writing a "
        "guarded Setup variable alone can make the firmware reload defaults. This subcommand writes no "
        "checksum. Use above4g_unlock.py, which writes the checksum immediately after, or restore.")
    if not a.apply:
        if missing_expect:
            say("Note: --apply will also require %s." % ", ".join(missing_expect))
        say("DRY RUN: every check passed; nothing was written. Add --apply --i-understand-the-risk to write.")
        return EXIT_OK

    with IgnoreSignals():
        return _apply_write(a, ref, var, new_data, bset, real)


def _apply_write(a, ref, var, new_data, bset, real):
    """The write window of cmd_write. Runs with signals ignored; prints nothing until the write
    and its read-back are finished."""
    blob = var.raw[:4] + new_data
    failure, problems = None, []
    with Unlocked([var.path], real):
        unwritable = probe_writable([var.path])
        if not unwritable:
            try:
                raw_write(var.path, blob)
            except BaseException as exc:       # includes KeyboardInterrupt and a broken pipe
                failure = exc
        if failure is None and not unwritable:
            try:
                live = read_var(a.efivars, ref)
            except VarError as exc:
                problems.append(str(exc))
            except BaseException as exc:
                problems.append("verification was interrupted: %s" % describe_failure(exc))
            else:
                if live.raw[:4] != var.raw[:4]:
                    problems.append("attributes changed after the write")
                if len(live.data) != len(var.data):
                    problems.append("length changed after the write: %d -> %d"
                                    % (len(var.data), len(live.data)))
                if live.data != new_data:
                    problems.append("read-back differs from what was written")
                outside = [o for o in diff_offsets(bset.entry(ref).data, live.data)
                           if not (a.offset <= o < a.offset + a.size)]
                if outside:
                    problems.append("bytes outside the intended field changed: %s"
                                    % ", ".join("0x%x" % o for o in outside[:20]))
    if unwritable:
        for p, exc in unwritable:
            say("REFUSE: cannot open %s for writing: %s" % (p, describe_failure(exc)))
        say("VERDICT: refused. Nothing was written.")
        return EXIT_REFUSED
    if failure is None and not problems:
        say("WRITTEN and verified: only offsets 0x%x..0x%x differ from the backup." % (
            a.offset, a.offset + a.size - 1))
        say("Reboot and verify the physical outcome; see README.md, section 'After the reboot'.")
        return EXIT_OK
    if failure is not None:
        say("STOP: the write failed: %s" % describe_failure(failure))
    for p in problems:
        say("STOP: verification failed: %s" % p)
    say("State: %s %s." % (ref, live_state(a.efivars, bset, ref)))
    say("Restoring %s from the backup now (only this variable)." % ref)
    code = restore_backup(a.efivars, bset.path, apply=True, risk_ok=True, dmi_dir=a.dmi,
                          only=[ref], allow_length_change=True, guidance=False, preloaded=bset)
    if code == EXIT_OK:
        say("The automatic restore completed.")
        stop_guidance(say, bset.path, a.efivars, a.dmi, False, recovered=True, bset=bset)
    else:
        say("The automatic restore did not complete.")
        stop_guidance(say, bset.path, a.efivars, a.dmi, is_checksum_guarded(ref.name),
                      lengths_differ(a.efivars, bset, [ref]), bset=bset)
    return EXIT_STOP


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--efivars", default=DEFAULT_EFIVARS, metavar="PATH",
                        help="efivarfs directory (default %(default)s); a synthetic directory of "
                             "<Name>-<guid> files works for testing")
    dmi = argparse.ArgumentParser(add_help=False)
    dmi.add_argument("--dmi", default=DEFAULT_DMI, metavar="PATH",
                     help="DMI directory (default %(default)s)")
    p = argparse.ArgumentParser(
        prog="uefivar.py", description=__doc__.split("\n\n")[0],
        epilog="Run 'python3 uefivar.py --self-test' to exercise everything on a synthetic directory.")
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")

    s = sub.add_parser("read", parents=[common], help="show one variable")
    s.add_argument("var", metavar="NAME-GUID")
    s.add_argument("--offset", type=argint, default=None, help="start of the slice to show")
    s.add_argument("--length", type=argint, default=None, help="length of the slice to show")
    s.add_argument("--hex", action="store_true", help="print only the data as one hex string")
    s.set_defaults(func=cmd_read)

    s = sub.add_parser("list", parents=[common], help="list variables")
    s.add_argument("--grep", metavar="REGEX", help="case-insensitive filter on the file name, "
                   "e.g. 'sum|crc|hash|integrity|error|restore|changed' to look for guard variables")
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("backup", parents=[common, dmi],
                       help="copy raw variables into a timestamped directory")
    s.add_argument("vars", nargs="+", metavar="NAME-GUID",
                   help="variables to back up AND restore, in restore order: Setup first, then its "
                        "checksum variable, then the rest")
    s.add_argument("--dir", required=True, metavar="DIR", help="parent directory for the backup")
    s.add_argument("--reference-only", nargs="+", metavar="NAME-GUID",
                   help="variables saved for the record that restore never writes (flags, counters)")
    s.add_argument("--allow-volatile-backup", action="store_true",
                   help="accept a --dir that is gone after a reboot (tmpfs, ramfs, a live system's "
                        "RAM-backed root); on a live system use a USB stick or a disk partition instead")
    s.set_defaults(func=cmd_backup)

    s = sub.add_parser("checksum", parents=[common],
                       help="sum16 of Setup, the stored checksum, and the live delta")
    s.add_argument("--setup", required=True, metavar="NAME-GUID", help="the Setup variable")
    s.add_argument("--stored", required=True, metavar="NAME-GUID", help="the stored checksum variable")
    s.set_defaults(func=cmd_checksum)

    s = sub.add_parser("diff", parents=[common], help="byte diff of two variable dumps")
    s.add_argument("file_a", metavar="FILE_A")
    s.add_argument("file_b", metavar="FILE_B")
    s.add_argument("--raw", action="store_true", help="files hold plain data, no 4-byte attribute prefix")
    s.add_argument("--max-lines", type=int, default=200)
    s.set_defaults(func=cmd_diff)

    s = sub.add_parser("write", parents=[common, dmi],
                       help="change one field of one variable (DRY RUN unless --apply)")
    s.add_argument("var", metavar="NAME-GUID")
    s.add_argument("--offset", type=argint, required=True, help="byte offset into the DATA (not the file)")
    s.add_argument("--value", type=argint, required=True, help="new value of the field")
    s.add_argument("--size", type=argint, default=1, help="field width in bytes, from your IFR (default 1)")
    s.add_argument("--allow-multibyte", action="store_true",
                   help="confirm a field wider than 1 byte; check the width in your IFR first")
    s.add_argument("--expect-old", type=argint, default=None,
                   help="refuse unless the field holds this now (required with --apply)")
    s.add_argument("--expect-var-len", type=argint, default=None,
                   help="refuse unless the variable holds exactly this many data bytes "
                        "(required with --apply)")
    s.add_argument("--expect-attrs", type=argint, default=None,
                   help="refuse unless the attributes equal this (reference build: 0x7; "
                        "required with --apply)")
    s.add_argument("--backup", metavar="DIR",
                   help="backup directory from 'backup'; its copy must equal the live variable now, "
                        "and its BIOS version must match this machine's")
    s.add_argument("--no-checksum-companion", action="store_true",
                   help="allow writing a Setup or checksum variable on its own: only for boards with "
                        "no checksum guard; on a guarded board use above4g_unlock.py")
    s.add_argument("--apply", action="store_true", help="really write (default is a dry run)")
    s.add_argument("--i-understand-the-risk", dest="risk", action="store_true",
                   help="required with --apply")
    s.set_defaults(func=cmd_write)
    return p


def run_selftest():
    if HERE not in sys.path:
        sys.path.insert(0, HERE)
    import selftest
    return selftest.run()


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--self-test" in argv:
        return run_selftest()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return EXIT_REFUSED
    try:
        return args.func(args)
    except VarError as exc:
        say("ERROR: %s" % exc)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
