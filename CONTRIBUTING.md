# Contributing

This is a technical-community knowledge base about keeping out-of-warranty hardware in service. The
most valuable contributions are:

- **Hardware reports.** You tried the method on a board not yet listed: say which board, BIOS version,
  GPU, which variables and offsets you used (from your own IFR), whether a checksum guard was present,
  and what happened. A negative result is as useful as a positive one. Use the
  [hardware report template](.github/ISSUE_TEMPLATE/hardware-report.md).
- **Safety fixes.** A wrong offset, a guard that passes when it should refuse, a missing warning, a
  recovery step that does not work on some board. These get priority.
- **Corrections.** If something here is inaccurate, open an issue with a source.

Please keep contributions **vendor-neutral and reproducible**, and **never commit** firmware images,
variable dumps from your machine, or any machine-identifying data (serials, MAC addresses, hostnames, IP
addresses, local file paths). The tools and docs describe how to read those from your own hardware; they
do not redistribute them.

By contributing you agree your contribution is licensed under this repo's terms (MIT for code, CC BY
4.0 for docs and images).
