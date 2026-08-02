# Security Policy

## Supported versions

EdgeChainDB is currently a research-quality prototype. Security fixes are applied to the latest version on the `main` branch. Older revisions are not maintained.

| Version | Supported |
| --- | --- |
| Latest `main` | Yes |
| Older revisions | No |

## Reporting a vulnerability

Please do not open a public issue for a suspected vulnerability.

Use GitHub's private vulnerability reporting or security-advisory feature for this repository. Include:

- the affected component and version or commit;
- reproduction steps or a minimal proof of concept;
- the expected and observed behavior;
- the potential confidentiality, integrity, or availability impact;
- any proposed mitigation, if known.

Reports will be acknowledged as soon as practical. Confirmed issues will be investigated, fixed on a private branch when appropriate, and disclosed after a remediation is available.

## Security scope

The repository demonstrates tamper-evident IoT telemetry, signed device chains, Merkle blocks, quorum signatures, and durable local recovery. It is not yet a production security product. Production deployment additionally requires authenticated enrollment, TLS or mutual TLS, secret management, key rotation and revocation, authorization, rate limiting, backup and restore procedures, privacy controls, independent review, and deployment-specific threat modeling.
