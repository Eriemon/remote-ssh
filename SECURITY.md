# Security Policy

## Supported Versions

Security fixes target the latest `main` branch unless a release branch is explicitly announced.

## Reporting a Vulnerability

Please report security issues through GitHub private vulnerability reporting if it is enabled for this repository. If that is unavailable, open a minimal public issue that requests a private coordination channel and does not include exploit details, secrets, server names, account names, or private infrastructure information.

## What Counts

- Secret exposure, credential leakage, or unsafe logging.
- Real hostnames, usernames, ports, key names, key paths, inventory snapshots, or local user paths committed to public files.
- Output redaction failures.
- Path traversal or file operations escaping the configured `workdir` boundary.
- Request-review bypasses for upload, mkdir, delete, or arbitrary command execution.
- Documentation that encourages unsafe SSH key, host-key, or remote-server handling.

## Handling Expectations

We will acknowledge valid reports, reproduce them in a minimal environment, and publish fixes with clear notes. Do not include private keys, real SSH targets, private server lists, proprietary project data, or private network details in a report.
