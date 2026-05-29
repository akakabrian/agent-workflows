---
name: Bug report
about: Report something that doesn't work as documented
title: "[bug] "
labels: bug
---

**What happened**
A clear description of the bug.

**What you expected**
What you expected to happen instead.

**Reproduction**
A minimal workflow script and the exact `owf` command you ran:

```python
# minimal_repro.py
```

```bash
owf run minimal_repro.py --provider fake
```

**Environment**
- OS:
- Python version (`python3 --version`):
- Provider used (`fake` / `claude` / `codex`):
- Open Agent Workflows version / commit:

**Artifacts**
If relevant, attach `summary.md` / `report.md` from the run directory
(`.workflows/runs/<run_id>/`). Do not paste secrets.
