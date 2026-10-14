---
name: Hardware report (it worked / it didn't)
about: Tell others whether the unlock worked on a board, so the compatibility list grows
title: "[HW] <vendor> <model> + <GPU>: worked / failed"
labels: hardware-report
---

**A negative result is as useful as a positive one. Please do not include your MAC address,
hostnames, serial numbers, or local file paths.**

## The board
- Vendor / model / form factor (e.g. Lenovo ThinkCentre M720q Tiny):
- BIOS version:
- Did it already expose "Above 4G Decoding", or was it hidden?

## The variables
- Setup store GUID and the offset that mattered (e.g. 0xA49 "Above 4GB MMIO BIOS assignment"):
- PCI_COMMON (or equivalent) GUID and byte, if you used one:
- Variable attributes (e.g. 0x7 = NV+BS+RT), and whether you wrote from Linux/efivarfs or a UEFI shell:
- Was there an integrity guard (e.g. a Setup checksum / Error 0162)? How did you handle it?

## The GPU
- Model and BAR size (e.g. NVIDIA A2, 16 GB):
- Driver branch used (NVIDIA open vs proprietary):

## Result
- [ ] A >4 GB window appeared and the card booted / the driver bound
- [ ] Still failed with "PCI Out of Resources" / other (describe)
- Did you need `pci=realloc` or `pci=nocrs`?

## Recovery (if anything went wrong)
- What recovered it (Load Defaults, CMOS clear, variable restore, programmer)?

## Anything else
Notes that would help the next person.
