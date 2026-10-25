---
name: Bug report
about: Something does not work as described
labels: bug
---

**What happened, and what you expected**


**Steps to reproduce**


**Device and versions**
- Device:
- Armada OS version:
- armada-trackpad-daemon version or commit:

**Status**

Output of `armada-trackpadctl --json status`:

```json
```

**Logs**

`journalctl --user -u armada-trackpadd -b`, and for a problem with the root units
`journalctl -b -u armada-trackpad-keyboard -u armada-trackpad-firmware`:

```
```
