#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Self-test for uefivar.py, above4g_unlock.py and restore.py.

  python3 selftest.py            (or: python3 uefivar.py --self-test, or: python3 -m unittest selftest)

Builds a synthetic efivars directory in a temporary folder (<Name>-<guid> files holding a
4-byte little-endian attribute prefix + data), runs every command against it, and removes
it again. Nothing here touches a real firmware variable, and chattr is never called: the
tests fail if it is.

Covers: read, list, backup (and tamper detection), checksum, diff, dry-run, apply, restore,
write order, the refusal paths (wrong size or width, missing backup, stale backup, checksum
delta out of range including a wrapped negative delta, authenticated attribute bit, wrong
length, wrong old value, wrong BIOS, non-zero guard flag, missing confirmations), errno
mapping for EROFS / EACCES / EINVAL, automatic restore when the checksum write fails, no
rollback when PCI_COMMON fails after the pair was written, and the whole-blob diff that
catches a stray byte change.

Also covers the safety fixes found by code review, each with a test that fails on the old
code: a KeyboardInterrupt or a BrokenPipeError between the Setup and checksum writes, signals
ignored during every write window, nothing printed between writes, a restore onto a variable of
another length, a backup with no recorded BIOS version or from another board, a swapped,
tampered or malformed manifest, fsync failures and tmpfs backups, `write --apply` without its
expectations or without --no-checksum-companion, and a reference-only entry used as a restore
point.

NOT covered, and it cannot be from here: a real efivarfs mount, real firmware behaviour,
or the immutable-flag handling (chattr / lsattr).
"""

import contextlib
import errno
import hashlib
import io
import json
import os
import random
import re
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock
import builtins

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import above4g_unlock as au  # noqa: E402
import restore as rs  # noqa: E402
import uefivar as uv  # noqa: E402

SETUP = "Setup-ec87d643-eba4-4bb5-a1e5-3f3e36b20da9"
LCS = "LastCheckSum-e102c8aa-0f10-4a0e-90a7-a6f816089bb7"
PCI = "PCI_COMMON-aca9f304-21e2-4852-9875-7ff4881d67a5"
FLAG_NAMES = ["SetupDataChecksumError", "RestoreSetupDataFlag", "BIOSSetupDataChangedFlag"]
OTHER_GUID = "00000000-1111-2222-3333-444444444444"
BIOS = "M1UKT72A"
OFFSET = 0xA49


class Box(object):
    """A synthetic machine: an efivars directory, a dmi directory, a profile."""

    def __init__(self, root, delta=1, setup_attrs=7, setup_len=5000, old_byte=0, bios=BIOS):
        self.root = root
        self.ef = os.path.join(root, "efivars")
        self.dmi = os.path.join(root, "dmi")
        os.makedirs(self.ef)
        os.makedirs(self.dmi)
        with open(os.path.join(self.dmi, "bios_version"), "w") as fh:
            fh.write(bios + "\n")
        for key, val in (("board_name", "TESTBOARD"), ("product_name", "TESTPRODUCT")):
            with open(os.path.join(self.dmi, key), "w") as fh:
                fh.write(val + "\n")
        rnd = random.Random(20240601)
        data = bytearray(rnd.getrandbits(8) for _ in range(setup_len))
        data[10], data[11] = 0x0A, 0x0D          # bytes a text-mode write would corrupt
        if setup_len > OFFSET:
            data[OFFSET] = old_byte
        self.put(SETUP, setup_attrs, bytes(data))
        self.put(LCS, 7, ((uv.sum16(data) + delta) & 0xFFFF).to_bytes(2, "little"))
        self.put(PCI, 7, bytes([0, 0, 1, 0, 0, 0, 0]))
        for n in FLAG_NAMES:
            self.put("%s-%s" % (n, OTHER_GUID), 7, b"\x00")
        self.put("SysEventLogCount-%s" % OTHER_GUID, 7, b"\x06")
        self.put("LastSetupdataSum-e102c8aa-0f10-4a0e-90a7-a6f816089bb7", 7, b"\x23\xdc")
        with open(os.path.join(HERE, "profile.example.json"), "r", encoding="utf-8") as fh:
            prof = json.load(fh)
        prof["verified_by_user"] = True
        self.profile = os.path.join(root, "profile.json")
        self.write_profile(prof)

    def write_profile(self, prof):
        with open(self.profile, "w", encoding="utf-8") as fh:
            json.dump(prof, fh)

    def put(self, name, attrs, data):
        with open(os.path.join(self.ef, name), "wb") as fh:
            fh.write(attrs.to_bytes(4, "little") + bytes(data))

    def raw(self, name):
        with open(os.path.join(self.ef, name), "rb") as fh:
            return fh.read()

    def data(self, name):
        return self.raw(name)[4:]

    def snapshot(self):
        return {n: self.raw(n) for n in sorted(os.listdir(self.ef))}

    def core(self):
        return {n: self.raw(n) for n in (SETUP, LCS, PCI)}


def call(main, argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        code = main(argv)
    return code, buf.getvalue()


def call_checked(case, main, argv):
    """call(), but an exception that escapes main() fails the test instead of aborting the run."""
    try:
        return call(main, argv)
    except BaseException as exc:                  # KeyboardInterrupt and BrokenPipeError included
        case.fail("%s escaped %s.main with a traceback instead of reaching the STOP path"
                  % (type(exc).__name__, main.__module__))


def read_json(path):
    with open(path, "r", encoding="utf-8") as fh:
        return json.load(fh)


def writes_recorder():
    """Wrap uefivar.raw_write; returns (patch context, list of file names in write order)."""
    order = []
    orig = uv.raw_write

    def rec(path, blob):
        order.append(os.path.basename(path))
        return orig(path, blob)
    return mock.patch.object(uv, "raw_write", rec), order, orig


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="uefivar-selftest-")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        guard = mock.patch.object(uv, "_chattr", side_effect=AssertionError("chattr called on a synthetic dir"))
        guard.start()
        self.addCleanup(guard.stop)
        # The host may keep its temp directory on tmpfs; tests that need a mount table patch it again.
        mounts = mock.patch.object(uv, "_proc_mounts_lines", return_value=None)
        mounts.start()
        self.addCleanup(mounts.stop)
        self.box = Box(self.tmp)
        self.bdir = os.path.join(self.tmp, "backups")

    def common(self, box=None):
        b = box or self.box
        return ["--efivars", b.ef]

    def backup(self, extra=()):
        code, out = call(uv.main, ["backup", SETUP, LCS, PCI, "--dir", self.bdir, "--efivars", self.box.ef,
                                   "--dmi", self.box.dmi] + list(extra))
        self.assertEqual(code, 0, out)
        names = sorted(os.listdir(self.bdir))
        return os.path.join(self.bdir, names[-1])

    def unlock_args(self, *more, **kw):
        b = kw.get("box") or self.box
        return ["unlock", "--profile", b.profile, "--efivars", b.ef, "--dmi", b.dmi] + list(more)

    def apply_argv(self, path, *more):
        """A complete, valid `write --apply` of the reference field. Setup needs
        --no-checksum-companion: this synthetic box stands for a board with no checksum guard."""
        return (["write", SETUP, "--offset", "0xa49", "--value", "1", "--expect-old", "0",
                 "--expect-var-len", "5000", "--expect-attrs", "7", "--no-checksum-companion",
                 "--backup", path, "--apply", "--i-understand-the-risk", "--dmi", self.box.dmi]
                + self.common() + list(more))

    def token(self, box=None):
        code, out = call(au.main, self.unlock_args(box=box))
        self.assertEqual(code, 0, out)
        m = re.search(r"--confirm (\S+)", out)
        self.assertTrue(m, out)
        return m.group(1)

    def apply_args(self, token=None, *more):
        return self.unlock_args("--apply", "--i-understand-the-risk", "--backup-dir", self.bdir,
                                "--confirm", token or self.token(), *more)


# ---------------------------------------------------------------------------
class TestUefivarBasics(Base):
    def test_parse_ref(self):
        r = uv.VarRef.parse("Setup-EC87D643-EBA4-4BB5-A1E5-3F3E36B20DA9")
        self.assertEqual((r.name, r.filename), ("Setup", SETUP))
        r = uv.VarRef.parse("Some-Dashed-Name-" + OTHER_GUID)
        self.assertEqual(r.name, "Some-Dashed-Name")
        for bad in ("Setup", "Setup-1234", "-" + OTHER_GUID, "../x-" + OTHER_GUID):
            with self.assertRaises(uv.VarError):
                uv.VarRef.parse(bad)

    def test_read(self):
        code, out = call(uv.main, ["read", SETUP] + self.common())
        self.assertEqual(code, 0, out)
        self.assertIn("0x00000007", out)
        self.assertIn("NON_VOLATILE|BOOTSERVICE_ACCESS|RUNTIME_ACCESS", out)
        self.assertIn("5000 (0x1388) data bytes", out)
        code, out = call(uv.main, ["read", LCS, "--hex"] + self.common())
        self.assertEqual(out.strip(), self.box.data(LCS).hex())
        code, out = call(uv.main, ["read", "Nope-" + OTHER_GUID] + self.common())
        self.assertEqual(code, 2)

    def test_list(self):
        code, out = call(uv.main, ["list", "--grep", "flag|error"] + self.common())
        self.assertEqual(code, 0)
        self.assertIn("RestoreSetupDataFlag", out)
        self.assertIn("SetupDataChecksumError", out)
        self.assertNotIn("PCI_COMMON", out)

    def test_checksum_measures_live_delta(self):
        for delta, expect in ((1, "(+1)"), (0, "(+0)")):
            box = Box(tempfile.mkdtemp(dir=self.tmp), delta=delta)
            code, out = call(uv.main, ["checksum", "--setup", SETUP, "--stored", LCS, "--efivars", box.ef])
            self.assertEqual(code, 0, out)
            self.assertIn("DELTA", out)
            self.assertIn(expect, out)
            self.assertIn("not a constant", out)
        box = Box(tempfile.mkdtemp(dir=self.tmp), delta=-1)
        code, out = call(uv.main, ["checksum", "--setup", SETUP, "--stored", LCS, "--efivars", box.ef])
        self.assertIn("(-1)", out)

    def test_checksum_refuses_wrong_width_stored_var(self):
        code, out = call(uv.main, ["checksum", "--setup", SETUP, "--stored", PCI, "--efivars", self.box.ef])
        self.assertEqual(code, 2, out)

    def test_diff(self):
        a = os.path.join(self.tmp, "a.bin")
        b = os.path.join(self.tmp, "b.bin")
        raw = self.box.raw(SETUP)
        mod = bytearray(raw)
        mod[4 + OFFSET] = 1
        with open(a, "wb") as fh:
            fh.write(raw)
        with open(b, "wb") as fh:
            fh.write(mod)
        code, out = call(uv.main, ["diff", a, b] + self.common())
        self.assertEqual(code, 1, out)
        self.assertIn("0x0a49: " + "%02x -> 01" % raw[4 + OFFSET], out)
        self.assertIn("1 byte(s) differ", out)
        code, out = call(uv.main, ["diff", a, a] + self.common())
        self.assertEqual(code, 0, out)
        code, out = call(uv.main, ["diff", SETUP, a] + self.common())   # resolved inside --efivars
        self.assertEqual(code, 0, out)

    def test_synthetic_dir_is_not_real_efivarfs(self):
        self.assertFalse(uv.is_real_efivarfs(self.box.ef))


class TestBackup(Base):
    def test_backup_roundtrip_and_manifest(self):
        path = self.backup(["--reference-only", "SysEventLogCount-" + OTHER_GUID])
        bs = uv.BackupSet.load(path)
        self.assertEqual([e.ref.filename for e in bs.entries][:3], [SETUP, LCS, PCI])
        self.assertEqual([e.restore for e in bs.entries], [True, True, True, False])
        for e in bs.entries[:3]:
            self.assertEqual(e.raw, self.box.raw(e.ref.filename))
        self.assertEqual(bs.bios_version, BIOS)
        self.assertTrue(os.path.exists(os.path.join(path, "MANIFEST.sha256")))

    def test_tampered_backup_is_rejected(self):
        path = self.backup()
        with open(os.path.join(path, SETUP + ".bin"), "r+b") as fh:
            fh.seek(100)
            fh.write(b"\xff")
        with self.assertRaises(uv.VarError):
            uv.BackupSet.load(path)
        code, out = call(rs.main, [path, "--efivars", self.box.ef, "--dmi", self.box.dmi])
        self.assertEqual(code, 2, out)
        self.assertIn("sha256", out)


class TestWriteGuards(Base):
    def w(self, *more, **kw):
        argv = (["write", SETUP, "--offset", hex(OFFSET), "--value", "1", "--dmi", self.box.dmi]
                + self.common() + list(more))
        before = self.box.snapshot()
        code, out = call(uv.main, argv)
        if kw.get("expect_unchanged", True):
            self.assertEqual(self.box.snapshot(), before, "a refused or dry-run write changed a variable")
        return code, out

    def test_dry_run_by_default_writes_nothing(self):
        path = self.backup()
        code, out = self.w("--backup", path, "--expect-old", "0", "--expect-var-len", "5000", "--expect-attrs", "7",
                           "--no-checksum-companion")
        self.assertEqual(code, 0, out)
        self.assertIn("DRY RUN", out)
        self.assertIn("00 -> 01".replace(" ", ""), out.replace(" ", ""))

    def test_refuses_wrong_sizes(self):
        path = self.backup()
        cases = [
            (["--size", "8"], "--allow-multibyte"),                  # the forum "write 8 bytes" mistake
            (["--size", "2"], "--allow-multibyte"),
            (["--size", "3", "--allow-multibyte"], "not 1, 2, 4 or 8"),
            (["--value", "0x100"], "does not fit in 1 byte"),
            (["--expect-var-len", "4096"], "you stated 4096"),
            (["--offset", "5000"], "runs past the end"),
        ]
        for extra, needle in cases:
            argv = ["--backup", path] + extra
            code, out = self.w(*argv)
            self.assertEqual(code, 2, (extra, out))
            self.assertIn("REFUSE", out)
            self.assertIn(needle, out, extra)

    def test_multibyte_needs_explicit_confirmation_but_then_passes_dry_run(self):
        path = self.backup()
        code, out = self.w("--backup", path, "--size", "8", "--allow-multibyte", "--no-checksum-companion")
        self.assertEqual(code, 0, out)
        self.assertIn("all 4992 other data bytes stay identical", out)
        self.assertIn("--apply will also require --expect-old, --expect-var-len, --expect-attrs", out)

    def test_refuses_without_backup(self):
        code, out = self.w()
        self.assertEqual(code, 2)
        self.assertIn("no backup", out)

    def test_refuses_stale_backup(self):
        path = self.backup()
        data = bytearray(self.box.data(SETUP))
        data[200] ^= 0xFF
        self.box.put(SETUP, 7, bytes(data))
        code, out = self.w("--backup", path)
        self.assertEqual(code, 2)
        self.assertIn("differs from the backup", out)

    def test_refuses_authenticated_attribute_bits(self):
        for bit in (0x10, 0x20, 0x80):
            box = Box(tempfile.mkdtemp(dir=self.tmp), setup_attrs=7 | bit)
            bdir = os.path.join(self.tmp, "b%x" % bit)
            c, o = call(uv.main, ["backup", SETUP, "--dir", bdir, "--efivars", box.ef, "--dmi", box.dmi])
            self.assertEqual(c, 0, o)
            path = os.path.join(bdir, os.listdir(bdir)[0])
            code, out = call(uv.main, ["write", SETUP, "--offset", "0xa49", "--value", "1", "--backup", path,
                                       "--efivars", box.ef, "--apply", "--i-understand-the-risk"])
            self.assertEqual(code, 2, out)
            self.assertIn("authenticated variable", out)
            self.assertEqual(box.data(SETUP)[OFFSET], 0)

    def test_refuses_wrong_old_value_and_attrs(self):
        path = self.backup()
        code, out = self.w("--backup", path, "--expect-old", "5")
        self.assertEqual(code, 2)
        self.assertIn("expected 0x5", out)
        code, out = self.w("--backup", path, "--expect-attrs", "0x3")
        self.assertEqual(code, 2)
        self.assertIn("expected 0x3", out)

    def test_apply_needs_the_risk_flag(self):
        path = self.backup()
        code, out = self.w("--backup", path, "--apply")
        self.assertEqual(code, 2)
        self.assertIn("--i-understand-the-risk", out)

    def test_apply_changes_exactly_the_field(self):
        path = self.backup()
        before = self.box.snapshot()
        rec, order, _ = writes_recorder()
        with rec:
            code, out = call(uv.main, self.apply_argv(path))
        self.assertEqual(code, 0, out)
        self.assertEqual(order, [SETUP])
        after = self.box.snapshot()
        self.assertEqual({k for k in after if after[k] != before[k]}, {SETUP})
        old, new = before[SETUP], after[SETUP]
        self.assertEqual(len(old), len(new))
        self.assertEqual(old[:4], new[:4])                         # attributes preserved
        self.assertEqual([i for i in range(len(old)) if old[i] != new[i]], [4 + OFFSET])
        self.assertEqual(new[4 + OFFSET], 1)
        # a second identical call is a no-op, not a second write
        code, out = call(uv.main, self.apply_argv(self.backup(), "--expect-old", "1"))
        self.assertIn("NOOP", out)

    def test_write_failures_print_stop_and_errno_meaning(self):
        cases = ((errno.EROFS, "EROFS", "write-protected"),
                 (errno.EACCES, "EACCES", "security violation"),
                 (errno.EINVAL, "EINVAL", "cannot tell"))
        for code_no, name, meaning in cases:
            path = self.backup()
            before = self.box.snapshot()
            with mock.patch.object(uv, "raw_write", side_effect=OSError(code_no, os.strerror(code_no))):
                code, out = call(uv.main, self.apply_argv(path))
            self.assertEqual(code, 3, out)
            self.assertIn("STOP", out)
            self.assertIn(name, out)
            self.assertIn(meaning, out)
            self.assertEqual(self.box.snapshot(), before)
            self.assertIn(uv.restore_command(path, self.box.ef, self.box.dmi), out)     # F9

    def test_short_write_is_a_stop(self):
        path = self.backup()
        with mock.patch.object(uv, "raw_write", side_effect=uv.ShortWrite("short write: 10 of 5004 bytes")):
            code, out = call(uv.main, self.apply_argv(path))
        self.assertEqual(code, 3, out)
        self.assertIn("STOP", out)

    def test_os_write_returning_fewer_bytes_is_caught_and_restored(self):
        path = self.backup()
        before = self.box.snapshot()
        real_write, fired = os.write, []

        def short_once(fd, data):
            if not fired and len(data) > 1000:
                fired.append(1)
                return real_write(fd, data[:10])       # leaves a truncated file behind
            return real_write(fd, data)
        with mock.patch.object(os, "write", short_once):
            code, out = call(uv.main, self.apply_argv(path))
        self.assertEqual(code, 3, out)
        self.assertIn("short write", out)
        self.assertIn("RESTORED", out)
        self.assertEqual(self.box.snapshot(), before)

    def test_write_that_changes_a_stray_byte_fails_and_restores(self):
        path = self.backup()
        before = self.box.snapshot()
        orig, fired = uv.raw_write, []

        def corrupting(p, blob):
            orig(p, blob)
            if not fired:
                fired.append(1)
                raw = bytearray(open(p, "rb").read())
                raw[4 + 100] ^= 0xFF
                open(p, "wb").write(bytes(raw))
        with mock.patch.object(uv, "raw_write", corrupting):
            code, out = call(uv.main, self.apply_argv(path))
        self.assertEqual(code, 3, out)
        self.assertIn("outside the intended field", out)
        self.assertIn("RESTORED", out)
        self.assertEqual(self.box.snapshot(), before)


# ---------------------------------------------------------------------------
class TestWorkflowDryRun(Base):
    def test_dry_run_prints_plan_and_changes_nothing(self):
        before = self.box.snapshot()
        code, out = call(au.main, self.unlock_args())
        self.assertEqual(code, 0, out)
        self.assertEqual(self.box.snapshot(), before)
        self.assertIn("DRY RUN complete", out)
        self.assertIn("AFTER THE WRITE", out)
        self.assertIn("Do NOT install the card yet", out)
        self.assertIn("live delta = stored - sum16 = 1", out)
        self.assertIn("--confirm %s:0xa49:0x3" % BIOS, out)
        self.assertFalse(os.path.exists(self.bdir), "a dry run must not create a backup directory")

    def test_dry_run_with_unverified_profile_notes_it(self):
        prof = json.load(open(self.box.profile))
        prof["verified_by_user"] = False
        self.box.write_profile(prof)
        code, out = call(au.main, self.unlock_args())
        self.assertEqual(code, 0, out)
        self.assertIn("will be refused until", out)

    def test_example_profile_as_shipped_is_unverified_and_complete(self):
        P, errs = au.load_profile(os.path.join(HERE, "profile.example.json"))
        self.assertEqual(errs, [])
        self.assertFalse(P["verified"])
        self.assertIn("EXAMPLE", P["title"])
        self.assertEqual(sorted(P["fields"]), ["pci_common", "setup"])
        with open(os.path.join(HERE, "profile.example.json")) as fh:
            self.assertIn("EXAMPLE - verify against YOUR firmware", json.load(fh)["_example_warning"])

    def test_script_has_no_board_data(self):
        with open(os.path.join(HERE, "above4g_unlock.py"), "r", encoding="utf-8") as fh:
            src = fh.read().lower()
        for marker in ("a49", "0x993", "aca9f304", "ec87d643", "e102c8aa", "m1ukt", "0x1388"):
            self.assertNotIn(marker, src)

    def test_only_setup(self):
        code, out = call(au.main, self.unlock_args("--only", "setup"))
        self.assertEqual(code, 0, out)
        self.assertNotIn("PCI_COMMON offset", out)
        self.assertRegex(out, r"(?m)--confirm %s:0xa49\s*$" % BIOS)


class TestWorkflowRefusals(Base):
    def refused(self, box, needle, *extra, **kw):
        before = box.snapshot()
        argv = self.unlock_args(*extra, box=box)
        code, out = call(au.main, argv)
        self.assertEqual(code, 2, out)
        self.assertIn(needle, out)
        self.assertEqual(box.snapshot(), before)
        if kw.get("no_backup", True):
            self.assertFalse(os.path.exists(self.bdir))
        return out

    def other(self, **kw):
        return Box(tempfile.mkdtemp(dir=self.tmp), **kw)

    def test_delta_out_of_range(self):
        self.refused(self.other(delta=5), "Outside the bound")

    def test_negative_delta_wraps_and_is_refused(self):
        self.refused(self.other(delta=-1), "Outside the bound")

    def test_delta_zero_and_one_are_both_fine(self):
        for d in (0, 1):
            code, out = call(au.main, self.unlock_args(box=self.other(delta=d)))
            self.assertEqual(code, 0, out)
            self.assertIn("delta = stored - sum16 = %d" % d, out)

    def test_authenticated_attribute_bit(self):
        self.refused(self.other(setup_attrs=7 | 0x20), "authenticated variable")

    def test_wrong_variable_length(self):
        self.refused(self.other(setup_len=4999), "holds 4999 data bytes")

    def test_wrong_old_value(self):
        self.refused(self.other(old_byte=5), "Unknown state")

    def test_wrong_bios(self):
        self.refused(self.other(bios="XXXX99Z"), "Offsets move between builds")

    def test_max_delta_cap_is_four(self):
        prof = json.load(open(self.box.profile))
        for value, accepted in ((4, True), (5, False), (16, False)):
            prof["checksum"]["max_delta"] = value
            self.box.write_profile(prof)
            code, out = call(au.main, self.unlock_args())
            if accepted:
                self.assertEqual(code, 0, out)
            else:
                self.assertEqual(code, 2, (value, out))
                self.assertIn("checksum.max_delta must be 0..4", out)

    def test_unreadable_dmi_fails_closed(self):
        box = self.other()
        os.remove(os.path.join(box.dmi, "bios_version"))
        self.refused(box, "cannot read")

    def test_nonzero_guard_flag(self):
        box = self.other()
        box.put("SetupDataChecksumError-" + OTHER_GUID, 7, b"\x01")
        self.refused(box, "guard flag SetupDataChecksumError is 01")

    def test_profile_errors(self):
        prof = json.load(open(self.box.profile))
        del prof["firmware"]
        prof["checksum"] = {"model": "something-else"}
        prof["fields"]["setup"]["width"] = 8
        self.box.write_profile(prof)
        code, out = call(au.main, self.unlock_args())
        self.assertEqual(code, 2, out)
        self.assertIn("PROFILE ERROR: firmware.bios_version is required", out)
        self.assertIn("fail closed", out)
        self.assertIn("--allow-multibyte", out)

    def test_apply_gates(self):
        token = self.token()
        base = self.unlock_args("--apply")
        before = self.box.snapshot()
        cases = [
            (base + ["--backup-dir", self.bdir, "--confirm", token], "--i-understand-the-risk is required"),
            (base + ["--i-understand-the-risk", "--confirm", token], "--backup-dir DIR is required"),
            (base + ["--i-understand-the-risk", "--backup-dir", self.bdir, "--confirm", "nope"], "--confirm %s" % token),
            (base + ["--i-understand-the-risk", "--backup-dir", self.bdir], "--confirm %s" % token),
        ]
        for argv, needle in cases:
            code, out = call(au.main, argv)
            self.assertEqual(code, 2, out)
            self.assertIn(needle, out)
        prof = json.load(open(self.box.profile))
        prof["verified_by_user"] = False
        self.box.write_profile(prof)
        code, out = call(au.main, self.apply_args(token))
        self.assertEqual(code, 2, out)
        self.assertIn("not marked", out)
        self.assertEqual(self.box.snapshot(), before)
        self.assertFalse(os.path.exists(self.bdir))


class TestWorkflowApply(Base):
    def test_apply_end_to_end(self):
        for delta in (1, 0):
            self.tmp2 = tempfile.mkdtemp(dir=self.tmp)
            box = Box(self.tmp2, delta=delta)
            self.bdir = os.path.join(self.tmp2, "backups")
            before = box.snapshot()
            token = self.token(box)
            rec, order, _ = writes_recorder()
            with rec:
                code, out = call(au.main, self.unlock_args("--apply", "--i-understand-the-risk", "--backup-dir",
                                                          self.bdir, "--confirm", token, box=box))
            self.assertEqual(code, 0, out)
            self.assertIn("VERIFIED", out)
            self.assertEqual(order, [SETUP, LCS, PCI])              # Setup, checksum immediately, then PCI_COMMON
            after = box.snapshot()
            self.assertEqual({k for k in after if after[k] != before[k]}, {SETUP, LCS, PCI})
            # Setup: exactly one byte, attributes preserved
            o, n = before[SETUP], after[SETUP]
            self.assertEqual(o[:4], n[:4])
            self.assertEqual([i for i in range(len(o)) if o[i] != n[i]], [4 + OFFSET])
            self.assertEqual(n[4 + OFFSET], 1)
            # checksum: sum16(new Setup) + the delta that was measured live
            self.assertEqual(int.from_bytes(after[LCS][4:], "little"), (uv.sum16(n[4:]) + delta) & 0xFFFF)
            self.assertEqual(after[LCS][:4], before[LCS][:4])
            # PCI_COMMON: byte 3 only; byte 0 never touched
            o, n = before[PCI], after[PCI]
            self.assertEqual([i for i in range(len(o)) if o[i] != n[i]], [4 + 3])
            self.assertEqual(n[4], o[4])
            # flags and counters untouched
            for k in after:
                if k not in (SETUP, LCS, PCI):
                    self.assertEqual(after[k], before[k])
            # backup + report
            bk = os.path.join(self.bdir, os.listdir(self.bdir)[0])
            bs = uv.BackupSet.load(bk)
            self.assertEqual([e.restore for e in bs.entries].count(False), 5)   # 3 flags + counter + extra sum
            for e in bs.entries:
                self.assertEqual(e.raw, before[e.ref.filename])
            rep = read_json(os.path.join(bk, "report.json"))
            self.assertEqual(rep["status"], "applied")
            self.assertEqual(rep["delta"], delta)

    def test_second_run_finds_nothing_to_do(self):
        token = self.token()
        code, out = call(au.main, self.apply_args(token))
        self.assertEqual(code, 0, out)
        code, out = call(au.main, self.unlock_args())
        self.assertEqual(code, 0, out)
        self.assertIn("Nothing to do", out)

    def test_only_setup_then_pci(self):
        before = self.box.snapshot()
        code, out = call(au.main, self.unlock_args("--apply", "--i-understand-the-risk", "--backup-dir",
                                                  self.bdir, "--only", "setup", "--confirm",
                                                  "%s:0xa49" % BIOS))
        self.assertEqual(code, 0, out)
        mid = self.box.snapshot()
        self.assertEqual({k for k in mid if mid[k] != before[k]}, {SETUP, LCS})
        code, out = call(au.main, self.unlock_args("--apply", "--i-understand-the-risk", "--backup-dir",
                                                  self.bdir, "--only", "pci_common", "--confirm",
                                                  "%s:0x3" % BIOS))
        self.assertEqual(code, 0, out)
        end = self.box.snapshot()
        self.assertEqual({k for k in end if end[k] != mid[k]}, {PCI})

    def _failing_write(self, fail_prefix, exc):
        orig = uv.raw_write

        def f(path, blob):
            if os.path.basename(path).startswith(fail_prefix):
                raise exc
            return orig(path, blob)
        return f

    def test_checksum_write_fails_so_setup_is_rewritten_from_backup(self):
        before = self.box.snapshot()
        token = self.token()
        with mock.patch.object(uv, "raw_write", self._failing_write("LastCheckSum", OSError(errno.EROFS, "ro"))):
            code, out = call(au.main, self.apply_args(token))
        self.assertEqual(code, 3, out)
        self.assertIn("STOP: writing %s failed" % LCS, out)
        self.assertIn("EROFS", out)
        self.assertIn("write-protected", out)
        self.assertIn("RESTORED", out)
        self.assertEqual(self.box.snapshot(), before)

    def test_setup_write_fails_with_einval(self):
        before = self.box.snapshot()
        token = self.token()
        with mock.patch.object(uv, "raw_write", self._failing_write("Setup-", OSError(errno.EINVAL, "inval"))):
            code, out = call(au.main, self.apply_args(token))
        self.assertEqual(code, 3, out)
        self.assertIn("EINVAL", out)
        self.assertEqual(self.box.snapshot(), before)
        self.assertIn("still hold their original values", out)       # nothing changed, nothing rolled back

    def test_stray_setup_byte_after_write_fails_the_run_and_restores(self):
        before = self.box.snapshot()
        token = self.token()
        orig, fired = uv.raw_write, []

        def corrupting(p, blob):
            orig(p, blob)
            if os.path.basename(p).startswith("Setup-") and not fired:
                fired.append(1)
                raw = bytearray(open(p, "rb").read())
                raw[4 + 1234] ^= 0x55           # a byte the firmware "maintains"
                open(p, "wb").write(bytes(raw))
        with mock.patch.object(uv, "raw_write", corrupting):
            code, out = call(au.main, self.apply_args(token))
        self.assertEqual(code, 3, out)
        self.assertIn("outside the intended range changed: 0x4d2", out)
        self.assertEqual(self.box.snapshot(), before)


class TestVerify(Base):
    IOMEM_OK = "00000000-00000fff : Reserved\n  4000000000-7fffffffff : PCI Bus 0000:00\n"
    IOMEM_NONE = "00000000-00000fff : Reserved\n  bf000000-bfffffff : PCI Bus 0000:00\n"
    IOMEM_HIDDEN = "00000000-00000000 : Reserved\n  00000000-00000000 : PCI Bus 0000:00\n"

    def setUp(self):
        Base.setUp(self)
        code, out = call(au.main, self.apply_args())
        self.assertEqual(code, 0, out)
        self.report = os.path.join(self.bdir, os.listdir(self.bdir)[0])

    def iomem(self, text):
        p = os.path.join(self.tmp, "iomem")
        with open(p, "w") as fh:
            fh.write(text)
        return p

    def v(self, iomem_text, *more):
        return call(au.main, ["verify", "--profile", self.box.profile, "--efivars", self.box.ef, "--dmi",
                              self.box.dmi, "--report", self.report, "--iomem", self.iomem(iomem_text)] + list(more))

    def test_pass_with_high_window(self):
        code, out = self.v(self.IOMEM_OK)
        self.assertEqual(code, 0, out)
        self.assertIn("the change persisted", out)
        self.assertIn("PCI range above 4 GiB", out)
        self.assertIn("does not by itself prove the unlock", out)

    def test_no_window_is_a_warning_not_a_pass_claim(self):
        code, out = self.v(self.IOMEM_NONE)
        self.assertEqual(code, 0, out)
        self.assertIn("no 'PCI Bus' range above 4 GiB", out)
        code, out = self.v(self.IOMEM_HIDDEN)
        self.assertIn("run as root", out)

    def test_reverted_byte_fails(self):
        data = bytearray(self.box.data(SETUP))
        data[OFFSET] = 0
        self.box.put(SETUP, 7, bytes(data))
        code, out = self.v(self.IOMEM_OK)
        self.assertEqual(code, 2, out)
        self.assertIn("back at its default", out)

    def test_flag_and_watch_changes_fail(self):
        self.box.put("SysEventLogCount-" + OTHER_GUID, 7, b"\x07")
        code, out = self.v(self.IOMEM_OK)
        self.assertEqual(code, 2, out)
        self.assertIn("firmware logged something", out)
        self.box.put("SysEventLogCount-" + OTHER_GUID, 7, b"\x06")
        self.box.put("RestoreSetupDataFlag-" + OTHER_GUID, 7, b"\x01")
        code, out = self.v(self.IOMEM_OK)
        self.assertEqual(code, 2, out)
        self.assertIn("guard flag RestoreSetupDataFlag = 01", out)


# ---------------------------------------------------------------------------
class TestRestore(Base):
    def applied(self):
        before = self.box.snapshot()
        code, out = call(au.main, self.apply_args())
        self.assertEqual(code, 0, out)
        return before, os.path.join(self.bdir, os.listdir(self.bdir)[0])

    def r(self, bk, *more):
        return call(rs.main, [bk, "--efivars", self.box.ef, "--dmi", self.box.dmi] + list(more))

    def test_dry_run_default_then_apply_restores_in_order(self):
        before, bk = self.applied()
        changed = self.box.snapshot()
        code, out = self.r(bk)
        self.assertEqual(code, 0, out)
        self.assertIn("DRY RUN", out)
        self.assertEqual(self.box.snapshot(), changed)
        code, out = self.r(bk, "--apply")
        self.assertEqual(code, 2, out)
        self.assertIn("--i-understand-the-risk", out)
        rec, order, _ = writes_recorder()
        with rec:
            code, out = self.r(bk, "--apply", "--i-understand-the-risk")
        self.assertEqual(code, 0, out)
        self.assertEqual(order, [SETUP, LCS, PCI])
        self.assertIn("RESTORED and verified by read-back", out)
        self.assertEqual(self.box.snapshot(), before)
        code, out = self.r(bk, "--apply", "--i-understand-the-risk")
        self.assertIn("Nothing to restore", out)

    def test_refuses_attribute_mismatch(self):
        before, bk = self.applied()
        self.box.put(SETUP, 7 | 0x20, self.box.data(SETUP))
        snap = self.box.snapshot()
        code, out = self.r(bk, "--apply", "--i-understand-the-risk")
        self.assertEqual(code, 2, out)
        self.assertIn("attributes cannot be changed by overwriting", out)
        self.assertEqual(self.box.snapshot(), snap)

    def test_refuses_authenticated_variable_even_when_attributes_match(self):
        box = Box(tempfile.mkdtemp(dir=self.tmp), setup_attrs=7 | 0x20)
        bdir = os.path.join(self.tmp, "authbk")
        c, o = call(uv.main, ["backup", SETUP, "--dir", bdir, "--efivars", box.ef, "--dmi", box.dmi])
        self.assertEqual(c, 0, o)
        bk = os.path.join(bdir, os.listdir(bdir)[0])
        data = bytearray(box.data(SETUP))
        data[300] ^= 0xFF
        box.put(SETUP, 7 | 0x20, bytes(data))                   # attributes equal the backup's, data differs
        snap = box.snapshot()
        code, out = call(rs.main, [bk, "--efivars", box.ef, "--dmi", box.dmi, "--apply", "--i-understand-the-risk"])
        self.assertEqual(code, 2, out)
        self.assertIn("authenticated variable", out)
        self.assertEqual(box.snapshot(), snap)

    def test_refuses_other_bios(self):
        before, bk = self.applied()
        with open(os.path.join(self.box.dmi, "bios_version"), "w") as fh:
            fh.write("OTHER1\n")
        snap = self.box.snapshot()
        code, out = self.r(bk, "--apply", "--i-understand-the-risk")
        self.assertEqual(code, 2, out)
        self.assertIn("different build", out)
        self.assertEqual(self.box.snapshot(), snap)

    def test_restore_failure_is_a_stop(self):
        before, bk = self.applied()
        with mock.patch.object(uv, "raw_write", side_effect=OSError(errno.EIO, "io")):
            code, out = self.r(bk, "--apply", "--i-understand-the-risk")
        self.assertEqual(code, 3, out)
        self.assertIn("STOP", out)
        self.assertIn("EIO", out)
        self.assertIn("Recovery ladder", out)

    def test_reference_only_variables_are_never_written(self):
        before, bk = self.applied()
        self.box.put("SysEventLogCount-" + OTHER_GUID, 7, b"\x09")
        rec, order, _ = writes_recorder()
        with rec:
            code, out = self.r(bk, "--apply", "--i-understand-the-risk")
        self.assertEqual(code, 0, out)
        self.assertNotIn("SysEventLogCount-" + OTHER_GUID, order)
        self.assertEqual(self.box.data("SysEventLogCount-" + OTHER_GUID), b"\x09")

    def test_self_test_flag_is_wired(self):
        self.assertTrue(callable(uv.run_selftest))


# ---------------------------------------------------------------------------
# Safety fixes found by code review. Each test below fails on the code as it was before its fix.
# ---------------------------------------------------------------------------

SIGNAL_NAMES = [n for n in ("SIGINT", "SIGTERM", "SIGHUP", "SIGQUIT") if hasattr(signal, n)]


def handlers_now():
    return {n: signal.getsignal(getattr(signal, n)) for n in SIGNAL_NAMES}


def interrupt_after(prefix, exc, seen=None):
    """A raw_write that completes the write to the file whose name starts with prefix and then
    raises exc, once (exc None: never raises). seen: a list that receives the signal handlers in
    force during every write."""
    orig = uv.raw_write
    fired = []

    def w(path, blob):
        if seen is not None:
            seen.append(handlers_now())
        orig(path, blob)
        if exc is not None and os.path.basename(path).startswith(prefix) and not fired:
            fired.append(1)
            raise exc
    return w


def rehash(bk):
    """Rewrite MANIFEST.sha256 so it matches the files as they are now."""
    lines = []
    for n in sorted(os.listdir(bk)):
        if n.endswith(".bin") or n == "manifest.json":
            with open(os.path.join(bk, n), "rb") as fh:
                lines.append("%s  %s" % (hashlib.sha256(fh.read()).hexdigest(), n))
    with open(os.path.join(bk, "MANIFEST.sha256"), "w") as fh:
        fh.write("\n".join(lines) + "\n")


def edit_manifest(bk, mutate, fix_hashes=True):
    """Apply mutate to the decoded manifest.json (it may return a replacement dict or list), write it
    back, and by default make MANIFEST.sha256 agree, so only the check under test can object."""
    mp = os.path.join(bk, "manifest.json")
    with open(mp, "r", encoding="utf-8") as fh:
        m = json.load(fh)
    new = mutate(m)
    with open(mp, "w", encoding="utf-8") as fh:
        json.dump(new if isinstance(new, (dict, list)) else m, fh, indent=2)
    if fix_hashes:
        rehash(bk)


class TestInterruptsAndSignals(Base):
    """F1, F2, F9: nothing may split Setup from its checksum between two writes."""

    def newest_backup(self):
        return os.path.join(self.bdir, sorted(os.listdir(self.bdir))[-1])

    def restore_argv(self, bk):
        return [bk, "--efivars", self.box.ef, "--dmi", self.box.dmi, "--apply", "--i-understand-the-risk"]

    def applied(self):
        before = self.box.snapshot()
        code, out = call(au.main, self.apply_args())
        self.assertEqual(code, 0, out)
        return before, self.newest_backup()

    def test_ignoresignals_ignores_inside_and_puts_the_old_handlers_back(self):
        before = handlers_now()
        with uv.IgnoreSignals():
            self.assertEqual(set(handlers_now().values()), {signal.SIG_IGN})
            with uv.IgnoreSignals():                      # re-entrant
                self.assertEqual(set(handlers_now().values()), {signal.SIG_IGN})
            self.assertEqual(set(handlers_now().values()), {signal.SIG_IGN})
        self.assertEqual(handlers_now(), before)

    def test_keyboard_interrupt_between_setup_and_checksum_takes_the_stop_path(self):
        before = self.box.snapshot()
        token = self.token()
        handlers, seen = handlers_now(), []
        with mock.patch.object(uv, "raw_write", interrupt_after("Setup-", KeyboardInterrupt(), seen)):
            code, out = call_checked(self, au.main, self.apply_args(token))
        bk = self.newest_backup()
        self.assertEqual(code, 3, out)
        self.assertIn("STOP: writing %s failed" % SETUP, out)
        self.assertIn("KeyboardInterrupt", out)
        # the pair is reported, from the live variables
        self.assertRegex(out, r"Setup\s+write failed or was interrupted; the live value differs from the backup")
        self.assertRegex(out, r"LastCheckSum\s+not attempted; the live value equals the backup")
        self.assertIn("Setup and LastCheckSum do not agree", out)
        self.assertIn("Error 0162", out)
        # the rollback ran and the machine is back as it was
        self.assertIn("RESTORED and verified by read-back", out)
        self.assertEqual(self.box.snapshot(), before)
        self.assertEqual(read_json(os.path.join(bk, "report.json"))["status"], "stopped-and-restored")
        # the exact restore command is printed (F9)
        self.assertIn(uv.restore_command(bk, self.box.ef, self.box.dmi), out)
        self.assertIn(os.path.join(HERE, "restore.py"), out)
        # signals were ignored during every write and are back afterwards
        self.assertTrue(seen)
        for h in seen:
            self.assertEqual(set(h.values()), {signal.SIG_IGN})
        self.assertEqual(handlers_now(), handlers)

    def test_sigterm_and_sighup_are_ignored_while_the_pair_is_written(self):
        token = self.token()
        seen = []
        with mock.patch.object(uv, "raw_write", interrupt_after("", None, seen)):
            code, out = call(au.main, self.apply_args(token))
        self.assertEqual(code, 0, out)
        self.assertEqual(len(seen), 3)                        # Setup, checksum, PCI_COMMON
        for h in seen:
            self.assertEqual(h, {n: signal.SIG_IGN for n in SIGNAL_NAMES})

    def test_print_raising_broken_pipe_cannot_split_the_pair_during_a_restore(self):
        before, bk = self.applied()
        orig, patches = uv.raw_write, []

        def break_the_pipe_after_setup(path, blob):
            orig(path, blob)
            if os.path.basename(path).startswith("Setup-") and not patches:
                p = mock.patch.object(builtins, "print", side_effect=BrokenPipeError(errno.EPIPE, "Broken pipe"))
                p.start()
                patches.append(p)
        try:
            with mock.patch.object(uv, "raw_write", break_the_pipe_after_setup):
                code, _ = call_checked(self, rs.main, self.restore_argv(bk))
        finally:
            for p in patches:
                p.stop()
        # Output was lost, the work was not: Setup and LastCheckSum are both back.
        self.assertEqual(code, 0)
        self.assertEqual(self.box.snapshot(), before)

    def test_a_real_closed_pipe_neither_aborts_the_run_nor_changes_its_exit_status(self):
        before = self.box.snapshot()
        token = self.token()
        argv = [sys.executable, "-I", os.path.join(HERE, "above4g_unlock.py")] + self.apply_args(token)
        proc = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True)
        proc.stdout.readline()
        proc.stdout.close()                        # the reader goes away, like an SSH session that drops
        err = proc.stderr.read()
        proc.stderr.close()
        self.assertEqual(proc.wait(timeout=120), 0, err)
        self.assertEqual(err, "")
        after = self.box.snapshot()
        self.assertEqual({k for k in after if after[k] != before[k]}, {SETUP, LCS, PCI})
        self.assertEqual(int.from_bytes(after[LCS][4:], "little"), (uv.sum16(after[SETUP][4:]) + 1) & 0xFFFF)

    def test_print_raising_broken_pipe_cannot_end_an_unlock_half_way(self):
        before = self.box.snapshot()
        token = self.token()
        orig, patches = uv.raw_write, []

        def break_the_pipe_after_setup(path, blob):
            orig(path, blob)
            if os.path.basename(path).startswith("Setup-") and not patches:
                p = mock.patch.object(builtins, "print", side_effect=BrokenPipeError(errno.EPIPE, "Broken pipe"))
                p.start()
                patches.append(p)
        try:
            with mock.patch.object(uv, "raw_write", break_the_pipe_after_setup):
                code, _ = call_checked(self, au.main, self.apply_args(token))
        finally:
            for p in patches:
                p.stop()
        after = self.box.snapshot()
        self.assertEqual(code, 0)
        self.assertEqual({k for k in after if after[k] != before[k]}, {SETUP, LCS, PCI})
        self.assertEqual(int.from_bytes(after[LCS][4:], "little"), (uv.sum16(after[SETUP][4:]) + 1) & 0xFFFF)
        self.assertEqual(read_json(os.path.join(self.newest_backup(), "report.json"))["status"], "applied")

    def events(self, fn):
        """Run fn, return its result and the ordered events: 'print' and 'write:<name>'."""
        events, real_print, orig = [], builtins.print, uv.raw_write

        def pr(*args, **kw):
            events.append("print")
            return real_print(*args, **kw)

        def wr(path, blob):
            events.append("write:" + os.path.basename(path).split("-")[0])
            return orig(path, blob)
        with mock.patch.object(builtins, "print", pr), mock.patch.object(uv, "raw_write", wr):
            return fn(), events

    def assert_quiet_between_writes(self, events, writes):
        idx = [i for i, e in enumerate(events) if e.startswith("write:")]
        self.assertEqual([events[i] for i in idx], writes)
        self.assertNotIn("print", events[idx[0]:idx[-1] + 1], events)

    def test_restore_prints_nothing_between_its_writes(self):
        before, bk = self.applied()
        (code, out), events = self.events(lambda: call(rs.main, self.restore_argv(bk)))
        self.assertEqual(code, 0, out)
        self.assert_quiet_between_writes(events, ["write:Setup", "write:LastCheckSum", "write:PCI_COMMON"])
        self.assertIn("wrote %s" % SETUP, out)               # still reported, after the writes
        self.assertEqual(self.box.snapshot(), before)

    def test_unlock_prints_nothing_between_its_writes(self):
        token = self.token()
        (code, out), events = self.events(lambda: call(au.main, self.apply_args(token)))
        self.assertEqual(code, 0, out)
        self.assert_quiet_between_writes(events, ["write:Setup", "write:LastCheckSum", "write:PCI_COMMON"])

    def test_interrupt_inside_a_restore_says_do_not_reboot_and_the_printed_command_repairs_it(self):
        before, bk = self.applied()
        handlers, seen = handlers_now(), []
        with mock.patch.object(uv, "raw_write", interrupt_after("Setup-", KeyboardInterrupt(), seen)):
            code, out = call_checked(self, rs.main, self.restore_argv(bk))
        self.assertEqual(code, 3, out)
        self.assertIn("STOP: restoring %s failed" % SETUP, out)
        self.assertRegex(out, r"LastCheckSum-\S+\s+not attempted")
        self.assertIn("DO NOT REBOOT", out)
        cmd = uv.restore_command(bk, self.box.ef, self.box.dmi)
        self.assertEqual(out.count(cmd), 1, out)
        for h in seen:
            self.assertEqual(set(h.values()), {signal.SIG_IGN})
        self.assertEqual(handlers_now(), handlers)
        # Setup went back, its checksum did not: the state the warning is about ...
        self.assertNotEqual(self.box.raw(LCS), before[LCS])
        # ... and the command that was printed fixes it.
        code, out = call(rs.main, self.restore_argv(bk))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.box.snapshot(), before)

    def test_interrupt_inside_the_write_subcommand_is_reported_and_restored(self):
        path = self.backup()
        before = self.box.snapshot()
        handlers, seen = handlers_now(), []
        with mock.patch.object(uv, "raw_write", interrupt_after("Setup-", KeyboardInterrupt(), seen)):
            code, out = call_checked(self, uv.main, self.apply_argv(path))
        self.assertEqual(code, 3, out)
        self.assertIn("STOP: the write failed: KeyboardInterrupt", out)
        self.assertIn("RESTORED and verified by read-back", out)
        self.assertEqual(self.box.snapshot(), before)
        self.assertIn(uv.restore_command(path, self.box.ef, self.box.dmi), out)
        for h in seen:
            self.assertEqual(set(h.values()), {signal.SIG_IGN})
        self.assertEqual(handlers_now(), handlers)


class TestRollbackPolicy(Base):
    """F4: a rollback is two more writes. Do it only when Setup and its checksum disagree."""

    def run_unlock(self, failing):
        """Apply with raw_write failing for the file whose name starts with `failing`;
        returns (exit code, output, [names written or attempted, in order], snapshot before)."""
        before = self.box.snapshot()
        token = self.token()
        orig, names = uv.raw_write, []

        def f(path, blob):
            base = os.path.basename(path)
            names.append(base.split("-")[0])
            if failing(base, names):
                raise OSError(errno.EACCES, "denied")
            return orig(path, blob)
        with mock.patch.object(uv, "raw_write", f):
            code, out = call(au.main, self.apply_args(token))
        return code, out, names, before

    def test_pci_common_failure_leaves_the_consistent_new_pair_alone(self):
        code, out, names, before = self.run_unlock(lambda base, names: base.startswith("PCI_COMMON"))
        after = self.box.snapshot()
        self.assertEqual(code, 3, out)
        self.assertEqual(names, ["Setup", "LastCheckSum", "PCI_COMMON"])      # no rewrite of Setup or the checksum
        self.assertNotEqual(after[SETUP], before[SETUP])
        self.assertEqual(after[PCI], before[PCI])
        self.assertEqual(int.from_bytes(after[LCS][4:], "little"), (uv.sum16(after[SETUP][4:]) + 1) & 0xFFFF)
        self.assertIn("STOP: writing %s failed" % PCI, out)
        self.assertIn("EACCES", out)
        self.assertIn("security violation", out)
        self.assertIn("consistent pair", out)
        self.assertIn("will not raise Error 0162", out)
        self.assertIn("No automatic rollback", out)
        self.assertNotIn("DO NOT REBOOT", out)
        bk = os.path.join(self.bdir, os.listdir(self.bdir)[0])
        self.assertIn(uv.restore_command(bk, self.box.ef, self.box.dmi), out)
        self.assertEqual(read_json(os.path.join(bk, "report.json"))["status"], "stopped-not-rolled-back")
        # the printed command does put everything back
        code, out = call(rs.main, [bk, "--efivars", self.box.ef, "--dmi", self.box.dmi, "--apply",
                                   "--i-understand-the-risk"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.box.snapshot(), before)

    def test_pci_common_only_run_that_fails_says_setup_and_checksum_were_not_written(self):
        before = self.box.snapshot()
        token = "%s:0x3" % BIOS                                   # --only pci_common: the offset of that field alone
        orig, names = uv.raw_write, []

        def f(path, blob):
            names.append(os.path.basename(path).split("-")[0])
            raise OSError(errno.EACCES, "denied")
        with mock.patch.object(uv, "raw_write", f):
            code, out = call(au.main, self.unlock_args("--apply", "--i-understand-the-risk", "--backup-dir", self.bdir,
                                                      "--only", "pci_common", "--confirm", token))
        self.assertEqual(code, 3, out)
        self.assertEqual(names, ["PCI_COMMON"])                   # no write of Setup or the checksum, no rollback
        self.assertEqual(self.box.snapshot(), before)
        self.assertIn("This run does not write Setup or its checksum", out)
        self.assertNotIn("no checksum guard is declared", out)
        self.assertIn("No automatic rollback", out)
        bk = os.path.join(self.bdir, os.listdir(self.bdir)[0])
        self.assertIn(uv.restore_command(bk, self.box.ef, self.box.dmi), out)

    def test_checksum_failure_prints_the_state_and_rolls_back(self):
        code, out, names, before = self.run_unlock(lambda base, names: base.startswith("LastCheckSum"))
        self.assertEqual(code, 3, out)
        self.assertEqual(names, ["Setup", "LastCheckSum", "Setup"])           # Setup, failed checksum, rollback
        self.assertIn("do not agree", out)
        self.assertRegex(out, r"Setup\s+write completed; the live value differs from the backup")
        self.assertRegex(out, r"LastCheckSum\s+write failed or was interrupted; the live value equals the backup")
        self.assertIn("RESTORED and verified by read-back", out)
        self.assertEqual(self.box.snapshot(), before)

    def short_setup_write(self, then_fail_rollback):
        """Setup is left truncated to 10 bytes by a short write. Returns (code, output, snapshot before)."""
        before = self.box.snapshot()
        token = self.token()
        orig, calls = uv.raw_write, []

        def f(path, blob):
            if os.path.basename(path).startswith("Setup-"):
                calls.append(1)
                if len(calls) == 1:
                    with open(path, "wb") as fh:
                        fh.write(blob[:10])
                    raise uv.ShortWrite("short write on %s: 10 of %d bytes" % (path, len(blob)))
                if then_fail_rollback:
                    raise OSError(errno.EIO, "io")
            return orig(path, blob)
        with mock.patch.object(uv, "raw_write", f):
            code, out = call(au.main, self.apply_args(token))
        return code, out, before

    def test_a_truncated_setup_is_still_repaired_by_the_rollback(self):
        code, out, before = self.short_setup_write(False)
        self.assertEqual(code, 3, out)
        self.assertIn("short write", out)
        self.assertIn("RESTORED and verified by read-back", out)
        self.assertEqual(self.box.snapshot(), before)

    def test_the_printed_command_allows_a_length_change_when_setup_was_left_truncated(self):
        code, out, before = self.short_setup_write(True)
        self.assertEqual(code, 3, out)
        self.assertIn("DO NOT REBOOT", out)
        bk = os.path.join(self.bdir, os.listdir(self.bdir)[0])
        self.assertEqual(out.count(uv.restore_command(bk, self.box.ef, self.box.dmi, True)), 1, out)
        self.assertEqual(len(self.box.raw(SETUP)), 10)              # attribute prefix + 6 data bytes
        # and that command works
        code, out = call(rs.main, [bk, "--efivars", self.box.ef, "--dmi", self.box.dmi, "--apply",
                                   "--i-understand-the-risk", "--allow-length-change"])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.box.snapshot(), before)

    def test_a_rollback_that_fails_says_do_not_reboot_and_prints_the_command_once(self):
        def f(base, names):
            return base.startswith("LastCheckSum") or (base.startswith("Setup-") and names.count("Setup") == 2)
        code, out, names, before = self.run_unlock(f)
        self.assertEqual(code, 3, out)
        self.assertIn("The automatic restore did not complete", out)
        self.assertIn("DO NOT REBOOT", out)
        bk = os.path.join(self.bdir, os.listdir(self.bdir)[0])
        self.assertEqual(out.count(uv.restore_command(bk, self.box.ef, self.box.dmi)), 1, out)
        self.assertEqual(read_json(os.path.join(bk, "report.json"))["status"], "stopped-RESTORE-INCOMPLETE")
        # Setup is new and the checksum is old: the state the message warns about
        self.assertNotEqual(self.box.raw(SETUP), before[SETUP])
        self.assertEqual(self.box.raw(LCS), before[LCS])


class TestBackupSafety(Base):
    """F3, F7, F8: a backup must be trustworthy before it is used, and durable when it is made."""

    def changed_setup(self):
        data = bytearray(self.box.data(SETUP))
        data[300] ^= 0xFF
        self.box.put(SETUP, 7, bytes(data))

    def restore(self, bk, *more, **kw):
        dmi = kw.get("dmi", self.box.dmi)
        return call(rs.main, [bk, "--efivars", self.box.ef, "--dmi", dmi, "--apply", "--i-understand-the-risk"]
                    + list(more))

    # ---- F3: tie the backup to this machine and this layout
    def test_restore_refuses_a_live_variable_of_another_length(self):
        bk = self.backup()
        original = self.box.snapshot()
        self.box.put(SETUP, 7, self.box.data(SETUP)[:4999])
        snap = self.box.snapshot()
        for apply in (False, True):
            argv = [bk, "--efivars", self.box.ef, "--dmi", self.box.dmi] + (
                ["--apply", "--i-understand-the-risk"] if apply else [])
            code, out = call(rs.main, argv)
            self.assertEqual(code, 2, out)
            self.assertIn("4999 data bytes, the backup 5000", out)
            self.assertEqual(self.box.snapshot(), snap)
        # --allow-length-change skips that one check and nothing else
        code, out = self.restore(bk, "--allow-length-change")
        self.assertEqual(code, 0, out)
        self.assertEqual(self.box.snapshot(), original)
        self.box.put(SETUP, 7, self.box.data(SETUP)[:4999])
        with open(os.path.join(self.box.dmi, "bios_version"), "w") as fh:
            fh.write("OTHER1\n")
        code, out = self.restore(bk, "--allow-length-change")
        self.assertEqual(code, 2, out)
        self.assertIn("different build", out)

    def test_restore_refuses_a_backup_that_records_no_bios_version(self):
        blank = os.path.join(self.tmp, "dmi-blank")
        os.makedirs(blank)
        with open(os.path.join(blank, "board_name"), "w") as fh:         # a machine name, but no BIOS version
            fh.write("TESTBOARD\n")
        code, out = call(uv.main, ["backup", SETUP, LCS, PCI, "--dir", self.bdir, "--efivars", self.box.ef,
                                   "--dmi", blank])
        self.assertEqual(code, 0, out)
        self.assertIn("warning: no BIOS version could be read", out)
        bk = os.path.join(self.bdir, os.listdir(self.bdir)[0])
        self.assertIsNone(uv.BackupSet.load(bk).bios_version)
        self.changed_setup()
        snap = self.box.snapshot()
        code, out = self.restore(bk)
        self.assertEqual(code, 2, out)
        self.assertIn("does not record a BIOS version", out)
        self.assertEqual(self.box.snapshot(), snap)

    def test_restore_refuses_when_this_machines_bios_cannot_be_read(self):
        bk = self.backup()
        self.changed_setup()
        snap = self.box.snapshot()
        nodmi = os.path.join(self.tmp, "dmi-unreadable")
        os.makedirs(nodmi)
        code, out = self.restore(bk, dmi=nodmi)
        self.assertEqual(code, 2, out)
        self.assertIn("cannot read the BIOS version", out)
        self.assertEqual(self.box.snapshot(), snap)

    def test_restore_refuses_a_different_board_product_or_vendor(self):
        for key in uv.PLATFORM_KEYS:
            box = Box(tempfile.mkdtemp(dir=self.tmp))
            with open(os.path.join(box.dmi, key), "w") as fh:
                fh.write("ALPHA\n")
            bdir = os.path.join(box.root, "bk")
            code, out = call(uv.main, ["backup", SETUP, LCS, PCI, "--dir", bdir, "--efivars", box.ef, "--dmi", box.dmi])
            self.assertEqual(code, 0, out)
            bk = os.path.join(bdir, os.listdir(bdir)[0])
            self.assertEqual(uv.BackupSet.load(bk).platform[key], "ALPHA")
            data = bytearray(box.data(SETUP))
            data[300] ^= 0xFF
            box.put(SETUP, 7, bytes(data))
            snap = box.snapshot()
            argv = [bk, "--efivars", box.ef, "--dmi", box.dmi, "--apply", "--i-understand-the-risk"]
            with open(os.path.join(box.dmi, key), "w") as fh:
                fh.write("BRAVO\n")
            code, out = call(rs.main, argv)
            self.assertEqual(code, 2, (key, out))
            self.assertIn("%s 'ALPHA' but this machine reports 'BRAVO'" % key, out)
            os.remove(os.path.join(box.dmi, key))                        # unreadable now: also a refusal
            code, out = call(rs.main, argv)
            self.assertEqual(code, 2, (key, out))
            self.assertEqual(box.snapshot(), snap)
            with open(os.path.join(box.dmi, key), "w") as fh:
                fh.write("ALPHA\n")
            code, out = call(rs.main, argv)
            self.assertEqual(code, 0, (key, out))

    # ---- F7: the manifest is verified, not trusted
    def test_manifest_json_is_listed_in_manifest_sha256(self):
        bk = self.backup()
        with open(os.path.join(bk, "MANIFEST.sha256")) as fh:
            listed = dict((name, digest) for digest, name in (line.split("  ", 1) for line in fh.read().splitlines()))
        with open(os.path.join(bk, "manifest.json"), "rb") as fh:
            self.assertEqual(listed["manifest.json"], hashlib.sha256(fh.read()).hexdigest())
        self.assertEqual(set(listed), {SETUP + ".bin", LCS + ".bin", PCI + ".bin", "manifest.json"})

    def test_tampered_manifest_json_is_refused(self):
        bk = self.backup()
        edit_manifest(bk, lambda m: m["variables"][1].__setitem__("restore", False), fix_hashes=False)
        with self.assertRaises(uv.VarError) as ctx:
            uv.BackupSet.load(bk)
        self.assertIn("manifest.json: sha256 differs from MANIFEST.sha256", str(ctx.exception))
        self.changed_setup()
        snap = self.box.snapshot()
        code, out = self.restore(bk)
        self.assertEqual(code, 2, out)
        self.assertEqual(self.box.snapshot(), snap)

    def test_swapped_file_mapping_is_refused(self):
        bk = self.backup()

        def swap(m):                        # Setup's entry now describes the checksum's blob, completely
            a, b = m["variables"][0], m["variables"][1]
            for key in ("file", "sha256", "data_length", "attrs"):
                a[key], b[key] = b[key], a[key]
        edit_manifest(bk, swap)
        with self.assertRaises(uv.VarError) as ctx:
            uv.BackupSet.load(bk)
        self.assertIn("swapped mapping", str(ctx.exception))
        self.changed_setup()
        snap = self.box.snapshot()
        code, out = self.restore(bk)
        self.assertEqual(code, 2, out)
        self.assertEqual(self.box.snapshot(), snap)

    def test_missing_or_incomplete_hash_file_is_refused(self):
        bk = self.backup()
        hp = os.path.join(bk, "MANIFEST.sha256")
        with open(hp) as fh:
            lines = fh.read().splitlines()

        def without(needle):
            return [l for l in lines if needle not in l]
        for body in (None, without("manifest.json"), without(".bin")[:1], ["not a hash line"], lines + lines[:1]):
            if body is None:
                os.remove(hp)
            else:
                with open(hp, "w") as fh:
                    fh.write("\n".join(body) + "\n")
            with self.assertRaises(uv.VarError):
                uv.BackupSet.load(bk)
            code, out = self.restore(bk)
            self.assertEqual(code, 2, out)

    def test_manifest_sha256_must_agree_with_every_blob_file(self):
        bk = self.backup()
        hp = os.path.join(bk, "MANIFEST.sha256")
        with open(hp) as fh:
            lines = fh.read().splitlines()
        wrong = ["%s  %s" % ("0" * 64, l.split("  ", 1)[1]) if SETUP in l else l for l in lines]
        with open(hp, "w") as fh:
            fh.write("\n".join(wrong) + "\n")
        with self.assertRaises(uv.VarError) as ctx:     # blob and manifest.json still agree with each other
            uv.BackupSet.load(bk)
        self.assertIn("sha256 differs from MANIFEST.sha256", str(ctx.exception))

    def test_restore_key_must_be_present_and_a_boolean(self):
        mutations = (lambda m: m["variables"][1].pop("restore"),
                     lambda m: m["variables"][1].__setitem__("restore", "yes"),
                     lambda m: m["variables"][1].__setitem__("restore", 1))
        for i, mutate in enumerate(mutations):
            self.bdir = os.path.join(self.tmp, "restore-key-%d" % i)
            bk = self.backup()
            edit_manifest(bk, mutate)
            with self.assertRaises(uv.VarError) as ctx:
                uv.BackupSet.load(bk)
            self.assertIn("'restore' must be true or false", str(ctx.exception))
        self.bdir = os.path.join(self.tmp, "restore-key-false")
        bk = self.backup()
        edit_manifest(bk, lambda m: m["variables"][1].__setitem__("restore", False))
        self.assertEqual([e.restore for e in uv.BackupSet.load(bk).entries], [True, False, True])

    def test_malformed_manifests_are_refusals_never_tracebacks(self):
        def first(key, value):
            return lambda m: m["variables"][0].__setitem__(key, value)
        cases = [
            lambda m: [],
            lambda m: {"format": 1, "variables": "none"},
            lambda m: m["variables"].__setitem__(0, "Setup"),
            lambda m: m["variables"][0].pop("name"),
            first("name", 5),
            first("guid", "zz"),
            first("data_length", "5000"),
            first("data_length", True),
            first("attrs", None),
            first("sha256", 5),
            first("file", "../" + SETUP + ".bin"),
            first("file", None),
            lambda m: m.__setitem__("bios_version", 7),
            lambda m: m.__setitem__("board_name", ["x"]),
            lambda m: m["variables"].append(dict(m["variables"][0])),
            lambda m: m.__setitem__("variables", []),
        ]
        for i, mutate in enumerate(cases):
            self.bdir = os.path.join(self.tmp, "malformed-%d" % i)
            bk = self.backup()
            edit_manifest(bk, mutate)
            with self.assertRaises(uv.VarError, msg="case %d" % i):
                uv.BackupSet.load(bk)
            code, out = self.restore(bk)
            self.assertEqual(code, 2, (i, out))
            self.assertIn("REFUSE", out)

    def test_blob_length_must_match_the_recorded_length(self):
        bk = self.backup()
        blob = os.path.join(bk, SETUP + ".bin")
        with open(blob, "ab") as fh:
            fh.write(b"\x00")
        with open(blob, "rb") as fh:
            digest = hashlib.sha256(fh.read()).hexdigest()
        # every hash agrees; only the recorded length is wrong
        edit_manifest(bk, lambda m: m["variables"][0].__setitem__("sha256", digest))
        with self.assertRaises(uv.VarError) as ctx:
            uv.BackupSet.load(bk)
        self.assertIn("length differs from the manifest", str(ctx.exception))

    # ---- F8: a backup is on disk before anything is written
    def test_fsync_failure_refuses_the_backup(self):
        argv = ["backup", SETUP, "--dir", self.bdir, "--efivars", self.box.ef, "--dmi", self.box.dmi]
        with mock.patch.object(os, "fsync", side_effect=OSError(errno.EIO, "io error")):
            code, out = call(uv.main, argv)
        self.assertEqual(code, 2, out)
        self.assertIn("fsync failed", out)

    def test_fsync_failure_stops_unlock_before_any_write(self):
        before = self.box.snapshot()
        token = self.token()
        with mock.patch.object(os, "fsync", side_effect=OSError(errno.EIO, "io error")):
            code, out = call(au.main, self.apply_args(token))
        self.assertEqual(code, 2, out)
        self.assertIn("REFUSE: backup failed", out)
        self.assertIn("fsync failed", out)
        self.assertEqual(self.box.snapshot(), before)

    def test_backup_directory_and_its_parent_are_fsynced(self):
        with mock.patch.object(uv, "_fsync_dir") as fsync_dir:
            bk = self.backup()
        called = [c[0][0] for c in fsync_dir.call_args_list]
        self.assertIn(bk, called)
        self.assertIn(self.bdir, called)

    def test_directory_fsync_failure_is_a_refusal(self):
        # Pretend to be POSIX so the branch runs on any host; the file descriptor is a stand-in.
        with mock.patch.object(os, "name", "posix"), mock.patch.object(os, "open", return_value=99) as opened, \
                mock.patch.object(os, "close") as closed, \
                mock.patch.object(os, "fsync", side_effect=OSError(errno.EIO, "io error")):
            with self.assertRaises(uv.VarError) as ctx:
                uv._fsync_dir(self.tmp)
        self.assertIn("fsync failed on directory", str(ctx.exception))
        self.assertEqual(opened.call_args[0][0], self.tmp)
        closed.assert_called_once_with(99)

    def tmpfs_table(self, path, fstype="tmpfs"):
        real = os.path.realpath(path).replace("\\", "\\134").replace(" ", "\\040")
        return ["/dev/root / ext4 rw 0 0", "%s %s %s rw,nosuid 0 0" % (fstype, real, fstype)]

    def test_backup_on_tmpfs_or_ramfs_is_refused_unless_allowed(self):
        argv = ["backup", SETUP, "--dir", self.bdir, "--efivars", self.box.ef, "--dmi", self.box.dmi]
        for fstype in ("tmpfs", "ramfs"):
            with mock.patch.object(uv, "_proc_mounts_lines", return_value=self.tmpfs_table(self.tmp, fstype)):
                code, out = call(uv.main, argv)
                self.assertEqual(code, 2, out)
                self.assertIn(fstype, out)
                self.assertIn("--allow-volatile-backup", out)
                self.assertFalse(os.path.exists(self.bdir))
                code, out = call(uv.main, argv + ["--allow-volatile-backup"])
                self.assertEqual(code, 0, out)
            shutil.rmtree(self.bdir)

    def test_unlock_refuses_a_backup_directory_on_tmpfs_before_writing(self):
        before = self.box.snapshot()
        token = self.token()
        with mock.patch.object(uv, "_proc_mounts_lines", return_value=self.tmpfs_table(self.tmp)):
            code, out = call(au.main, self.apply_args(token))
            self.assertEqual(code, 2, out)
            self.assertIn("REFUSE: backup failed", out)
            self.assertEqual(self.box.snapshot(), before)
            code, out = call(au.main, self.apply_args(token, "--allow-volatile-backup"))
            self.assertEqual(code, 0, out)

    def test_volatile_check_uses_the_longest_mount_point_and_skips_without_proc_mounts(self):
        table = ["tmpfs /tmp tmpfs rw 0 0", "/dev/sda2 /tmp/disk ext4 rw 0 0", "/dev/sda1 / ext4 rw 0 0"]
        with mock.patch("os.path.realpath", side_effect=lambda p: p):
            with mock.patch.object(uv, "_proc_mounts_lines", return_value=table):
                self.assertEqual(uv.volatile_backup_fs("/tmp/x/backups"), "tmpfs")
                self.assertIsNone(uv.volatile_backup_fs("/tmp/disk/backups"))
                self.assertIsNone(uv.volatile_backup_fs("/var/backups"))
                self.assertIsNone(uv.volatile_backup_fs("/tmpx/backups"))
            with mock.patch.object(uv, "_proc_mounts_lines", return_value=None):
                self.assertIsNone(uv.volatile_backup_fs("/tmp/x/backups"))


class TestWriteSubcommandGuards(Base):
    """F5, F6: `write` is the sharpest tool in the box."""

    def argv(self, var, offset, value, path, *more):
        return (["write", var, "--offset", offset, "--value", value, "--backup", path, "--dmi", self.box.dmi]
                + self.common() + list(more))

    def test_apply_needs_all_three_expectations(self):
        path = self.backup()
        before = self.box.snapshot()
        for flag in ("--expect-old", "--expect-var-len", "--expect-attrs"):
            argv = self.apply_argv(path)
            i = argv.index(flag)
            del argv[i:i + 2]
            code, out = call(uv.main, argv)
            self.assertEqual(code, 2, (flag, out))
            self.assertIn("%s is required with --apply" % flag, out)
            self.assertEqual(self.box.snapshot(), before)

    def test_setup_and_checksum_variables_need_no_checksum_companion(self):
        path = self.backup()
        before = self.box.snapshot()
        cases = [(SETUP, "0xa49", "1"), (LCS, "0", "5")]
        for var, off, val in cases:
            for extra in ([], ["--expect-old", "0", "--expect-var-len", "5000", "--expect-attrs", "7",
                               "--apply", "--i-understand-the-risk"]):
                code, out = call(uv.main, self.argv(var, off, val, path, *extra))
                self.assertEqual(code, 2, (var, extra, out))
                self.assertIn("--no-checksum-companion", out)
                self.assertIn("above4g_unlock.py", out)
                self.assertEqual(self.box.snapshot(), before)

    def test_a_variable_the_checksum_does_not_cover_writes_without_the_flag(self):
        path = self.backup()
        before = self.box.snapshot()
        code, out = call(uv.main, self.argv(PCI, "3", "1", path, "--expect-old", "0", "--expect-var-len", "7",
                                            "--expect-attrs", "7", "--apply", "--i-understand-the-risk"))
        self.assertEqual(code, 0, out)
        after = self.box.snapshot()
        self.assertEqual({k for k in after if after[k] != before[k]}, {PCI})
        self.assertEqual([i for i in range(len(before[PCI])) if before[PCI][i] != after[PCI][i]], [4 + 3])

    def test_write_refuses_a_backup_made_on_another_bios_or_without_a_readable_one(self):
        path = self.backup()
        before = self.box.snapshot()
        with open(os.path.join(self.box.dmi, "bios_version"), "w") as fh:
            fh.write("OTHER1\n")
        code, out = call(uv.main, self.apply_argv(path))
        self.assertEqual(code, 2, out)
        self.assertIn("different build", out)
        os.remove(os.path.join(self.box.dmi, "bios_version"))
        code, out = call(uv.main, self.apply_argv(path))
        self.assertEqual(code, 2, out)
        self.assertIn("cannot read the BIOS version", out)
        self.assertEqual(self.box.snapshot(), before)

    def test_a_reference_only_backup_entry_is_not_a_restore_point(self):
        name = "SysEventLogCount-" + OTHER_GUID
        path = self.backup(["--reference-only", name])
        before = self.box.snapshot()
        argv = self.argv(name, "0", "7", path, "--expect-old", "6", "--expect-var-len", "1", "--expect-attrs", "7")
        for extra in ([], ["--apply", "--i-understand-the-risk"]):
            code, out = call(uv.main, argv + extra)
            self.assertEqual(code, 2, out)
            self.assertIn("for reference only", out)
            self.assertEqual(self.box.snapshot(), before)

    def test_a_failed_write_restores_only_the_variable_that_was_written(self):
        path = self.backup()
        self.box.put(PCI, 7, bytes([0, 0, 1, 1, 0, 0, 0]))         # drifts after the backup; not what is written
        before = self.box.snapshot()
        orig, fired = uv.raw_write, []

        def corrupting(p, blob):
            orig(p, blob)
            if not fired:
                fired.append(1)
                raw = bytearray(open(p, "rb").read())
                raw[4 + 100] ^= 0xFF
                open(p, "wb").write(bytes(raw))
        with mock.patch.object(uv, "raw_write", corrupting):
            code, out = call(uv.main, self.apply_argv(path))
        self.assertEqual(code, 3, out)
        self.assertIn("only this variable", out)
        self.assertEqual(self.box.snapshot(), before)             # Setup back; PCI_COMMON left exactly as it was
        self.assertNotIn(PCI, out)


class TestRestoreCommand(Base):
    def test_restore_command_is_absolute_and_carries_efivars_and_dmi(self):
        cmd = uv.restore_command("rel/backup dir", "rel/ef", "rel/dmi")
        words = shlex.split(cmd)
        self.assertEqual(words[0], "python3")
        self.assertEqual(words[1], os.path.join(HERE, "restore.py"))
        self.assertTrue(os.path.isabs(words[1]))
        self.assertEqual(words[2], os.path.abspath("rel/backup dir"))
        self.assertEqual(words[words.index("--efivars") + 1], os.path.abspath("rel/ef"))
        self.assertEqual(words[words.index("--dmi") + 1], os.path.abspath("rel/dmi"))
        self.assertEqual(words[-2:], ["--apply", "--i-understand-the-risk"])
        self.assertEqual(shlex.split(uv.restore_command("b", "e", "d", True))[-1], "--allow-length-change")


# ---------------------------------------------------------------------------
# Second review round. Each test below fails on the code as it stood before its fix.
# ---------------------------------------------------------------------------

CASPER_LIKE = "overlay %s overlay rw,relatime,lowerdir=/run/rootfsbase,upperdir=/cow/upper,workdir=/cow/work 0 0"


class Round2(Base):
    def newest_backup(self):
        return os.path.join(self.bdir, sorted(os.listdir(self.bdir))[-1])

    def restore_argv(self, bk, *more):
        return [bk, "--efivars", self.box.ef, "--dmi", self.box.dmi, "--apply", "--i-understand-the-risk"] + list(more)

    def partial_backup(self, *names, **kw):
        """A backup of just the named variables, in its own directory."""
        bdir = os.path.join(self.tmp, kw.get("sub", "partial"))
        code, out = call(uv.main, ["backup"] + list(names) + ["--dir", bdir, "--efivars", self.box.ef,
                                                              "--dmi", self.box.dmi])
        self.assertEqual(code, 0, out)
        return os.path.join(bdir, sorted(os.listdir(bdir))[-1]), out

    def stored_minus_sum16(self):
        return (int.from_bytes(self.box.data(LCS), "little") - uv.sum16(self.box.data(SETUP))) & 0xFFFF


# ---- R1: restore must not split Setup from its checksum
class TestR1RestorePairGuard(Round2):
    def test_restore_of_a_setup_only_backup_is_refused_without_the_companion_flag(self):
        bk, _ = self.partial_backup(SETUP)
        code, out = call(au.main, self.apply_args())
        self.assertEqual(code, 0, out)
        unlocked = self.box.snapshot()
        for argv in (self.restore_argv(bk), [bk, "--efivars", self.box.ef, "--dmi", self.box.dmi]):    # apply and dry run
            code, out = call(rs.main, argv)
            self.assertEqual(code, 2, out)
            self.assertIn("without a checksum variable", out)
            self.assertIn("--no-checksum-companion", out)
            self.assertEqual(self.box.snapshot(), unlocked)
            self.assertEqual(self.stored_minus_sum16(), 1)                 # the pair is still consistent

    def test_the_companion_flag_is_the_explicit_escape(self):
        bk, _ = self.partial_backup(SETUP)
        original = self.box.snapshot()
        code, out = call(au.main, self.apply_args())
        self.assertEqual(code, 0, out)
        code, out = call(rs.main, self.restore_argv(bk, "--no-checksum-companion"))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.box.raw(SETUP), original[SETUP])

    def test_restore_of_a_checksum_only_backup_is_refused(self):
        bk, _ = self.partial_backup(LCS)
        code, out = call(au.main, self.apply_args())
        self.assertEqual(code, 0, out)
        unlocked = self.box.snapshot()
        code, out = call(rs.main, self.restore_argv(bk))
        self.assertEqual(code, 2, out)
        self.assertIn("without a Setup variable", out)
        self.assertEqual(self.box.snapshot(), unlocked)

    def test_a_full_backup_restores_without_the_flag(self):
        before = self.box.snapshot()
        code, out = call(au.main, self.apply_args())
        self.assertEqual(code, 0, out)
        code, out = call(rs.main, self.restore_argv(self.newest_backup()))
        self.assertEqual(code, 0, out)
        self.assertEqual(self.box.snapshot(), before)

    def test_automatic_rollback_is_not_blocked_and_the_printed_command_runs(self):
        path, _ = self.partial_backup(SETUP)
        before = self.box.snapshot()
        orig, fired = uv.raw_write, []

        def corrupting(p, blob):
            orig(p, blob)
            if not fired:
                fired.append(1)
                raw = bytearray(open(p, "rb").read())
                raw[4 + 100] ^= 0xFF
                open(p, "wb").write(bytes(raw))
        with mock.patch.object(uv, "raw_write", corrupting):
            code, out = call(uv.main, self.apply_argv(path))
        self.assertEqual(code, 3, out)
        self.assertIn("RESTORED and verified by read-back", out)           # the only=[...] path has no companion check
        self.assertEqual(self.box.snapshot(), before)
        cmd = uv.restore_command(path, self.box.ef, self.box.dmi, no_checksum_companion=True)
        self.assertIn(cmd, out)
        self.assertIn("--no-checksum-companion", cmd)
        code, out = call(rs.main, shlex.split(cmd)[2:])                    # and the printed command itself runs
        self.assertEqual(code, 0, out)

    def test_restore_command_adds_the_flag_only_when_asked(self):
        plain = shlex.split(uv.restore_command("b", "e", "d"))
        flagged = shlex.split(uv.restore_command("b", "e", "d", no_checksum_companion=True))
        self.assertNotIn("--no-checksum-companion", plain)
        self.assertEqual(flagged[-1], "--no-checksum-companion")

    def test_backup_warns_when_setup_is_listed_without_a_checksum_variable(self):
        _, out = self.partial_backup(SETUP, sub="p1")
        self.assertIn("without a checksum variable", out)
        _, out = self.partial_backup(LCS, sub="p2")
        self.assertIn("without a Setup variable", out)
        _, out = self.partial_backup(SETUP, LCS, PCI, sub="p3")
        self.assertNotIn("without a", out)


# ---- R3: live-system roots are volatile too
class TestR3VolatileRoots(Round2):
    def kind(self, table, path="/mnt/x/backups"):
        with mock.patch("os.path.realpath", side_effect=lambda p: p):
            with mock.patch.object(uv, "_proc_mounts_lines", return_value=table):
                return uv.volatile_backup_fs(path)

    def test_overlay_roots_squashfs_aufs_and_rootfs_are_volatile(self):
        root_on = lambda line: ["tmpfs /cow tmpfs rw 0 0", line]                    # noqa: E731
        ov = "overlay / overlay rw,lowerdir=/run/rootfsbase,upperdir=/cow/upper,workdir=/cow/work 0 0"
        self.assertTrue(self.kind(root_on(ov)))                                         # upper layer on tmpfs
        self.assertTrue(self.kind(["ramfs /cow ramfs rw 0 0", ov]))                     # or on ramfs
        self.assertIsNone(self.kind(["/dev/sda3 /cow ext4 rw 0 0", ov]))                # upper layer on a disk
        self.assertTrue(self.kind(["overlay / overlay rw,lowerdir=/a 0 0"]))             # no upperdir: cannot tell
        self.assertTrue(self.kind(["overlay / overlay rw,upperdir=/cow/upper,workdir=/w 0 0"]))   # upper is on itself
        self.assertTrue(self.kind(["none / aufs rw 0 0"]))
        self.assertTrue(self.kind(["/dev/loop0 / squashfs ro 0 0"]))
        self.assertTrue(self.kind(["rootfs / rootfs rw 0 0"]))
        self.assertIsNone(self.kind(["/dev/sda1 / ext4 rw 0 0"]))

    def test_a_live_usb_root_is_refused_with_the_live_system_advice(self):
        real = os.path.realpath(self.tmp).replace("\\", "\\134").replace(" ", "\\040")
        table = ["tmpfs /cow tmpfs rw 0 0", CASPER_LIKE % real]
        argv = ["backup", SETUP, "--dir", self.bdir, "--efivars", self.box.ef, "--dmi", self.box.dmi]
        with mock.patch.object(uv, "_proc_mounts_lines", return_value=table):
            code, out = call(uv.main, argv)
            self.assertEqual(code, 2, out)
            self.assertIn("live system: put --backup-dir on a USB stick or a disk partition", out)
            self.assertIn("--allow-volatile-backup", out)
            self.assertFalse(os.path.exists(self.bdir))
            code, out = call(uv.main, argv + ["--allow-volatile-backup"])
            self.assertEqual(code, 0, out)

    def test_unlock_refuses_a_live_usb_root_before_writing(self):
        before = self.box.snapshot()
        token = self.token()
        real = os.path.realpath(self.tmp).replace("\\", "\\134").replace(" ", "\\040")
        with mock.patch.object(uv, "_proc_mounts_lines", return_value=["tmpfs /cow tmpfs rw 0 0", CASPER_LIKE % real]):
            code, out = call(au.main, self.apply_args(token))
        self.assertEqual(code, 2, out)
        self.assertIn("live system", out)
        self.assertEqual(self.box.snapshot(), before)


# ---- R4: a durable mark that a write window was open
class TestR4WritingMarker(Round2):
    def test_report_says_writing_before_the_first_write_and_applied_after(self):
        token = self.token()
        events, orig = [], uv.raw_write

        def w(path, blob):
            rp = os.path.join(self.bdir, os.listdir(self.bdir)[0], "report.json")
            events.append((os.path.basename(path).split("-")[0], read_json(rp)["status"]))
            return orig(path, blob)
        with mock.patch.object(uv, "raw_write", w):
            code, out = call(au.main, self.apply_args(token))
        self.assertEqual(code, 0, out)
        self.assertEqual([s for _, s in events], ["writing"] * 3)                  # open for all three writes
        self.assertEqual(read_json(os.path.join(self.newest_backup(), "report.json"))["status"], "applied")

    def test_write_file_atomic_fsyncs_the_file_and_the_directory_and_replaces_whole(self):
        target = os.path.join(self.tmp, "r.json")
        with open(target, "w") as fh:
            fh.write("old")
        with mock.patch.object(os, "fsync", wraps=os.fsync) as fs, mock.patch.object(uv, "_fsync_dir") as fd:
            uv.write_file_atomic(target, b"new")
        self.assertGreaterEqual(fs.call_count, 1)
        fd.assert_called_with(self.tmp)
        with open(target, "rb") as fh:
            self.assertEqual(fh.read(), b"new")
        self.assertEqual(os.listdir(self.tmp).count("r.json.tmp"), 0)

    def test_a_killed_run_leaves_a_writing_report_that_blocks_the_next_run_until_restored(self):
        before = self.box.snapshot()
        token = self.token()
        src = ("import os, sys\n"
               "sys.path.insert(0, %r)\n"
               "import uefivar as uv, above4g_unlock as au\n"
               "orig = uv.raw_write\n"
               "def w(path, blob):\n"
               "    orig(path, blob)\n"
               "    if os.path.basename(path).startswith('Setup-'):\n"
               "        os._exit(9)\n"                                                  # power loss: no cleanup runs
               "uv.raw_write = w\n"
               "sys.exit(au.main(%r))\n" % (HERE, self.apply_args(token)))
        r = subprocess.run([sys.executable, "-I", "-c", src], capture_output=True, text=True)
        self.assertEqual(r.returncode, 9, r.stdout + r.stderr)
        bk = self.newest_backup()
        self.assertNotEqual(self.box.raw(SETUP), before[SETUP])                   # Setup new ...
        self.assertEqual(self.box.raw(LCS), before[LCS])                          # ... checksum old
        self.assertEqual(read_json(os.path.join(bk, "report.json"))["status"], "writing")
        cmd = uv.restore_command(bk, self.box.ef, self.box.dmi)
        # the next unlock sees delta 0 and would say "Nothing to do"; with --backup-dir it must stop instead
        for extra in ([], ["--apply", "--i-understand-the-risk", "--confirm", token]):
            code, out = call(au.main, self.unlock_args("--backup-dir", self.bdir, *extra))
            self.assertEqual(code, 3, out)
            self.assertIn("STOP", out)
            self.assertIn("still says writing", out)
            self.assertIn(cmd, out)
            self.assertNotIn("Nothing to do", out)
        iomem = os.path.join(self.tmp, "iomem")
        with open(iomem, "w") as fh:
            fh.write("00000000-00000fff : Reserved\n")
        code, out = call(au.main, ["verify", "--profile", self.box.profile, "--efivars", self.box.ef, "--dmi",
                                   self.box.dmi, "--report", bk, "--iomem", iomem])
        self.assertEqual(code, 3, out)
        self.assertIn("still says writing", out)
        # the printed command puts it right and clears the flag
        code, out = call(rs.main, shlex.split(cmd)[2:])
        self.assertEqual(code, 0, out)
        self.assertEqual(self.box.snapshot(), before)
        self.assertNotEqual(read_json(os.path.join(bk, "report.json"))["status"], "writing")
        code, out = call(au.main, self.unlock_args("--backup-dir", self.bdir))
        self.assertEqual(code, 0, out)

    def test_without_backup_dir_a_dry_run_does_not_look_for_reports(self):
        code, out = call(au.main, self.unlock_args())
        self.assertEqual(code, 0, out)


# ---- R5: Setup-family names
class TestR5SetupFamily(Round2):
    def test_names_that_contain_setup_or_checksum_are_guarded(self):
        for n in ("Setup", "setup", "SETUPDATA", "SetupData", "AMITSESetup", "SetupVolatile", "SystemSetup",
                  "LastCheckSum", "ChecksumX"):
            self.assertTrue(uv.is_checksum_guarded(n), n)
        for n in ("PCI_COMMON", "SysEventLogCount", "BootOrder"):
            self.assertFalse(uv.is_checksum_guarded(n), n)

    def test_write_to_a_setup_family_variable_needs_the_flag(self):
        name = "SetupData-" + OTHER_GUID
        self.box.put(name, 7, b"\x00\x01\x02\x03")
        path, _ = self.partial_backup(name)
        before = self.box.snapshot()
        code, out = call(uv.main, ["write", name, "--offset", "0", "--value", "9", "--backup", path,
                                   "--dmi", self.box.dmi] + self.common())
        self.assertEqual(code, 2, out)
        self.assertIn("--no-checksum-companion", out)
        self.assertEqual(self.box.snapshot(), before)


# ---- R7: the BIOS string alone does not identify a machine
class TestR7MachineBinding(Round2):
    def strip_names(self, *keep):
        for key in ("board_name", "product_name", "sys_vendor"):
            if key not in keep:
                p = os.path.join(self.box.dmi, key)
                if os.path.exists(p):
                    os.remove(p)

    def test_backup_refuses_when_neither_board_nor_product_name_can_be_read(self):
        self.strip_names()
        code, out = call(uv.main, ["backup", SETUP, LCS, PCI, "--dir", self.bdir, "--efivars", self.box.ef,
                                   "--dmi", self.box.dmi])
        self.assertEqual(code, 2, out)
        self.assertIn("board_name", out)
        self.assertIn("product_name", out)
        self.assertFalse(os.path.exists(self.bdir))

    def test_one_of_the_two_is_enough(self):
        for keep in ("board_name", "product_name"):
            self.strip_names(keep)
            for k in ("board_name", "product_name"):                 # restore the one to keep
                if k == keep:
                    with open(os.path.join(self.box.dmi, k), "w") as fh:
                        fh.write("NAME\n")
            self.bdir = os.path.join(self.tmp, "keep-" + keep)
            bk = self.backup()
            self.assertEqual(list(uv.BackupSet.load(bk).platform), [keep])

    def test_unlock_warns_in_a_dry_run_and_refuses_before_any_write_with_apply(self):
        self.strip_names()
        code, out = call(au.main, self.unlock_args())
        self.assertEqual(code, 0, out)
        self.assertIn("board_name and product_name", out)
        before = self.box.snapshot()
        token = self.token()
        code, out = call(au.main, self.apply_args(token))
        self.assertEqual(code, 2, out)
        self.assertIn("board_name and product_name", out)
        self.assertEqual(self.box.snapshot(), before)
        self.assertFalse(os.path.exists(self.bdir))

    def test_restore_refuses_a_manifest_that_recorded_neither(self):
        bk = self.backup()

        def drop(m):
            m.pop("board_name", None)
            m.pop("product_name", None)
        edit_manifest(bk, drop)
        data = bytearray(self.box.data(SETUP))
        data[300] ^= 0xFF
        self.box.put(SETUP, 7, bytes(data))
        snap = self.box.snapshot()
        code, out = call(rs.main, self.restore_argv(bk))
        self.assertEqual(code, 2, out)
        self.assertIn("records neither a board name nor a product name", out)
        self.assertEqual(self.box.snapshot(), snap)


# ---- R10: wording, and every directory the backup path needed
class TestR10Durability(Round2):
    def test_every_directory_created_for_dir_is_fsynced_with_its_parent(self):
        deep = os.path.join(self.tmp, "a", "b", "backups")
        with mock.patch.object(uv, "_fsync_dir") as fsync_dir:
            code, out = call(uv.main, ["backup", SETUP, "--dir", deep, "--efivars", self.box.ef,
                                       "--dmi", self.box.dmi])
        self.assertEqual(code, 0, out)
        called = set(os.path.abspath(c[0][0]) for c in fsync_dir.call_args_list)
        for p in (self.tmp, os.path.join(self.tmp, "a"), os.path.join(self.tmp, "a", "b"), deep):
            self.assertIn(os.path.abspath(p), called)

    def test_the_backup_message_does_not_claim_a_disk_verification(self):
        code, out = call(uv.main, ["backup", SETUP, "--dir", self.bdir, "--efivars", self.box.ef,
                                   "--dmi", self.box.dmi])
        self.assertEqual(code, 0, out)
        self.assertNotIn("verified by re-reading", out)
        self.assertIn("page cache", out)
        token = self.token()
        code, out = call(au.main, self.apply_args(token))
        self.assertEqual(code, 0, out)
        self.assertNotIn("verified by re-reading", out)
        self.assertIn("page cache", out)


# ---- R2: unencodable text must not silence the rest of the output
class TestR2Say(Round2):
    def test_unencodable_text_is_escaped_and_later_lines_still_appear(self):
        src = ("import sys\n"
               "sys.dont_write_bytecode = True\n"
               "sys.path.insert(0, %r)\n"
               "import uefivar as uv\n"
               "sys.stdout.reconfigure(encoding='ascii', errors='strict')\n"
               "uv.say('before')\n"
               "uv.say('variable caf\\u00e9')\n"
               "uv.say('DO NOT REBOOT (must be visible)')\n" % HERE)
        r = subprocess.run([sys.executable, "-I", "-c", src], capture_output=True, text=True)
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertEqual(r.stdout.splitlines(),
                         ["before", "variable caf\\xe9", "DO NOT REBOOT (must be visible)"])


# ---- R6: --only pci_common without that field is a refusal
class TestR6OnlyWithoutField(Round2):
    def test_only_pci_common_on_a_profile_without_it_is_a_clean_refusal(self):
        prof = read_json(self.box.profile)
        del prof["fields"]["pci_common"]
        self.box.write_profile(prof)
        for argv in (self.unlock_args("--only", "pci_common"),
                     ["verify", "--profile", self.box.profile, "--efivars", self.box.ef, "--dmi", self.box.dmi,
                      "--only", "pci_common"]):
            code, out = call_checked(self, au.main, argv)
            self.assertEqual(code, 2, out)
            self.assertIn("fields.pci_common", out)


# ---- R8: every target must be writable before the first write
class TestR8WritableProbe(Round2):
    def unwritable(self, prefix):
        orig = os.open

        def f(path, flags, *a, **k):
            if os.path.basename(str(path)).startswith(prefix) and flags & os.O_WRONLY and not flags & (os.O_TRUNC | os.O_CREAT):
                raise OSError(errno.EROFS, "read-only file system")      # the probe opens write-only, no truncate
            return orig(path, flags, *a, **k)
        return mock.patch.object(os, "open", f)

    def test_unlock_writes_nothing_when_a_later_target_is_not_writable(self):
        before = self.box.snapshot()
        token = self.token()
        rec, order, _ = writes_recorder()
        with self.unwritable("PCI_COMMON"), rec:
            code, out = call(au.main, self.apply_args(token))
        self.assertEqual(code, 2, out)
        self.assertEqual(order, [])
        self.assertIn("Nothing was written", out)
        self.assertEqual(self.box.snapshot(), before)
        self.assertNotEqual(read_json(os.path.join(self.bdir, os.listdir(self.bdir)[0], "report.json"))["status"],
                            "writing")

    def test_restore_writes_nothing_when_a_later_target_is_not_writable(self):
        code, out = call(au.main, self.apply_args())
        self.assertEqual(code, 0, out)
        applied = self.box.snapshot()
        rec, order, _ = writes_recorder()
        with self.unwritable("PCI_COMMON"), rec:
            code, out = call(rs.main, self.restore_argv(self.newest_backup()))
        self.assertEqual(code, 2, out)
        self.assertEqual(order, [])
        self.assertEqual(self.box.snapshot(), applied)

    def test_write_subcommand_refuses_an_unwritable_target(self):
        path = self.backup()
        before = self.box.snapshot()
        with self.unwritable("Setup-"):
            code, out = call(uv.main, self.apply_argv(path))
        self.assertEqual(code, 2, out)
        self.assertIn("Nothing was written", out)
        self.assertEqual(self.box.snapshot(), before)

    def test_the_probe_does_not_truncate(self):
        p = os.path.join(self.tmp, "probe.bin")
        with open(p, "wb") as fh:
            fh.write(b"keep me")
        self.assertEqual(uv.probe_writable([p]), [])
        with open(p, "rb") as fh:
            self.assertEqual(fh.read(), b"keep me")
        self.assertEqual([x[0] for x in uv.probe_writable([os.path.join(self.tmp, "missing.bin")])],
                         [os.path.join(self.tmp, "missing.bin")])


# ---- R11: the automatic rollback uses the backup it already verified
class TestR11Preloaded(Round2):
    def test_unlock_rollback_survives_the_dmi_vanishing_and_does_not_reload_the_backup(self):
        before = self.box.snapshot()
        token = self.token()
        orig, fired = uv.raw_write, []

        def w(path, blob):
            base = os.path.basename(path)
            if base.startswith("LastCheckSum"):
                raise OSError(errno.EIO, "io")
            orig(path, blob)
            if base.startswith("Setup-") and not fired:
                fired.append(1)
                os.remove(os.path.join(self.box.dmi, "bios_version"))      # DMI goes away mid-run
        loads, real_load = [], uv.BackupSet.load
        with mock.patch.object(uv, "raw_write", w), \
                mock.patch.object(uv.BackupSet, "load", side_effect=lambda p: (loads.append(p), real_load(p))[1]):
            code, out = call(au.main, self.apply_args(token))
        self.assertEqual(code, 3, out)
        self.assertIn("RESTORED and verified by read-back", out)
        self.assertEqual(self.box.snapshot(), before)
        self.assertEqual(len(loads), 1, loads)                           # only make_backup's own check

    def test_write_rollback_survives_the_dmi_vanishing(self):
        path = self.backup()
        before = self.box.snapshot()
        orig, fired = uv.raw_write, []

        def w(p, blob):
            orig(p, blob)
            if not fired:
                fired.append(1)
                raw = bytearray(open(p, "rb").read())
                raw[4 + 100] ^= 0xFF
                open(p, "wb").write(bytes(raw))
                os.remove(os.path.join(self.box.dmi, "bios_version"))
        with mock.patch.object(uv, "raw_write", w):
            code, out = call(uv.main, self.apply_argv(path))
        self.assertEqual(code, 3, out)
        self.assertIn("RESTORED and verified by read-back", out)
        self.assertEqual(self.box.snapshot(), before)

    def test_a_manual_restore_still_checks_the_machine(self):
        bk = self.backup()
        data = bytearray(self.box.data(SETUP))
        data[300] ^= 0xFF
        self.box.put(SETUP, 7, bytes(data))
        os.remove(os.path.join(self.box.dmi, "bios_version"))
        code, out = call(rs.main, self.restore_argv(bk))
        self.assertEqual(code, 2, out)


# ---- R9: say so when the signals cannot be ignored
class TestR9SignalWarning(Round2):
    def test_a_failure_to_install_the_handlers_is_reported(self):
        buf = io.StringIO()

        def run_in_thread():
            with contextlib.redirect_stdout(buf):
                with uv.IgnoreSignals():
                    pass
        t = threading.Thread(target=run_in_thread)
        t.start()
        t.join()
        self.assertIn("could not ignore", buf.getvalue())
        self.assertIn("SIGINT", buf.getvalue())


def suite():
    loader = unittest.defaultTestLoader
    s = unittest.TestSuite()
    for case in (TestUefivarBasics, TestBackup, TestWriteGuards, TestWorkflowDryRun, TestWorkflowRefusals,
                 TestWorkflowApply, TestVerify, TestRestore, TestInterruptsAndSignals, TestRollbackPolicy,
                 TestBackupSafety, TestWriteSubcommandGuards, TestRestoreCommand, TestR1RestorePairGuard,
                 TestR3VolatileRoots, TestR4WritingMarker, TestR5SetupFamily, TestR7MachineBinding,
                 TestR10Durability, TestR2Say, TestR6OnlyWithoutField, TestR8WritableProbe, TestR11Preloaded,
                 TestR9SignalWarning):
        s.addTests(loader.loadTestsFromTestCase(case))
    return s


def run():
    result = unittest.TextTestRunner(verbosity=1, stream=sys.stdout).run(suite())
    print("SELF-TEST %s: %d tests, %d failures, %d errors" % (
        "PASSED" if result.wasSuccessful() else "FAILED", result.testsRun,
        len(result.failures), len(result.errors)))
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    sys.exit(run())
