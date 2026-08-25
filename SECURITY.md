# Security

Please do not disclose suspected vulnerabilities in a public issue or pull
request. Report them privately through a security advisory or other private
contact channel provided by the repository hosting service, including the
affected component, reproduction steps, and impact.

If no private channel is currently available, ask the project maintainers for
one without sharing sensitive details publicly. We will acknowledge reports
when possible, investigate them, and coordinate disclosure after a fix or
mitigation is available.

Do not include real credentials, personal data, or production secrets in
reports. For general bugs without security impact, use the normal issue
tracker.

## CI vulnerability exceptions

CI scans locked Python/Node dependencies and the three production container
images. A temporary Trivy exception must be added to
`security/vulnerability-exceptions.yaml` with the CVE ID, a concrete
`statement`, and an `expired_at` date. Expired or malformed exceptions fail CI;
exceptions are not a substitute for upgrading a dependency.
