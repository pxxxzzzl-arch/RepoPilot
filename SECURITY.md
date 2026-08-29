# Security Policy

## Supported version

Security fixes target the latest release and the `main` branch.

## Reporting a vulnerability

Do not open a public issue. Use GitHub's private vulnerability reporting for this repository:

https://github.com/pxxxzzzl-arch/RepoPilot/security/advisories/new

Include the affected version, impact, a minimal reproduction, and whether the issue can escape the repository or Docker boundary. Remove API keys, environment variables, proprietary source code, and full audit traces.

You should receive an acknowledgement within seven days. No bounty or response-time guarantee is offered.

## Security boundary

RepoPilot reduces risk; it is not a general-purpose malware sandbox. Untrusted target tests must use `DockerSandboxRunner`. The trusted local runner is intended only for repositories the operator has already reviewed and trusts.
