---
name: Device support
about: Ask for, or offer to add, support for another handheld
labels: device
---

**Device**
- Model:
- OS (Armada OS version, or another gamescope and InputPlumber system):
- InputPlumber version:

**Identification**

Output of `tr '\0' '\n' < /sys/firmware/devicetree/base/compatible` (ARM) or
`cat /sys/class/dmi/id/product_name` (x86):

```
```

The touchscreen's block from `cat /proc/bus/input/devices`:

```
```

**Can you test on the device?**

CONTRIBUTING.md, "Adding a device", lists what a new device needs.
