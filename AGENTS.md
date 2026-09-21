# Development and release checks

- Each feature needs basic checks and a local smoke test. Use a disposable database
  and mock providers for UI checks; never use real posting as a smoke test.
- Run focused regression checks for changed behavior. Broaden coverage for shared
  scheduling, authentication, upload or database changes. State what was actually
  verified and which external integrations were not exercised.
- Keep data, credentials, `.nicegui/` and `LOCAL_DEVELOPMENT.md` out of commits.
- Before delivering a local update, increment the application version with
  `python scripts/release_version.py --base origin/main --level patch|minor|major`.
  Use patch for fixes/copy/style, minor for backwards-compatible features and major
  for incompatible behavior/data changes. The command is idempotent relative to
  the base revision. Version lives in `x_operator/version.py`; do not duplicate it.
- Use Conventional Commit titles: `fix:`/`docs:`/`style:` for patches, `feat:` for
  features, and `feat!:` or a `BREAKING CHANGE:` body for breaking changes. The
  GitHub main workflow bumps unversioned updates using the highest scope in a push;
  a manually incremented version is preserved. Do not force-push around a failed
  release workflow or branch protection.
- New interface strings go through `x_operator.ui.i18n.t` with translations in
  `ui/locales/en.json`, `ja.json` and `zh-Hant.json`. Interpolate user data after translation. Do
  not translate database keys, user content, login credentials or model prompts.
