# Why this is worth doing in 2026

A 1-litre office PC with an empty PCIe slot and a datacenter GPU retired from service are both, on
paper, e-waste. Together they can make a small, cheap machine for local AI inference, a media server or a
lab. On some of these PCs the BIOS will not boot with such a card installed, because of a setting the
manufacturer ships but hides. This page gives the numbers behind doing that assembly instead of buying
new.

## Memory and accelerators are scarce and expensive

The AI buildout that made datacenter GPUs valuable also raised the price of everything around them.

- Conventional DRAM contract prices rose **93-98% quarter on quarter** (Q1 2026), and DRAM industry
  revenue reached about **$97 billion**.
  [TrendForce, 2026-06-01](https://www.trendforce.com/presscenter/news/20260601-13070.html)
- On the 2026-07-23 earnings call, Intel's CEO called the situation *"one of the most severe supply
  constraints in [the industry's] history"*, and its CFO said server-CPU demand *"continues to far
  outpace available supply"*.
  [Intel Q2 2026 call](https://download.intel.com/newsroom/2026/earnings/Intel-Q22026-Earnings-Call.pdf)
- IDC does not expect the shortage to ease **until early 2028**.
  [IDC, 2026-07-08](https://www.idc.com/resource-center/press-releases/2q26-pc-top5/)

Datacenter operators retire working low-wattage accelerators on their own refresh cycles, and those
cards reach the used market. For a home user the obstacle is often the board: a small-business desktop
that will not give the card the address space it needs.

## Much of the carbon is spent making the hardware

Apple's life-cycle report for its 2025 15-inch MacBook Air puts
[71%](https://apple.com/environment/pdf/products/notebooks/M4_MacBook_Air_PER_March2025.pdf) of the
laptop's lifetime emissions in production. That is one product, after Apple's own clean-energy cuts, so
other makers may differ. The world made **62 million tonnes of e-waste in 2022 and formally recycled under
a quarter of it**
([UN Global E-waste Monitor 2024](https://www.itu.int/en/ITU-D/Environment/Pages/Publications/The-Global-E-Waste-Monitor-2024.aspx)).

Reusing two pieces of hardware you already have, or can buy second-hand, avoids manufacturing a new
machine and keeps a working accelerator out of the scrap stream. Windows 10 support ended on
[2025-10-14](https://support.microsoft.com/en-us/windows/windows-10-consumer-extended-security-updates-esu-program-33e17de9-36b3-43bb-874d-6c53d2e4bf42),
and Dell's COO put the Windows-11-capable PCs not yet upgraded at about 500 million. Adding the roughly
500 million that cannot run Windows 11, the article that reports this arrives at nearly a billion PCs on
Windows 10
([reported via Pureinfotech](https://pureinfotech.com/billion-pcs-stuck-windows-10-dell/)), so capable
small desktops looking for a second job are easy to find.

## Where this unlock fits

A GPU with a large memory window (a 16 GB datacenter card, or one that uses resizable BAR) needs that
window mapped **above** the 4 GB line in the system's address space. The BIOS setting that allows this,
**Above 4G Decoding**, exists in the firmware of some OEM desktops that hide it from the menu. Those
boards stop at POST with *"PCI Out of Resources"* when the card is installed.

This project turns the hidden setting on by editing the firmware's own UEFI variables. It changes
settings data, not firmware code: no soldering and no chip reflash. That does not make a bad write
harmless. The evidence that it can be undone is thin, the backup restore needs a board that still boots
to an OS, and the reference board had no out-of-band recovery. [05](05-safety-and-recovery.md) has the
details. On the reference build the change turned a 1-litre PC stuck in a boot loop into one that booted
and ran a 16 GB accelerator.

---

*Figures here are dated and linked; some rest on secondary reporting, noted at each source. Re-check any
number before quoting it. The memory and GPU markets move monthly.*
