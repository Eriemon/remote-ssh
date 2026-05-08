# Contributing

Thank you for improving remote-ssh. This repository is an agent skill first: changes should help an AI coding agent use SSH with clearer target selection, safer defaults, and better validation evidence.

## Contribution Principles

- Keep `SKILL.md` concise and operational.
- Move detailed background, schemas, workflows, and long examples into `references/`.
- Keep deterministic SSH workflow logic in `scripts/remote_ssh.py`.
- Keep platform wrappers thin and delegate to the Python helper.
- Do not modify `~/.ssh`, generate keys, rewrite SSH config, or accept new host keys unless the user explicitly requested that behavior.
- Do not claim remote validation, inventory, or software checks passed unless the corresponding command actually ran.
- Keep real hostnames, usernames, ports, key names, key paths, inventory snapshots, logs, request files, downloads, and local machine paths out of commits.

## Suggested Workflow

1. Open an issue describing the agent behavior, SSH safety problem, configuration case, or documentation improvement.
2. Make a focused change with a clear before/after behavior.
3. Run the relevant offline validation and CLI checks.
4. Include command output or validation evidence in the pull request.

## Validation

Useful local commands:

```powershell
python .\scripts\remote_ssh.py --help
python .\scripts\remote_ssh.py discover --help
python .\scripts\remote_ssh.py list --help
python .\scripts\validate_remote_ssh.py
```

Real SSH checks are optional for most documentation and offline validation changes, but required before claiming behavior against an actual remote host:

```powershell
python .\scripts\validate_remote_ssh.py --with-ssh --server-list <private-server-list.json>
```

## Documentation Expectations

- Keep the default `README.md` in English.
- Put Chinese user-facing documentation in `README-CN.md`.
- Keep examples short, reproducible, and free of real infrastructure details.
- Describe sensitive output handling explicitly when changing command behavior.
