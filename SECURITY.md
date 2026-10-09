# Security and privacy

This application is for a small set of trusted users of one shared notebook.
Cloudflare Access policies decide who may enter; the API checks signed Access
tokens independently. Jobs and history are scoped to the authenticated subject,
but notebook contents are shared among authorized users.

The application sends sanitized content to a remote model. Read
[PRIVACY.md](PRIVACY.md) before using real notes. Explicit private markers are
needed for confidential content that automatic rules cannot recognize.

Keep personal notes and credentials outside the repository and static web root.
Do not publish real source documents, JWTs, passwords, model output, production
configuration, or identifying screenshots in an issue or pull request. Reproduce
problems with fictional data. Report an exploitable vulnerability using the
repository's private vulnerability reporting feature when available.

The test suite covers authentication, job ownership, privacy parsing and outbound
validation, cancellation, resource limits, source rendering, and frontend recovery.
It uses fake model processes; it does not verify a particular external model or
the effective policy of a deployed Cloudflare account.
