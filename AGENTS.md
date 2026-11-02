# Working on srdev

srdev is the tooling repository. The StarRocks checkout/worktree being built is a
separate repository. Start with `README.md`; for operational tasks read
`skills/srdev/SKILL.md` and only the references needed for the task.

- Treat the current scripts as authoritative. Historical review notes and timings
  describe earlier environments, not guarantees for this checkout.
- Preserve the single-container, multiple-dependency-version design. Site values
  belong in ignored `srdev.conf` / `registries.conf`; use anonymous examples.
- Never put customer information, credentials, or identifiable production data in
  code, filenames, branches, commits, or PRs. Inspect the actual diff before committing.
- `sr`, `Dockerfile`, and the tool files contribute to image identity. Changing them
  can block new work in an old container. Do not silently rebuild or restart a live
  environment as part of editing the repository.
- Build failures are not automatically retried. Public `-j` and `ut` / `--ut`
  were removed. BE UT defaults to ASAN + dynamic modules + PCH; normal builds
  default to Release, BE+FE. Keep docs consistent with the actual parser.
- Keep personal configuration, farm state, build outputs, and customer evidence
  outside version control. The root `srdev.conf.example` is the maintained template.
- Validate shell syntax, Python syntax, and `git diff --check`. Run Linux tests in
  a disposable container as described in `docs/development.md`; deployment tests
  must not use the live `/farm` or host network namespace.
- When adding a Docker `COPY` input, update `.dockerignore`'s allowlist too.

The reusable skill is maintained in `skills/srdev/`. Update relevant guidance when
changing CLI behavior, defaults, configuration, or operational invariants.
