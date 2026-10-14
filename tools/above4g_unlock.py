#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""above4g_unlock.py - guided, guarded workflow built on uefivar.py.

  unlock   back up, measure the checksum relationship, then set the Above-4GB-MMIO
           Setup byte, the stored checksum and (optionally) the PCI_COMMON Above-4G
           byte. DRY RUN unless --apply.
  verify   read-only checks after the reboot: bytes persisted, checksum relation,
           guard flags, watch counters, PCI windows above 4 GiB.

The script contains NO board data. Variable names, GUIDs, lengths, offsets, expected
values and the BIOS version all come from a profile file that you fill in from your
own firmware (see profile.example.json, which is an EXAMPLE for one reference build).

Safety properties, all enforced in code:
  * dry run by default; a real write needs --apply, --i-understand-the-risk,
    --backup-dir, --confirm <token printed by the dry run> and a profile that says
    "verified_by_user": true
  * refuses unless the BIOS version (and board/product name, if the profile gives
    them) matches the profile, variable lengths and attributes match, and every
    target byte holds its expected old value
  * refuses authenticated variables, a non-zero guard flag, or a checksum delta
    outside the profile's bound (a negative delta wraps and is refused)
  * mandatory backup, fsynced, re-read and compared (from the page cache, so it checks
    the copy, not the disk) before the first write; a backup on tmpfs, ramfs or a live
    system's root is refused unless --allow-volatile-backup is given; so is a machine whose
    DMI shows neither a board name nor a product name
  * write order: Setup, LastCheckSum immediately after, then PCI_COMMON; ONE whole-
    blob os.write per variable with the original attributes; nothing is printed between
    two writes, and SIGINT, SIGTERM, SIGHUP and SIGQUIT are ignored from the first write
    to the end of the STOP handling
  * after the writes the whole Setup blob is diffed against the backup and the run
    FAILS if any byte outside the intended ones changed
  * any failure or interrupt prints STOP, the errno meaning, what each variable holds now,
    and the exact restore command. If Setup and the checksum disagree it restores them
    from the backup; if they are still a consistent pair (for example PCI_COMMON failed
    after both were written) it does NOT roll back, because a rollback is two more writes

Exit codes: 0 ok / dry run clean, 2 refused, 3 STOP (a write or its verification failed).
"""

import argparse
import datetime
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import uefivar as uv  # noqa: E402

say = uv.say
EXIT_OK, EXIT_REFUSED, EXIT_STOP = uv.EXIT_OK, uv.EXIT_REFUSED, uv.EXIT_STOP

# UEFI specification global-variable GUID of SecureBoot. A public constant, not board data.
SECURE_BOOT_GUID = "8be4df61-93ca-11d2-aa0d-00e098032b8c"
IOMEM_RE = re.compile(r"^\s*([0-9a-fA-F]+)-([0-9a-fA-F]+) : (.*)$")
MODEL_SUM16 = "sum16-le-live-delta"
MODEL_NONE = "none"
MAX_DELTA_CAP = 4
ROLES = ("setup", "checksum", "pci_common")


# ---------------------------------------------------------------------------
# profile
# ---------------------------------------------------------------------------

def _num(value, what, errs):
    if isinstance(value, bool) or not isinstance(value, (int, str)):
        errs.append("%s must be a number or a 0x-prefixed string" % what)
        return None
    try:
        return int(str(value), 0)
    except ValueError:
        errs.append("%s: %r is not a number" % (what, value))
        return None


def _name_list(raw, key, errs):
    val = raw.get(key, [])
    if not isinstance(val, list) or not all(isinstance(x, str) and x.strip() for x in val):
        errs.append("%s must be a list of variable names" % key)
        return []
    return [x.strip() for x in val]


def parse_profile(raw):
    """Validate a decoded profile. Returns (profile dict, list of errors)."""
    errs = []
    if not isinstance(raw, dict):
        return None, ["the profile must be a JSON object"]
    if raw.get("profile_version") != 1:
        errs.append("profile_version must be 1")
    P = {"title": str(raw.get("title") or "(untitled profile)"),
         "verified": raw.get("verified_by_user") is True,
         "warning": raw.get("_example_warning")}

    fw = raw.get("firmware")
    fw = fw if isinstance(fw, dict) else {}
    bios = fw.get("bios_version")
    if isinstance(bios, str) and bios.strip():
        P["bios"] = [bios.strip()]
    elif isinstance(bios, list) and bios and all(isinstance(x, str) and x.strip() for x in bios):
        P["bios"] = [x.strip() for x in bios]
    else:
        P["bios"] = []
        errs.append("firmware.bios_version is required: the exact string your machine reports in "
                    "/sys/class/dmi/id/bios_version")
    P["board_name"] = fw.get("board_name") if isinstance(fw.get("board_name"), str) else None
    P["product_name"] = fw.get("product_name") if isinstance(fw.get("product_name"), str) else None

    cs = raw.get("checksum")
    cs = cs if isinstance(cs, dict) else {}
    P["model"] = cs.get("model")
    P["max_delta"] = 0
    if P["model"] == MODEL_SUM16:
        md = _num(cs.get("max_delta", 1), "checksum.max_delta", errs)
        if md is not None:
            if not 0 <= md <= MAX_DELTA_CAP:
                errs.append("checksum.max_delta must be 0..%d: a larger gap means the model is wrong "
                            "for the board" % MAX_DELTA_CAP)
            else:
                P["max_delta"] = md
    elif P["model"] == MODEL_NONE:
        ev = cs.get("evidence")
        if not (isinstance(ev, str) and ev.strip()):
            errs.append("checksum.model 'none' needs checksum.evidence: say how you established that "
                        "your firmware has no setup integrity check")
    else:
        errs.append("checksum.model must be %r or %r (fail closed: a missing or unknown model is refused)"
                    % (MODEL_SUM16, MODEL_NONE))

    vs = raw.get("variables")
    vs = vs if isinstance(vs, dict) else {}
    P["vars"] = {}
    fields_raw = raw.get("fields")
    fields_raw = fields_raw if isinstance(fields_raw, dict) else {}
    needed = ["setup"]
    if P["model"] == MODEL_SUM16:
        needed.append("checksum")
    if "pci_common" in fields_raw:
        needed.append("pci_common")
    for role in needed:
        spec = vs.get(role)
        if not isinstance(spec, dict):
            errs.append("variables.%s is required" % role)
            continue
        name, guid = spec.get("name"), spec.get("guid")
        if not (isinstance(name, str) and name and isinstance(guid, str)):
            errs.append("variables.%s needs name and guid" % role)
            continue
        try:
            ref = uv.VarRef(name, guid)
        except uv.VarError as exc:
            errs.append("variables.%s: %s" % (role, exc))
            continue
        length = _num(spec.get("data_length"), "variables.%s.data_length" % role, errs)
        if length is None or length <= 0:
            errs.append("variables.%s.data_length must be a positive number" % role)
            continue
        attrs = None
        if spec.get("attrs") is not None:
            attrs = _num(spec["attrs"], "variables.%s.attrs" % role, errs)
        P["vars"][role] = {"ref": ref, "length": length, "attrs": attrs}
    if P["model"] == MODEL_SUM16 and "checksum" in P["vars"] and P["vars"]["checksum"]["length"] != 2:
        errs.append("variables.checksum.data_length must be 2 for the %s model" % MODEL_SUM16)

    P["fields"] = {}
    for role in ("setup", "pci_common"):
        spec = fields_raw.get(role)
        if spec is None:
            if role == "setup":
                errs.append("fields.setup is required")
            continue
        if not isinstance(spec, dict) or role not in P["vars"]:
            errs.append("fields.%s needs a matching variables.%s entry" % (role, role))
            continue
        off = _num(spec.get("offset"), "fields.%s.offset" % role, errs)
        width = _num(spec.get("width", 1), "fields.%s.width" % role, errs)
        old = _num(spec.get("expect_old"), "fields.%s.expect_old" % role, errs)
        new = _num(spec.get("new"), "fields.%s.new" % role, errs)
        if None in (off, width, old, new):
            continue
        multi = spec.get("allow_multibyte") is True
        geo = (uv.check_field_geometry(off, width, new, P["vars"][role]["length"], multi)
               + uv.check_field_geometry(off, width, old, P["vars"][role]["length"], multi))
        for g in sorted(set(geo)):
            errs.append("fields.%s: %s" % (role, g))
        if old == new:
            errs.append("fields.%s: expect_old equals new; nothing to change" % role)
        if not geo and old != new:
            P["fields"][role] = {"offset": off, "width": width, "old": old, "new": new,
                                 "label": str(spec.get("label") or role)}
    P["guard_flags"] = _name_list(raw, "guard_flags", errs)
    P["watch"] = _name_list(raw, "watch", errs)
    P["also_backup"] = _name_list(raw, "also_backup", errs)
    return P, errs


def load_profile(path):
    try:
        with open(path, "r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, ValueError) as exc:
        raise uv.VarError("cannot read profile %s: %s" % (path, exc))
    return parse_profile(raw)


def selected_roles(P, only):
    if only:
        wanted = [x.strip() for x in only.split(",") if x.strip()]
        bad = [x for x in wanted if x not in ("setup", "pci_common")]
        if bad or not wanted:
            raise uv.VarError("--only takes setup, pci_common or both, comma separated; got %r" % only)
        chosen = [r for r in ("setup", "pci_common") if r in wanted]
        missing = [r for r in chosen if r not in P["fields"]]
        if missing:
            raise uv.VarError("--only %s: this profile defines no fields.%s, so there is nothing to change "
                              "or check there" % (",".join(missing), ",".join(missing)))
        return chosen
    return [r for r in ("setup", "pci_common") if r in P["fields"]]


# ---------------------------------------------------------------------------
# preflight
# ---------------------------------------------------------------------------

class Checks(object):
    def __init__(self):
        self.fails, self.warns = [], []

    def ok(self, msg):
        say("  [ ok ] " + msg)

    def info(self, msg):
        say("  [info] " + msg)

    def warn(self, msg):
        self.warns.append(msg)
        say("  [warn] " + msg)

    def fail(self, msg):
        self.fails.append(msg)
        say("  [FAIL] " + msg)


class Step(object):
    def __init__(self, role, var, new_data, intended, text):
        self.role = role
        self.var = var
        self.new_data = new_data
        self.blob = var.raw[:4] + new_data
        self.intended = set(intended)
        self.text = text


class State(object):
    def __init__(self):
        self.real = False
        self.vars, self.refs = {}, {}
        self.already = {}
        self.cur_sum = self.stored = self.delta = None
        self.flags, self.watch, self.also = [], [], []
        self.steps = []
        self.live_bios = None


def _resolve_list(a, names, c, what):
    out = []
    for n in names:
        try:
            ref = uv.loose_ref(a.efivars, n)
        except uv.VarError as exc:
            c.fail(str(exc))
            continue
        if ref is None:
            c.warn("%s variable %r is not present on this firmware" % (what, n))
        else:
            out.append(ref)
    return out


def gather(a, P, roles, apply, c):
    """Read-only checks. Prints each result, returns a State."""
    st = State()
    say("Preflight")
    if not os.path.isdir(a.efivars):
        c.fail("%s does not exist. Is this machine booted in UEFI mode, and is efivarfs mounted "
               "(mount -t efivarfs none %s)?" % (a.efivars, uv.DEFAULT_EFIVARS))
        return st
    st.real = uv.is_real_efivarfs(a.efivars)
    if st.real:
        c.ok("%s is a real efivarfs mount" % a.efivars)
        if apply and not uv.is_root():
            c.fail("not running as root")
        if apply and uv.efivarfs_is_readonly(a.efivars):
            c.fail("efivarfs is mounted read-only")
    else:
        c.info("%s is not an efivarfs mount (synthetic directory): chattr is never called" % a.efivars)

    # firmware fingerprint
    st.live_bios = uv.read_dmi(a.dmi, "bios_version")
    if st.live_bios is None:
        c.fail("cannot read %s/bios_version; the profile can only be checked against a readable "
               "fingerprint" % a.dmi)
    elif st.live_bios in P["bios"]:
        c.ok("BIOS version %r matches the profile" % st.live_bios)
    else:
        c.fail("BIOS version is %r, the profile is for %s. Offsets move between builds; build your "
               "own profile from this build's IFR." % (st.live_bios, ", ".join(repr(b) for b in P["bios"])))
    for key in ("board_name", "product_name"):
        if P[key]:
            live = uv.read_dmi(a.dmi, key)
            if live == P[key]:
                c.ok("%s %r matches the profile" % (key, live))
            else:
                c.fail("%s is %r, the profile says %r" % (key, live, P[key]))
    if not (uv.read_dmi(a.dmi, "board_name") or uv.read_dmi(a.dmi, "product_name")):
        (c.fail if apply else c.warn)(
            "board_name and product_name cannot be read from %s. A backup would be tied to this machine by the "
            "BIOS string alone, which many machines share, so --apply refuses that." % a.dmi)

    # variables
    for role in ROLES:
        spec = P["vars"].get(role)
        if spec is None:
            continue
        st.refs[role] = spec["ref"]
        try:
            var = uv.read_var(a.efivars, spec["ref"])
        except uv.VarError as exc:
            c.fail(str(exc))
            continue
        st.vars[role] = var
        if len(var.data) != spec["length"]:
            c.fail("%s holds %d data bytes, the profile says %d" % (spec["ref"], len(var.data), spec["length"]))
        else:
            c.ok("%s: %d data bytes, attrs 0x%x (%s)" % (spec["ref"], len(var.data), var.attrs,
                                                         uv.attrs_names(var.attrs)))
        for p in uv.attr_problems(var.attrs, spec["attrs"]):
            c.fail("%s: %s" % (spec["ref"], p))

    # target bytes
    for role in roles:
        f, var = P["fields"][role], st.vars.get(role)
        if var is None or len(var.data) != P["vars"][role]["length"]:
            continue
        cur = int.from_bytes(var.data[f["offset"]:f["offset"] + f["width"]], "little")
        if cur == f["new"]:
            st.already[role] = True
            c.info("%s offset 0x%x already holds 0x%x (%s): nothing to change there"
                   % (st.refs[role].name, f["offset"], cur, f["label"]))
        elif cur == f["old"]:
            c.ok("%s offset 0x%x holds the expected old value 0x%x" % (st.refs[role].name, f["offset"], cur))
        else:
            c.fail("%s offset 0x%x holds 0x%x; the profile expects 0x%x (or 0x%x if already applied). "
                   "Unknown state: stop and re-check your profile against the IFR."
                   % (st.refs[role].name, f["offset"], cur, f["old"], f["new"]))

    # checksum relationship, measured now
    if P["model"] == MODEL_SUM16:
        sv, cv = st.vars.get("setup"), st.vars.get("checksum")
        if sv is not None and cv is not None and len(cv.data) == 2:
            st.cur_sum = uv.sum16(sv.data)
            st.stored = int.from_bytes(cv.data, "little")
            st.delta = (st.stored - st.cur_sum) & 0xFFFF
            msg = ("sum16(Setup)=0x%04x, stored %s=0x%04x, live delta = stored - sum16 = %d (bound 0..%d)"
                   % (st.cur_sum, st.refs["checksum"].name, st.stored, st.delta, P["max_delta"]))
            if st.delta <= P["max_delta"]:
                c.ok(msg)
            elif "setup" in roles:
                c.fail(msg + ". Outside the bound: the checksum model does not describe this board. "
                       "Refusing; nothing is guessed.")
            else:
                c.warn(msg + ". Outside the bound (not writing Setup, so not fatal here).")
    else:
        c.warn("profile declares no integrity guard (%s). The tool cannot verify that; it rests on "
               "your evidence." % P.get("model"))

    # guard flags, watch counters, extra backups
    for ref in _resolve_list(a, P["guard_flags"], c, "guard flag"):
        try:
            var = uv.read_var(a.efivars, ref)
        except uv.VarError as exc:
            c.fail(str(exc))
            continue
        st.flags.append((ref, var))
        if any(bytearray(var.data)):
            c.fail("guard flag %s is %s, expected all zero. The firmware has already recorded something; "
                   "find out why before writing." % (ref.name, var.data.hex()))
        else:
            c.ok("guard flag %s is zero" % ref.name)
    for ref in _resolve_list(a, P["watch"], c, "watch"):
        try:
            var = uv.read_var(a.efivars, ref)
        except uv.VarError as exc:
            c.fail(str(exc))
            continue
        st.watch.append((ref, var))
        c.info("watch %s = %s (compared again after the reboot)" % (ref.name, var.data.hex()))
    st.also = _resolve_list(a, P["also_backup"], c, "extra backup")

    # context that is reported, not decided here
    try:
        sb = uv.read_var(a.efivars, uv.VarRef("SecureBoot", SECURE_BOOT_GUID))
        c.info("Secure Boot variable reads %s (the efivarfs route does not need it off; "
               "shell-based tools do)" % ("enabled" if sb.data[:1] == b"\x01" else "disabled"))
    except uv.VarError:
        c.info("Secure Boot state not readable here")
    c.info("CSM cannot be read from the OS: confirm in the firmware menu that boot mode is UEFI only "
           "and CSM is off before you enable 4G decoding")
    c.info("Keep AC power stable, stop other firmware writers such as fwupd, and boot the machine "
           "with the target card physically absent")
    return st


def build_steps(P, st, roles):
    steps = []
    if "setup" in roles and not st.already.get("setup") and "setup" in st.vars:
        f, var = P["fields"]["setup"], st.vars["setup"]
        new = uv.apply_field(var.data, f["offset"], f["width"], f["new"])
        steps.append(Step("setup", var, new, range(f["offset"], f["offset"] + f["width"]),
                          "%s offset 0x%x width %d: 0x%x -> 0x%x   %s" % (
                              st.refs["setup"].name, f["offset"], f["width"], f["old"], f["new"], f["label"])))
        if P["model"] == MODEL_SUM16:
            new_sum = (uv.sum16(new) + st.delta) & 0xFFFF
            cv = st.vars["checksum"]
            steps.append(Step("checksum", cv, new_sum.to_bytes(2, "little"), range(0, 2),
                              "%s: 0x%04x -> 0x%04x   (= sum16(new Setup) 0x%04x + live delta %d)" % (
                                  st.refs["checksum"].name, st.stored, new_sum, uv.sum16(new), st.delta)))
    if "pci_common" in roles and not st.already.get("pci_common") and "pci_common" in st.vars:
        f, var = P["fields"]["pci_common"], st.vars["pci_common"]
        new = uv.apply_field(var.data, f["offset"], f["width"], f["new"])
        steps.append(Step("pci_common", var, new, range(f["offset"], f["offset"] + f["width"]),
                          "%s offset 0x%x width %d: 0x%x -> 0x%x   %s" % (
                              st.refs["pci_common"].name, f["offset"], f["width"], f["old"], f["new"], f["label"])))
    return steps


def confirm_token(P, st, steps):
    parts = [st.live_bios or "?"]
    for s in steps:
        if s.role in P["fields"]:
            parts.append("0x%x" % P["fields"][s.role]["offset"])
    return ":".join(parts)


# ---------------------------------------------------------------------------
# after-reboot text
# ---------------------------------------------------------------------------

def print_after_reboot(profile_arg):
    say("")
    say("AFTER THE WRITE: the physical outcome is the proof. A byte that reads back correctly is not.")
    say("  1. Do NOT install the card yet. Reboot with the target card physically absent.")
    say("  2. The machine must boot normally. If the firmware reports a setup integrity error (Lenovo: "
        "Error 0162) it has reloaded defaults:")
    say("     your change is gone and so are any other BIOS settings you had customised. If it will not "
        "boot: power off, remove any added card, see docs/05-safety-and-recovery.md.")
    say("  3. Re-check the variables:  python3 above4g_unlock.py verify --profile %s --report <backup-dir>"
        % profile_arg)
    say("  4. Look for a new 64-bit root window:  dmesg | grep -i 'root bus resource'   "
        "(a range above 0x100000000), and  cat /proc/iomem  as root.")
    say("  5. Only when 3 and 4 look right: power off, install the card, boot, then  lspci -vv -s <bus:dev.fn>  "
        "(Region lines: 64-bit, prefetchable, address above 4 GiB)")
    say("     and, for NVIDIA cards,  nvidia-smi -q -d MEMORY  for the BAR1 total.")
    say("  6. Power-cycle once more and run verify again, to confirm the setting persists.")


# ---------------------------------------------------------------------------
# unlock
# ---------------------------------------------------------------------------

def _hex_or_none(var):
    return var.data.hex() if var is not None else None


def write_report(path, status, P, st, steps, bset, extra=None, strict=False):
    """Write <path>/report.json atomically and durably (temp file, fsync, rename, fsync of the
    directory). strict: let a failure escape; otherwise it is a warning."""
    report = {
        "tool": "above4g_unlock.py",
        "utc": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "status": status,
        "profile_title": P["title"],
        "bios_version": st.live_bios,
        "backup_dir": bset.path if bset else None,
        "sum16_before": st.cur_sum,
        "stored_checksum_before": st.stored,
        "delta": st.delta,
        "steps": [{"role": s.role, "variable": str(s.var.ref), "text": s.text} for s in steps],
        "guard_flags_before": {r.name: v.data.hex() for r, v in st.flags},
        "watch_before": {r.name: v.data.hex() for r, v in st.watch},
    }
    if extra:
        report.update(extra)
    try:
        uv.write_file_atomic(os.path.join(path, "report.json"), (json.dumps(report, indent=2) + "\n").encode("utf-8"))
    except (OSError, uv.VarError) as exc:
        if strict:
            raise
        say("warning: could not write report.json: %s" % exc)


def verify_after_write(a, st, steps, bset):
    problems, live = [], {}
    for s in steps:
        try:
            lv = uv.read_var(a.efivars, s.var.ref)
        except uv.VarError as exc:
            problems.append(str(exc))
            continue
        live[s.role] = lv
        if lv.raw[:4] != s.var.raw[:4]:
            problems.append("%s: attributes changed" % s.var.ref)
        if len(lv.data) != len(s.var.data):
            problems.append("%s: length changed %d -> %d" % (s.var.ref, len(s.var.data), len(lv.data)))
            continue
        if lv.data != s.new_data:
            problems.append("%s: read-back differs from what was written" % s.var.ref)
        outside = [o for o in uv.diff_offsets(bset.entry(s.var.ref).data, lv.data) if o not in s.intended]
        if outside:
            problems.append("%s: byte(s) outside the intended range changed: %s" % (
                s.var.ref, ", ".join("0x%x" % o for o in outside[:20])))
    if "setup" in live and "checksum" in live:
        want = (uv.sum16(live["setup"].data) + st.delta) & 0xFFFF
        got = int.from_bytes(live["checksum"].data, "little")
        if want != got:
            problems.append("stored checksum is 0x%04x but sum16(live Setup) + delta is 0x%04x" % (got, want))
    done = set(live)
    for role, var in st.vars.items():
        if role in [s.role for s in steps] or role in done:
            continue
        try:
            lv = uv.read_var(a.efivars, var.ref)
        except uv.VarError as exc:
            problems.append(str(exc))
            continue
        if lv.raw != var.raw:
            problems.append("%s changed although this run did not write it" % var.ref)
    return problems


def unfinished_reports(backup_dir):
    """The backup directories under backup_dir whose report.json still says "writing": a run
    opened a write window there and never closed it (power loss, SIGKILL, a crash)."""
    found = []
    try:
        names = sorted(os.listdir(backup_dir))
    except OSError:
        return found
    for n in names:
        d = os.path.join(backup_dir, n)
        rp = os.path.join(d, "report.json")
        if not n.startswith("efivar-backup-") or not os.path.isfile(rp):
            continue
        try:
            with open(rp, "r", encoding="utf-8") as fh:
                report = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(report, dict) and report.get("status") == "writing":
            found.append(d)
    return found


def report_unfinished(a, dirs):
    """The STOP-style message for reports that still say "writing", with the restore command."""
    for d in dirs:
        try:
            needs_flag = bool(uv.companion_problem([e.ref for e in uv.BackupSet.load(d).entries if e.restore]))
        except uv.VarError:
            needs_flag = False
        say("STOP: %s still says writing. A write window was open when that run ended (power loss, SIGKILL "
            "or a crash), so Setup and its checksum may not agree, and the next boot could raise Error 0162 "
            "and reload BIOS defaults. Do NOT reboot until this is dealt with." % os.path.join(d, "report.json"))
        say("Look at what the variables hold now, or put the backup back with:")
        say("  " + uv.restore_command(d, a.efivars, a.dmi, no_checksum_companion=needs_flag))
        say("restore.py marks the report resolved when it succeeds. If you have checked the live variables and "
            "want to keep them as they are, move that backup directory out of the way.")


def cmd_unlock(a):
    P, errs = load_profile(a.profile)
    if errs:
        for e in errs:
            say("PROFILE ERROR: %s" % e)
        say("VERDICT: refused. Fix the profile; nothing was read or written.")
        return EXIT_REFUSED
    roles = selected_roles(P, a.only)
    apply = a.apply
    say("Profile : %s" % P["title"])
    if P["warning"]:
        say("          %s" % P["warning"])
    say("Mode    : %s" % ("APPLY (real writes)" if apply else "DRY RUN (nothing is written)"))
    say("Fields  : %s" % ", ".join(roles))
    say("")
    if a.backup_dir:
        stuck = unfinished_reports(a.backup_dir)
        if stuck:
            report_unfinished(a, stuck)
            return EXIT_STOP
    c = Checks()
    st = gather(a, P, roles, apply, c)
    steps = build_steps(P, st, roles) if not c.fails else []
    say("")
    if c.fails:
        say("VERDICT: refused (%d failed check(s)). Nothing was written." % len(c.fails))
        return EXIT_REFUSED
    if not steps:
        say("Nothing to do: every selected field already holds its target value.")
        return EXIT_OK

    say("PLAN  (write order: Setup, then the checksum immediately, then PCI_COMMON)")
    for i, s in enumerate(steps, 1):
        say("  %d. %s" % (i, s.text))
    say("  Each write is ONE whole-blob os.write with the original attribute bytes (%s)." % (
        ", ".join("%s: %d bytes" % (s.var.ref.name, len(s.blob)) for s in steps)))
    say("  Afterwards the whole blob is diffed against the backup; any byte outside the ones above "
        "fails the run (STOP).")
    token = confirm_token(P, st, steps)

    gates = []
    if apply:
        if not P["verified"]:
            gates.append("the profile is not marked \"verified_by_user\": true. Check every value against the "
                         "IFR of your own BIOS image first.")
        if not a.risk:
            gates.append("--i-understand-the-risk is required for a real write")
        if not a.backup_dir:
            gates.append("--backup-dir DIR is required: no write happens without a verified backup")
        if a.confirm != token:
            gates.append("--confirm %s is required (BIOS version and the offsets about to be written)" % token)
    if gates:
        say("")
        for g in gates:
            say("REFUSE: %s" % g)
        say("VERDICT: refused. Nothing was written.")
        return EXIT_REFUSED
    if not apply:
        say("")
        say("DRY RUN complete: every check passed, nothing was written.")
        if not P["verified"]:
            say("Note: --apply will be refused until the profile says \"verified_by_user\": true.")
        say("To apply:  --apply --i-understand-the-risk --backup-dir DIR --confirm %s" % token)
        print_after_reboot(a.profile)
        return EXIT_OK

    # ---- apply -------------------------------------------------------------
    say("")
    core = [st.refs[r] for r in ROLES if r in st.refs]
    extra_refs = [r for r, _ in st.flags + st.watch] + st.also
    extra_refs = [r for r in extra_refs if r not in core]
    try:
        bset = uv.make_backup(a.efivars, core, a.backup_dir, extra_refs, a.dmi,
                              allow_volatile=a.allow_volatile_backup)
    except (uv.VarError, OSError) as exc:
        say("REFUSE: backup failed (%s). Nothing was written." % exc)
        return EXIT_REFUSED
    say("Backup written and fsynced; re-read and compared with the live variables (the re-read may be "
        "served from the page cache, so it checks the copy, not the disk): %s" % bset.path)
    for role, var in st.vars.items():
        if bset.entry(var.ref).raw != var.raw:
            say("REFUSE: %s changed between the preflight read and the backup. Nothing was written."
                % var.ref)
            return EXIT_REFUSED

    # From the first write to the last line of the STOP handling the process ignores SIGINT,
    # SIGTERM, SIGHUP and SIGQUIT: an interrupt between the Setup write and the checksum write
    # leaves them disagreeing.
    with uv.IgnoreSignals():
        return apply_writes(a, P, st, steps, bset)


def apply_writes(a, P, st, steps, bset):
    """The write window. Nothing is printed between two writes; any exception, KeyboardInterrupt
    and a broken pipe included, ends the loop and goes to the STOP handling."""
    wrote, inflight, failure, problems = [], None, None, []
    # Durable mark, before anything is written: if this process is killed or the power goes between
    # the Setup write and the checksum write, the next run finds a report that still says "writing".
    try:
        write_report(bset.path, "writing", P, st, steps, bset, strict=True)
    except (OSError, uv.VarError) as exc:
        say("REFUSE: cannot record the write in progress in report.json (%s). Nothing was written." % exc)
        return EXIT_REFUSED
    with uv.Unlocked([s.var.path for s in steps], st.real):
        unwritable = uv.probe_writable([s.var.path for s in steps])    # every target, before the first write
        if not unwritable:
            try:
                for s in steps:                   # nothing but the write between consecutive steps
                    inflight = s
                    uv.raw_write(s.var.path, s.blob)
                    wrote.append(s)
                    inflight = None
                problems = verify_after_write(a, st, steps, bset)
            except BaseException as exc:          # KeyboardInterrupt and a broken pipe included
                failure = exc
    if unwritable:
        for path, exc in unwritable:
            say("REFUSE: cannot open %s for writing: %s" % (path, uv.describe_failure(exc)))
        say("VERDICT: refused. Nothing was written.")
        write_report(bset.path, "refused-before-writing", P, st, steps, bset)
        return EXIT_REFUSED
    for s in wrote:
        say("wrote %s" % s.var.ref)
    if failure is None and not problems:
        say("VERIFIED: read-back matches, the whole blob differs from the backup only at the intended "
            "bytes, and the stored checksum equals sum16(Setup) + the live delta." if "checksum" in
            [s.role for s in steps] else
            "VERIFIED: read-back matches and the whole blob differs from the backup only at the intended bytes.")
        write_report(bset.path, "applied", P, st, steps, bset)
        say("Report: %s" % os.path.join(bset.path, "report.json"))
        print_after_reboot(a.profile)
        return EXIT_OK
    return stop_after_failure(a, P, st, steps, bset, wrote, inflight, failure, problems)


def pair_state(a, steps, bset):
    """Where Setup and its checksum stand now, read from the live variables, not from the record
    of writes: 'old' (every one equals the backup), 'new' (every one equals what this run meant to
    write), 'mixed' (anything else, including an unreadable one) or 'n/a' (this run does not write
    either of them)."""
    pair = [s for s in steps if s.role in ("setup", "checksum")]
    if not pair:
        return "n/a"
    seen = set()
    for s in pair:
        try:
            live = uv.read_var(a.efivars, s.var.ref)
        except uv.VarError:
            return "mixed"
        if live.raw == bset.entry(s.var.ref).raw:
            seen.add("old")
        elif live.raw == s.blob:
            seen.add("new")
        else:
            return "mixed"
    return seen.pop() if len(seen) == 1 else "mixed"


def stop_after_failure(a, P, st, steps, bset, wrote, inflight, failure, problems):
    """STOP handling after a failed or interrupted write window.

    Rollback is decided from the live state of the pair. When Setup and the checksum are still
    a consistent pair (both old, both new, or untouched), nothing is rewritten: a rollback is two
    more writes, and a failure between them would create the mismatch the firmware punishes. When
    they disagree, or one is unreadable, the backup is written back, Setup first and the checksum
    right after. Whatever happens, the exact restore command is printed."""
    if failure is not None and inflight is not None:
        say("STOP: writing %s failed: %s" % (inflight.var.ref, uv.describe_failure(failure)))
    elif failure is not None:
        say("STOP: the verification could not finish: %s" % uv.describe_failure(failure))
    for p in problems:
        say("STOP: verification failed: %s" % p)
    say("State at the stop, in write order:")
    for s in steps:
        if s in wrote:
            did = "write completed"
        elif s is inflight:
            did = "write failed or was interrupted"
        else:
            did = "not attempted"
        say("  %-14s %s; the live value %s" % (s.var.ref.name, did,
                                                uv.live_state(a.efivars, bset, s.var.ref)))
    refs = [s.var.ref for s in steps]
    names = " and ".join(s.var.ref.name for s in steps if s.role in ("setup", "checksum"))
    has_checksum = any(s.role == "checksum" for s in steps)
    state = pair_state(a, steps, bset)

    if state != "mixed":
        if state == "n/a":
            say("This run does not write Setup or its checksum; they are the consistent pair they were "
                "before, so a reboot will not raise Error 0162.")
        elif not has_checksum:
            say("%s: no checksum guard is declared for this profile, so there is no pair to disagree." % names)
        elif state == "new":
            say("%s hold the new values and are a consistent pair (the checksum was computed for this "
                "Setup), so a reboot will not raise Error 0162." % names)
        else:
            say("%s still hold their original values and are a consistent pair, so a reboot will not "
                "raise Error 0162." % names)
        say("No automatic rollback: it would rewrite Setup and the checksum, and a failure between those "
            "two writes would leave them disagreeing, which is the one state that costs BIOS settings.")
        if any(s.role == "pci_common" for s in steps):
            say("PCI_COMMON is not covered by the checksum; check it with 'uefivar.py read' if its write "
                "was the step that failed.")
        write_report(bset.path, "stopped-not-rolled-back", P, st, steps, bset, {"pair_state": state})
        uv.stop_guidance(say, bset.path, a.efivars, a.dmi, False, uv.lengths_differ(a.efivars, bset, refs),
                         bset=bset)
        return EXIT_STOP

    if has_checksum:
        say("STOP: %s do not agree now. The next boot would raise Error 0162 and reload BIOS defaults. "
            "Do NOT reboot." % names)
    else:
        say("STOP: Setup holds neither its backup value nor the one this run meant to write.")
    say("Restoring from the backup now, Setup first and its checksum right after, back to back.")
    code = uv.restore_backup(a.efivars, bset.path, apply=True, risk_ok=True, dmi_dir=a.dmi,
                             only=refs, allow_length_change=True, guidance=False, preloaded=bset)
    if code == EXIT_OK:
        write_report(bset.path, "stopped-and-restored", P, st, steps, bset, {"pair_state": state})
        uv.stop_guidance(say, bset.path, a.efivars, a.dmi, False, recovered=True, bset=bset)
    else:
        write_report(bset.path, "stopped-RESTORE-INCOMPLETE", P, st, steps, bset, {"pair_state": state})
        say("The automatic restore did not complete.")
        uv.stop_guidance(say, bset.path, a.efivars, a.dmi, True, uv.lengths_differ(a.efivars, bset, refs),
                         bset=bset)
    return EXIT_STOP


# ---------------------------------------------------------------------------
# verify (read only)
# ---------------------------------------------------------------------------

def high_pci_windows(text):
    wins, visible = [], False
    for line in text.splitlines():
        m = IOMEM_RE.match(line)
        if not m:
            continue
        s, e, desc = int(m.group(1), 16), int(m.group(2), 16), m.group(3).strip()
        if s != 0:
            visible = True
        if s >= (1 << 32) and desc.startswith("PCI Bus"):
            wins.append((s, e, desc))
    return wins, visible


def cmd_verify(a):
    P, errs = load_profile(a.profile)
    if errs:
        for e in errs:
            say("PROFILE ERROR: %s" % e)
        return EXIT_REFUSED
    roles = selected_roles(P, a.only)
    report = None
    if a.report:
        rp = os.path.join(a.report, "report.json") if os.path.isdir(a.report) else a.report
        try:
            with open(rp, "r", encoding="utf-8") as fh:
                report = json.load(fh)
        except (OSError, ValueError) as exc:
            say("REFUSE: cannot read the report %s: %s" % (rp, exc))
            return EXIT_REFUSED
        if isinstance(report, dict) and report.get("status") == "writing":
            report_unfinished(a, [os.path.dirname(os.path.abspath(rp))])
            return EXIT_STOP
    c = Checks()
    say("Profile: %s" % P["title"])
    say("Verify (read only)")
    if not os.path.isdir(a.efivars):
        c.fail("%s does not exist" % a.efivars)
    else:
        live_bios = uv.read_dmi(a.dmi, "bios_version")
        if live_bios in P["bios"]:
            c.ok("BIOS version %r matches the profile" % live_bios)
        else:
            c.fail("BIOS version is %r, the profile is for %s" % (live_bios, ", ".join(P["bios"])))
        vars_, cur = {}, {}
        for role in ROLES:
            spec = P["vars"].get(role)
            if spec is None:
                continue
            try:
                v = uv.read_var(a.efivars, spec["ref"])
            except uv.VarError as exc:
                c.fail(str(exc))
                continue
            vars_[role] = v
            if len(v.data) != spec["length"]:
                c.fail("%s holds %d data bytes, the profile says %d" % (spec["ref"], len(v.data), spec["length"]))
            for p in uv.attr_problems(v.attrs, spec["attrs"]):
                c.fail("%s: %s" % (spec["ref"], p))
        for role in roles:
            v, f = vars_.get(role), P["fields"][role]
            if v is None or len(v.data) < f["offset"] + f["width"]:
                continue
            now = int.from_bytes(v.data[f["offset"]:f["offset"] + f["width"]], "little")
            if now == f["new"]:
                c.ok("%s offset 0x%x holds 0x%x: the change persisted" % (v.ref.name, f["offset"], now))
            elif now == f["old"]:
                c.fail("%s offset 0x%x is back at its default 0x%x: the firmware reverted the change "
                       "(or it never took effect)" % (v.ref.name, f["offset"], now))
            else:
                c.fail("%s offset 0x%x holds 0x%x, neither the old nor the new value"
                       % (v.ref.name, f["offset"], now))
        if P["model"] == MODEL_SUM16 and "setup" in vars_ and "checksum" in vars_ and len(vars_["checksum"].data) == 2:
            s16 = uv.sum16(vars_["setup"].data)
            stored = int.from_bytes(vars_["checksum"].data, "little")
            delta = (stored - s16) & 0xFFFF
            msg = "sum16(Setup)=0x%04x stored=0x%04x delta=%d" % (s16, stored, delta)
            if report and report.get("delta") is not None and report["delta"] != delta:
                msg += " (was %d when written; the relation can move, the firmware's own verdict is what counts)" % report["delta"]
            (c.ok if delta <= P["max_delta"] else c.fail)(msg + " (bound 0..%d)" % P["max_delta"])
        for ref in _resolve_list(a, P["guard_flags"], c, "guard flag"):
            try:
                v = uv.read_var(a.efivars, ref)
            except uv.VarError as exc:
                c.fail(str(exc))
                continue
            (c.fail if any(bytearray(v.data)) else c.ok)("guard flag %s = %s" % (ref.name, v.data.hex()))
        for ref in _resolve_list(a, P["watch"], c, "watch"):
            try:
                v = uv.read_var(a.efivars, ref)
            except uv.VarError as exc:
                c.fail(str(exc))
                continue
            before = (report or {}).get("watch_before", {}).get(ref.name)
            if before is None:
                c.info("watch %s = %s (no report given, nothing to compare)" % (ref.name, v.data.hex()))
            elif before == v.data.hex():
                c.ok("watch %s = %s, unchanged since the write" % (ref.name, v.data.hex()))
            else:
                c.fail("watch %s changed from %s to %s since the write; the firmware logged something"
                       % (ref.name, before, v.data.hex()))
    try:
        with open(a.iomem, "r") as fh:
            text = fh.read()
    except OSError as exc:
        c.warn("cannot read %s: %s" % (a.iomem, exc))
    else:
        wins, visible = high_pci_windows(text)
        if not visible:
            c.warn("%s shows no addresses (run as root)" % a.iomem)
        elif wins:
            for s, e, d in wins:
                c.ok("PCI range above 4 GiB in iomem: 0x%x-0x%x  %s" % (s, e, d))
        else:
            c.warn("no 'PCI Bus' range above 4 GiB in %s. The reference build gained one before any card "
                   "was installed; the label and the dmesg line below are typical, not guaranteed on "
                   "every kernel and board." % a.iomem)
    c.info("also run:  dmesg | grep -i 'root bus resource'   (a range above 0x100000000)")
    say("")
    if c.fails:
        say("RESULT: %d check(s) failed. Do not install the card until you understand why. Restore with "
            "restore.py if needed." % len(c.fails))
        return EXIT_REFUSED
    say("RESULT: the variable checks passed. That shows the setting persisted and the firmware raised no flag;")
    say("it does not by itself prove the unlock. The proof is a 64-bit root window now, and later the card's "
        "BARs placed above 4 GiB.")
    return EXIT_OK


# ---------------------------------------------------------------------------
# command line
# ---------------------------------------------------------------------------

def build_parser():
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", required=True, metavar="FILE",
                        help="your per-board profile (JSON). profile.example.json is an EXAMPLE only")
    common.add_argument("--efivars", default=uv.DEFAULT_EFIVARS, metavar="PATH",
                        help="efivarfs directory (default %(default)s)")
    common.add_argument("--dmi", default=uv.DEFAULT_DMI, metavar="PATH",
                        help="DMI directory (default %(default)s)")
    common.add_argument("--only", metavar="LIST",
                        help="setup, pci_common or both (default: every field the profile defines). "
                             "'--only setup' first is the cautious order")
    p = argparse.ArgumentParser(prog="above4g_unlock.py", description=__doc__.split("\n\n")[0])
    sub = p.add_subparsers(dest="cmd", metavar="COMMAND")
    s = sub.add_parser("unlock", parents=[common], help="plan (default) or apply the change")
    s.add_argument("--backup-dir", metavar="DIR", help="parent directory for the backup (required with --apply)")
    s.add_argument("--allow-volatile-backup", action="store_true",
                   help="accept a --backup-dir that is gone after a reboot (tmpfs, ramfs, a live "
                        "system's RAM-backed root); on a live system use a USB stick or a disk partition instead")
    s.add_argument("--apply", action="store_true", help="really write; default is a dry run")
    s.add_argument("--i-understand-the-risk", dest="risk", action="store_true", help="required with --apply")
    s.add_argument("--confirm", metavar="TOKEN", help="the token the dry run prints (BIOS version and offsets)")
    s.set_defaults(func=cmd_unlock)
    s = sub.add_parser("verify", parents=[common], help="read-only checks after the reboot")
    s.add_argument("--report", metavar="DIR_OR_FILE", help="backup directory (or its report.json) from the unlock run")
    s.add_argument("--iomem", default="/proc/iomem", metavar="PATH", help="default %(default)s")
    s.set_defaults(func=cmd_verify)
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if "--self-test" in argv:
        return uv.run_selftest()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not getattr(args, "cmd", None):
        parser.print_help()
        return EXIT_REFUSED
    try:
        return args.func(args)
    except uv.VarError as exc:
        say("ERROR: %s" % exc)
        return EXIT_REFUSED


if __name__ == "__main__":
    sys.exit(main())
