# CE shadow-mode smoke test

This documentation-only change exercises the public CE GitHub App after the
bootstrap workflow has been installed on `trunk`.

Expected behavior:

1. The App checks trusted OCA verification and an approval of the current PR head
   from a collaborator with Write, Maintain, or Admin access.
2. It validates the candidate's history and public-content policy.
3. `CE / public-ci` reports that builds are not applicable under the documentation
   allowlist, and `CE / policy` reports readiness once all gates pass.
4. Shadow mode leaves the PR and target branch unchanged and does not offer the
   active-mode Integrate action.

This smoke test does not exercise build/MTR dispatch, forward upmerges, or atomic
publication. Those require separate rehearsal changes and deployment setup.
