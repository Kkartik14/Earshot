# Local-only documentation

`docs/adr/` and `docs/research/` are local-only working directories. Keep their files
available in the working copy, but never stage, commit, or push them. They are ignored
by `.gitignore`; do not force-add them.

When public documentation needs the same information, write a concise, current
explanation in the appropriate public guide instead of copying from these directories.
