---
name: Bug report
about: The controller misbehaves after sleep, or something in the plugin does not work
title: ""
labels: bug
---

**What happens**
Describe what you see (for example: "controller disconnects 28 seconds after wake", "buttons A and B are swapped after wake", "game list is empty").

**What you expected**

**Setup**
- Plugin version (shown in the plugin panel / `package.json`):
- SteamOS / distro and version:
- Decky Loader version:
- Controller (DualSense / DualSense Edge) and how it wakes the PC:

**Report**
In the plugin: Bluetooth settings > "Collect Bluetooth report", then run `bash deploy.sh ADDRESS fetch` (see the README) and attach the `summary.txt`.

> The report contains system logs and Bluetooth device addresses. Look through it before posting publicly.
