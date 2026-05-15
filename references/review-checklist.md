# Erie Remote SSH Review Checklist

Use this checklist before declaring an Erie Remote SSH task or skill change fully validated.

## Contents

- Skill Structure
- Safety and Privacy
- Schema and Selection
- Execution
- Validation

## Skill Structure

- `SKILL.md` frontmatter contains only `name` and `description`.
- The skill directory name, `SKILL.md` name, and `agents/openai.yaml` default prompt all use `erie-remote-ssh`.
- The description states both capability and trigger conditions.
- The body is concise and points to references for details.
- Every file under `references/` is directly linked from `SKILL.md`.
- No README, installation guide, changelog, or unrelated documentation is added inside the skill.
- Paths, validation targets, and tool locations are settings-driven rather than hardcoded in scripts.
- Software inventory probes, multi-version PATH scans, executable globs, Synopsys install roots, and Xilinx install roots are settings-driven through `inventory.software_catalog`.
- Discovery and add-server workflows are represented in `SKILL.md` without bloating the skill body.
- SSH config alias fallback and detached job workflows are represented in `SKILL.md` without bloating the skill body.
- The configuration gate is documented at both levels: the agent asks the user in conversation first, and the CLI requires explicit manual/script/cancel input before any server-list mutation.
- Key-only repair is documented at both levels: the agent asks the user before running `configure-key --interactive`, and the CLI writes only after candidate-key verification.
- Platform wrappers stay thin and delegate to `scripts/remote_ssh.py`.
- Platform-specific `.bat`, `.sh`, and `.ps1` wrappers live under `scripts/bat`, `scripts/shell`, or `scripts/powershell`; only Python helpers live directly in `scripts`.
- Runtime configuration does not depend on a repository-level `ref` directory.
- `config/server_list.template.json` exists and contains only placeholder, non-sensitive values.
- Markdown files are UTF-8 without BOM, contain no replacement characters, and the Chinese encoding canary round-trips through source, dist directory, and zip artifacts.
- Root and release `.gitattributes` declare UTF-8 working-tree encoding for `.gitattributes`, `.gitignore`, Markdown, YAML, and JSON text files.
- Release artifacts are built with `scripts/build_release.py`; source, `dist/erie-remote-ssh-v<version>`, `dist/erie-remote-ssh-v<version>.zip`, and the installed skill key files match byte-for-byte when installed validation is enabled.
- Skill-local `.gitignore` ignores `config/server_list.local.json`, backups, `reports/`, legacy request/download roots, tmp/log output, and complements repository-level ignores.
- Before updating or replacing the skill from GitHub, a local directory, a release artifact, or another source, `scripts/install_skill.py` creates a backup under `${CODEX_HOME:-~/.codex}/skill-backups`, preserves installed `config/server_list.local.json`, `config/server_list.local.json.bak.*`, and `reports/`, and reports `preserved_hash_verified: true`.
- Failed installations restore the backup and do not leave a partial mixed installed skill.
- Source artifacts must not contain `config/server_list.local.json` or server-list backup files; installation must reject them instead of overwriting user configuration.
- Source artifacts must not contain runtime directories such as `reports/`, `requests/`, `downloads/`, `tmp/`, or `logs/`; installation must reject them instead of silently skipping or reporting them as preserved.
- Backup directory failures, including a non-directory `${CODEX_HOME:-~/.codex}/skill-backups` path, must fail with a clean `error:` message and no traceback while leaving the installed target unchanged.

## Safety and Privacy

- Default CLI output redacts host, username, port, key name, and key path.
- Full connection details require an explicit `--show-sensitive` flag.
- Default SSH arguments do not accept or write new host keys.
- Any use of `--accept-new-host-key` is user-approved because it may update `known_hosts`.
- The workflow does not silently modify `~/.ssh`, generate keys, or rewrite SSH config.
- Guided key generation shows the target path, requires confirmation, refuses overwrite, explains the required one-time manual remote login, and never installs public keys remotely.
- Inventory output is treated as a report and does not update the source JSON; `scan-software` is the explicit write-back path for `software_scan`.
- `discover` reads configuration only and does not scan networks or probe unknown hosts.
- Missing server-list fallback reads OpenSSH config aliases only, excludes wildcard/pattern hosts, redacts sensitive HostName/User/Port/IdentityFile values by default, and never writes a server record.
- `add-server --interactive` does not generate keys without explicit user confirmation, modify SSH config, or scan networks; enabled additions connect only for the mandatory read-only software scan.
- `configure --interactive`, `add-server --interactive`, and `update-server --interactive` ask how to handle a missing private key before writing an enabled unusable entry.
- `configure-key --interactive` is limited to `key_name`, `validation`, `workspace_check`, and `software_scan`; it must not modify host, port, username, workdir, enabled, category, functions, or notes.
- `configure-key --interactive` generates local keys only after explicit confirmation, prints `authorized_keys` guidance, verifies passwordless SSH before JSON write-back, and leaves the server list unchanged on cancellation or verification failure.
- Failed mandatory scans are cached as `software_scan.status: failed` without discarding the server entry.
- `workspace-check` creates a server-list backup, writes validation/workspace metadata, and refreshes `software_scan` only when invoked explicitly.
- `setup-key` does not generate keys, copy public keys, run `ssh-copy-id`, edit `authorized_keys`, modify `~/.ssh`, or rewrite SSH config.
- Passwordless verification keeps `BatchMode=yes` in default SSH options.
- Server-list backups are ignored by git and do not expose sensitive details through committed files.
- Installed user server lists and backups are never compared to source/release artifacts and are never overwritten during installation.
- Request files do not contain real host, username, port, key name, or key path values.
- Project config files do not contain real host, username, port, key name, or key path values.
- `choices` output is redacted by default and does not connect, scan, or write the server list.
- Download, request, and validation temp directories default under `reports/` and are ignored or kept out of commits.
- Default runtime paths must not create root-level `out`, `remote-validation-bundles`, `requests`, `downloads`, or `tmp` directories next to `erie-remote-ssh`; only release builds intentionally create root-level `dist/`.

## Schema and Selection

- Config root is a JSON object with supported `version`.
- `servers` is an array and every entry is an object.
- Required server fields are present and non-empty where applicable.
- `enabled` is a JSON boolean, not a coerced string or number.
- Server selectors fail clearly on unknown or ambiguous matches.
- Same-host multi-login entries are allowed, and ambiguous host/user@host selectors require choosing an id/name or port before SSH.
- `choices` groups servers by `category`, shows explicit or inferred `functions`, and treats missing metadata as compatible legacy input.
- Disabled servers require explicit override.
- `init-config` creates the v1 empty list and refuses overwrite unless `--force` is explicit.
- `configure --interactive` offers manual, script, and cancel modes without a default before changing server configuration.
- Existing-server guided configuration shows a redacted server summary, then requires an explicit add/update/cancel action without a default.
- Interactive add groups prompts, accepts category/functions/notes metadata, shows a redacted summary, and rejects duplicate id/name selectors, invalid ports, empty required fields, and non-boolean enabled values.
- Interactive add warns on same-host entries and defaults to cancelling exact host+username+port duplicates.
- Interactive add runs and caches a software scan for enabled servers.
- Interactive update supports numbered selection through `configure`, field-menu edits, selected-field prompting, `all`, `done`, redacted save confirmation, and `cancel`; metadata-only edits preserve validation/workspace caches and do not force missing-key repair or software scans.
- Remote file paths reject empty paths, absolute paths, drive paths, backslashes, and parent traversal.
- Built-in write/delete/upload operations resolve remote targets under `workdir` again at execution time.
- Upload local source paths resolve under configured `paths.upload_roots`; request execution revalidates the recorded upload root and relative path.
- The default `${project_root}` boundary resolves to the repository root in a source checkout and to the skill root in installed or release copies.
- Sensitive local upload sources require `--confirm-sensitive-local-upload` and a non-empty reason at request creation and execution.

## Execution

- `list` defaults to enabled servers and redacted output.
- `choices` runs after discovery and before remote access when the user has not already selected a server.
- `check` runs before `command`, `exec`, or `inventory`.
- `command` does not connect.
- `exec` and `inventory` use positive timeouts.
- `command --ssh-alias` does not connect or read/write the server list; `exec --ssh-alias` uses OpenSSH alias fallback without forcing `-i` or cached workdir metadata.
- `exec --cmd` and `request-command --cmd` fail with a clear delimiter migration hint, while `--cmd` after `--` remains part of the remote command.
- `exec-detached` and `request-command --detached` create remote job state under the effective `workdir` and local manifests under `reports/jobs`; `status` and `tail-log` operate by job id, not arbitrary remote paths.
- `scan-software` uses a positive timeout and writes only the local `software_scan` cache.
- `workspace-check` uses a positive timeout, writes `validation` / `workspace_check`, and auto-refreshes `software_scan` after successful workspace validation.
- With project context active, `workspace-check` uses the project effective workdir, writes project workspace status to project config, and does not overwrite global server `workspace_check`.
- `project-init --interactive` checks the remote directory before writing local project config and asks for overwrite, rename, timestamp, or cancel on collisions.
- Project-context request files record `project_id`, `effective_workdir`, and `workdir_source`; `run-request` uses the recorded workdir.
- `workspace-check` and `exec -- echo ok` authentication failures include guidance to ask before running key-only `configure-key --interactive` repair.
- `software` reads cached install status, prints multi-version rows from cached `versions`, and does not connect to the remote host.
- Remote commands are short and non-destructive unless the user clearly requested otherwise.
- Missing optional inventory tools degrade to `not detected`.
- `run-request` requires `--execute` and re-runs local/server checks before acting.
- Recursive delete requires an explicit request flag.

## Validation

- Run the standard skill validator.
- Run positive CLI tests for choices, list, check, command, exec, software, scan-software, and inventory when network access is allowed.
- Run choices tests for category grouping, JSON output, legacy missing metadata, disabled visibility with `--all`, same-host `--host` filtering, redaction, and no server-list mutation.
- Run negative CLI tests for malformed JSON, unsupported version, invalid server entries, invalid fields, disabled targets, missing keys, and invalid timeouts.
- Run configuration tests for default settings, copied settings, environment-variable paths, and CLI overrides.
- Run discovery and add-server tests for missing lists, empty lists, enabled servers, backups, duplicate entries, same-host prompts, invalid ports, and empty required fields.
- Run SSH config fallback tests for missing server lists, alias redaction, wildcard exclusion, `--show-sensitive`, `command --ssh-alias`, `exec --ssh-alias`, and no server-list mutation.
- Run detached job tests for startup manifests, running/succeeded/failed status, tail-log line limits, and detached request execution.
- Run configure/update tests for manual mode, script mode, cancel mode, explicit no-default prompts, missing-key generate/disable/cancel branches, fake `ssh_keygen`, and server-list backups.
- Run encoding tests for UTF-8 no-BOM Markdown, no replacement characters, Chinese canary consistency, and source/dist/zip byte consistency.
- Run artifact-boundary tests confirming default requests, downloads, and validation temp paths stay under `${skill_dir}/reports/`, not root-level runtime directories.
- Run project workdir tests for config discovery priority, `--project-config`, `--project`, `--no-project`, collision handling, project workspace cache separation, and request project-context binding.
- Run software catalog tests for duplicate ids, invalid `path_scan`, unsafe executable globs, invalid directory scans, cached named-tool queries, multi-version table output, scan write-back, workspace-check write-back, and add-server scan write-back.
- Run passwordless setup tests for existing private/public key files and missing-key guidance.
- Run key-only repair tests for missing keys, generated keys, remote public-key confirmation, failed authentication, no-write failure behavior, and preservation of non-key server fields.
- Run configure script tests for Windows batch and POSIX shell where the shell runner is available.
- Run request and path-boundary tests for upload, mkdir, delete, command risk summaries, invalid paths, and missing `--execute`.
- Run upload-root tests for default roots, external configured roots, `${cwd}`, sensitive path confirmation, and tampered request JSON.
- Run real SSH file tests for workspace check, upload, list, stat, download, command request, and cleanup when network access is allowed.
- Run wrapper tests for `.bat`, `.ps1`, and `.sh` entry points where the runner exists.
- Run isolated no-ref validation from a temporary copy that does not contain a repository-level `ref` directory.
- Run installed-skill validation after copying the refreshed artifact into `$CODEX_HOME/skills/erie-remote-ssh`; stale installed files must fail validation.
- Run safe installer tests proving backups land under `$CODEX_HOME/skill-backups`, installed `server_list.local.json`, backup files, and `reports/` survive installation unchanged, protected hashes are verified, and failed copies roll back.
- Run installer rejection tests for source runtime artifacts and invalid backup directory paths; both must leave the target unchanged and print no traceback.
- Confirm `--with-ssh` requires an explicit real `--server-list`; without one, only offline skill development confidence can be claimed.
- Confirm `git status` is clean after validation.
